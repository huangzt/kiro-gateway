# -*- coding: utf-8 -*-

# Kiro Gateway
# https://github.com/jwadow/kiro-gateway
# Copyright (C) 2025 Jwadow
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
Account pool for multi-account queue-based request management.

Provides fair, queue-based request scheduling across multiple Kiro accounts.
Each account is serialized (concurrency=1) to avoid 429 rate limiting.
Total concurrency = number of accounts.

Architecture:
    - AccountSlot: Wraps a single KiroAuthManager with a semaphore
    - AccountPool: Manages multiple AccountSlots with FIFO queue scheduling

Usage:
    # Multi-account mode
    pool = AccountPool.from_directory("~/.kiro-accounts", region="us-east-1")

    # Single-account mode (backward compatible)
    pool = AccountPool.from_single(auth_manager)

    # Acquire and release
    slot = await pool.acquire(timeout=300)
    try:
        token = await slot.auth_manager.get_access_token()
        # ... use token ...
    finally:
        await pool.release(slot)
"""

import asyncio
import json
import time
import shutil
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from kiro.auth import KiroAuthManager
from kiro.quota_checker import QuotaInfo, check_quota, QuotaCheckError
from kiro.config import KIRO_HOST_CACHE_DIR, SESSION_STICKY_MAX_ENTRIES, SESSION_STICKY_TTL_SECONDS
from kiro.account_proxy import load_proxy_url_from_account_dir, mask_proxy_url


def _account_name_sort_key(name: str) -> tuple[int, str]:
    """
    Build a natural sort key for account names like `account-1`, `account-10`.

    Accounts that match `account-<number>` are sorted by numeric suffix.
    Non-matching names are sorted lexicographically after numeric accounts.
    """
    prefix = "account-"
    if name.startswith(prefix):
        suffix = name[len(prefix):]
        if suffix.isdigit():
            return (0, f"{int(suffix):010d}")
    return (1, name)


@dataclass
class AccountSlot:
    """
    Represents a single account slot in the pool.

    Each slot wraps a KiroAuthManager and a semaphore that limits
    concurrency to 1. This ensures requests using the same account
    are serialized to avoid 429 rate limiting from Kiro API.

    Attributes:
        name: Human-readable account identifier (directory name or "default")
        auth_manager: Authentication manager for this account
        semaphore: Semaphore with limit=1 for serial execution
        active_requests: Counter of currently active requests (for monitoring)
        total_requests: Counter of total requests served (for monitoring)
        cooldown_until: Unix timestamp when cooldown expires (0 = not cooling)
        is_exhausted: Whether this account's quota is used up
        is_disabled: Whether this account has been manually disabled via Admin UI
        quota_info: Latest quota info from Kiro Web Portal API
        proxy_url: Optional per-account outbound proxy (same semantics as VPN_PROXY_URL).
    """

    name: str
    auth_manager: KiroAuthManager
    proxy_url: Optional[str] = None
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))
    active_requests: int = 0
    total_requests: int = 0
    cooldown_until: float = 0.0
    is_exhausted: bool = False
    is_disabled: bool = False
    quota_info: Optional[QuotaInfo] = None
    _in_queue: bool = False  # Internal flag to prevent duplicates

    @property
    def is_cooling_down(self) -> bool:
        """Check if this slot is currently in cooldown."""
        return self.cooldown_until > time.monotonic()

    @property
    def cooldown_remaining(self) -> float:
        """Seconds remaining in cooldown (0 if not cooling)."""
        remaining = self.cooldown_until - time.monotonic()
        return max(0.0, remaining)

    @property
    def is_active_on_host(self) -> bool:
        """
        Check if this account's credentials match the files in KIRO_HOST_CACHE_DIR.
        Compares refreshTokens for better reliability than binary file comparison.
        """
        if not KIRO_HOST_CACHE_DIR:
            return False

        host_dir = Path(KIRO_HOST_CACHE_DIR)
        if not host_dir.exists():
            return False

        creds_dir = self.auth_manager.creds_dir
        if not creds_dir or not creds_dir.exists():
            return False

        host_token_path = host_dir / "kiro-auth-token.json"
        account_token_path = creds_dir / "kiro-auth-token.json"

        if not host_token_path.exists() or not account_token_path.exists():
            return False

        try:
            with open(host_token_path, "r", encoding="utf-8") as f:
                host_data = json.load(f)
            with open(account_token_path, "r", encoding="utf-8") as f:
                account_data = json.load(f)
                
            # Priority 1: Persistent RefreshToken
            host_rt = host_data.get("refreshToken")
            acc_rt = account_data.get("refreshToken")
            if host_rt and acc_rt:
                return host_rt == acc_rt
                
            # Priority 2: Profile ARN (very stable)
            host_arn = host_data.get("profileArn")
            acc_arn = account_data.get("profileArn")
            if host_arn and acc_arn:
                return host_arn == acc_arn

            # Priority 3: Client ID (SSO)
            host_cid = host_data.get("clientId")
            acc_cid = account_data.get("clientId")
            if host_cid and acc_cid:
                return host_cid == acc_cid

            # Last Resort: AccessToken or content match
            return host_data.get("accessToken") == account_data.get("accessToken")
            
        except Exception as e:
            logger.debug(f"Host active check failed for {self.name}: {e}")
            return False

    @property
    def email(self) -> str:
        """Account email from quota info, or empty string if unknown."""
        return self.quota_info.email if self.quota_info else ""

    @property
    def quota_summary(self) -> str:
        """Human-readable quota summary like '3.5 / 550' or 'unknown'."""
        if self.quota_info:
            return self.quota_info.usage_summary
        return "unknown"

    def __repr__(self) -> str:
        cooldown_str = f", cooldown={self.cooldown_remaining:.0f}s" if self.is_cooling_down else ""
        exhausted_str = ", EXHAUSTED" if self.is_exhausted else ""
        disabled_str = ", DISABLED" if self.is_disabled else ""
        quota_str = f", quota={self.quota_summary}" if self.quota_info else ""
        return (
            f"AccountSlot(name={self.name!r}, "
            f"active={self.active_requests}, "
            f"total={self.total_requests}{quota_str}{cooldown_str}{exhausted_str}{disabled_str})"
        )


class AccountPool:
    """
    Manages multiple Kiro account slots with fair queue-based scheduling.

    Ensures each account only handles one request at a time to prevent
    429 rate limiting. Requests are distributed across accounts using
    a FIFO queue for fair scheduling.

    The pool supports two modes:
    1. Single-account mode: Created from a single KiroAuthManager
    2. Multi-account mode: Created from a directory of credential files

    Attributes:
        slots: All account slots in the pool
        size: Number of accounts in the pool
    """

    def __init__(self, slots: List[AccountSlot], quota_check_interval: float = 300.0) -> None:
        """
        Initialize account pool with given slots.

        Args:
            slots: List of AccountSlot instances (can be empty for multi-account mode)
            quota_check_interval: Seconds between quota checks per account.
                                  Default is 300 (5 minutes). Set to 0 to disable.
        """
        self._slots = slots
        self._idle: deque[AccountSlot] = deque()
        self._idle_cv = asyncio.Condition()
        self._session_bindings: OrderedDict[str, str] = OrderedDict()
        self._session_binding_mono: Dict[str, float] = {}
        self._session_bindings_lock = asyncio.Lock()
        self._quota_check_interval = quota_check_interval
        self._broadcaster = None  # Will be set by main.py after initialization
        self._admin_config: Optional[Any] = None  # AdminConfig (set by main.py)

        # Status broadcast throttling (Throttle + Trailing)
        self._last_broadcast_time = 0.0
        self._pending_broadcast = False
        self._broadcast_timer: Optional[asyncio.Task] = None

        # Pre-populate idle deque with all available slots
        for slot in self._slots:
            if not slot.is_exhausted and not slot.is_disabled:
                slot._in_queue = True
                self._idle.append(slot)

        if not slots:
            logger.warning(
                "AccountPool initialized with 0 accounts. "
                "Add accounts via Admin UI to start processing requests."
            )
        else:
            logger.info(
                f"AccountPool initialized: {len(self._slots)} account(s), "
                f"max concurrency={len(self._slots)}, "
                f"quota_check_interval={quota_check_interval:.0f}s"
            )
        for slot in self._slots:
            logger.debug(f"  Account slot: {slot.name}")

    @property
    def slots(self) -> List[AccountSlot]:
        """All account slots in the pool."""
        return self._slots

    @property
    def size(self) -> int:
        """Number of accounts in the pool."""
        return len(self._slots)

    @property
    def available_count(self) -> int:
        """Number of currently available (idle and enabled) account slots."""
        return sum(1 for slot in self._slots if slot._in_queue and not slot.is_disabled and not slot.is_exhausted)

    @property
    def busy_count(self) -> int:
        """Number of currently busy account slots."""
        return sum(1 for slot in self._slots if slot.active_requests > 0)

    @property
    def exhausted_count(self) -> int:
        """Number of accounts that have reached their quota."""
        return sum(1 for slot in self._slots if slot.is_exhausted)

    def _idle_waiting_count(self) -> int:
        """Number of idle account slots waiting in the fair deque."""
        return len(self._idle)

    async def _idle_wait_popleft(self, wait_seconds: float) -> AccountSlot:
        """
        Pop the leftmost idle slot, waiting up to ``wait_seconds`` for one to appear.

        Raises:
            asyncio.TimeoutError: If no slot becomes available in time.
        """
        async with self._idle_cv:
            deadline = time.monotonic() + max(0.0, wait_seconds)
            while True:
                if self._idle:
                    slot = self._idle.popleft()
                    slot._in_queue = False
                    return slot
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                try:
                    await asyncio.wait_for(self._idle_cv.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    if time.monotonic() >= deadline:
                        raise asyncio.TimeoutError

    async def _idle_offer(self, slot: AccountSlot) -> None:
        """Append a slot to the idle deque (FIFO tail) if it may accept work."""
        async with self._idle_cv:
            if slot._in_queue:
                return
            if slot.is_disabled or slot.is_exhausted:
                return
            slot._in_queue = True
            self._idle.append(slot)
            self._idle_cv.notify_all()

    async def _idle_remove_slot_if_present(self, slot: AccountSlot) -> None:
        """Remove a slot from the idle deque if present (e.g. pool removal)."""
        async with self._idle_cv:
            try:
                self._idle.remove(slot)
            except ValueError:
                return
            slot._in_queue = False
            self._idle_cv.notify_all()

    def _session_prune_stale(self) -> None:
        """Drop session bindings older than SESSION_STICKY_TTL_SECONDS."""
        now = time.monotonic()
        cutoff = now - float(SESSION_STICKY_TTL_SECONDS)
        stale = [k for k, t in self._session_binding_mono.items() if t < cutoff]
        for k in stale:
            self._session_bindings.pop(k, None)
            self._session_binding_mono.pop(k, None)

    def _session_touch(self, session_key: str, account_name: str) -> None:
        """Record or refresh a session-to-account binding (LRU capped)."""
        self._session_bindings[session_key] = account_name
        self._session_bindings.move_to_end(session_key)
        self._session_binding_mono[session_key] = time.monotonic()
        while len(self._session_bindings) > int(SESSION_STICKY_MAX_ENTRIES):
            old_k, _ = self._session_bindings.popitem(last=False)
            self._session_binding_mono.pop(old_k, None)

    async def acquire_for_session(
        self,
        session_key: Optional[str],
        model_id: Optional[str] = None,
        timeout: float = 300.0,
    ) -> AccountSlot:
        """
        Acquire a slot, optionally pinning follow-up requests to one account.

        When ``session_key`` is non-empty and a binding exists or is created,
        subsequent calls with the same key reuse the same account when possible.

        Args:
            session_key: Client-supplied stable id (e.g. from ``X-Kiro-Session-Id``).
            model_id: Optional model id for plan-based filtering.
            timeout: Max seconds to wait for a suitable slot.

        Returns:
            An AccountSlot ready for use.

        Raises:
            asyncio.TimeoutError: If no slot becomes available (same as acquire_for_model).
        """
        sk = (session_key or "").strip()
        if not sk:
            return await self.acquire_for_model(model_id=model_id, timeout=timeout)

        async with self._session_bindings_lock:
            self._session_prune_stale()
            bound = self._session_bindings.get(sk)

        if bound:
            try:
                slot = await self._acquire_slot_by_name_for_model(
                    bound, model_id=model_id, timeout=timeout
                )
                async with self._session_bindings_lock:
                    self._session_touch(sk, slot.name)
                return slot
            except asyncio.TimeoutError:
                async with self._session_bindings_lock:
                    self._session_bindings.pop(sk, None)
                    self._session_binding_mono.pop(sk, None)
                sk_short = sk if len(sk) <= 24 else sk[:21] + "..."
                logger.info(
                    f"Session '{sk_short}' binding to account '{bound}' cleared; "
                    "selecting a new account."
                )

        slot = await self.acquire_for_model(model_id=model_id, timeout=timeout)
        async with self._session_bindings_lock:
            self._session_touch(sk, slot.name)
        return slot

    async def _acquire_slot_by_name_for_model(
        self,
        account_name: str,
        model_id: Optional[str] = None,
        timeout: float = 300.0,
    ) -> AccountSlot:
        """
        Wait until the named account slot is idle and acquire it for ``model_id``.

        Raises:
            asyncio.TimeoutError: If the account never becomes available or cannot serve the model.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError(
                    f"Account '{account_name}' did not become available within timeout"
                )
            slot_ref = self.get_slot_by_name(account_name)
            if slot_ref is None or slot_ref.is_disabled or slot_ref.is_exhausted:
                raise asyncio.TimeoutError(f"Account '{account_name}' is no longer available")
            if model_id and self._admin_config and not self._is_model_enabled_for_slot(slot_ref, model_id):
                raise asyncio.TimeoutError(
                    f"Model '{model_id}' is not enabled for account '{account_name}'"
                )

            slot: Optional[AccountSlot] = None
            async with self._idle_cv:
                idx: Optional[int] = None
                for i, s in enumerate(self._idle):
                    if s.name == account_name:
                        idx = i
                        break
                if idx is not None:
                    slot = self._idle[idx]
                    del self._idle[idx]
                    slot._in_queue = False
                    self._idle_cv.notify_all()
                else:
                    try:
                        await asyncio.wait_for(self._idle_cv.wait(), timeout=min(remaining, 1.0))
                    except asyncio.TimeoutError:
                        pass

            if slot is None:
                continue

            if self._quota_check_interval > 0:
                await self._maybe_refresh_quota(slot)

            if slot.is_exhausted or slot.is_disabled:
                raise asyncio.TimeoutError(f"Account '{account_name}' became unavailable during acquire")

            if model_id and self._admin_config and not self._is_model_enabled_for_slot(slot, model_id):
                raise asyncio.TimeoutError(
                    f"Model '{model_id}' is not enabled for account '{account_name}' after quota refresh"
                )

            slot.active_requests += 1
            slot.total_requests += 1
            email_str = f" ({slot.email})" if slot.email else ""
            quota_str = f", quota: {slot.quota_summary}" if slot.quota_info else ""
            plan_str = f", plan: {slot.quota_info.subscription_plan}" if slot.quota_info else ""
            model_part = repr(model_id) if model_id is not None else "(no filter)"
            logger.info(
                f"Account acquired (session-bound) for model {model_part}: {slot.name}{email_str} "
                f"(active={slot.active_requests}, total={slot.total_requests}{quota_str}{plan_str})"
            )
            await self._broadcast_status_update()
            return slot

    async def acquire(self, timeout: float = 300.0) -> AccountSlot:
        """
        Acquire an available account slot from the pool.

        Blocks until a slot becomes available or timeout is reached.
        Uses FIFO queue for fair scheduling across accounts.
        Before returning, checks if the slot's quota needs refreshing
        (based on quota_check_interval). If quota is exhausted, the slot
        is discarded and the next available slot is tried.

        Args:
            timeout: Maximum seconds to wait for an available slot.
                     Default is 300 seconds (5 minutes).

        Returns:
            An AccountSlot ready for use

        Raises:
            asyncio.TimeoutError: If no slot becomes available within timeout
        """
        logger.debug(
            f"Acquiring account slot... "
            f"(available={self.available_count}, busy={self.busy_count}, "
            f"queue_waiting={self._idle_waiting_count()})"
        )

        deadline = time.monotonic() + timeout

        while True:
            remaining_timeout = deadline - time.monotonic()
            if remaining_timeout <= 0:
                # Timeout reached - analyze why and provide specific error message
                if self.size == 0:
                    logger.error("No accounts configured in the pool. Add accounts via Admin UI.")
                    raise asyncio.TimeoutError("No accounts available")

                # Check specific reasons for timeout
                all_exhausted = all(slot.is_exhausted for slot in self._slots)
                all_cooling = all(
                    slot.cooldown_until > time.monotonic()
                    for slot in self._slots
                    if not slot.is_exhausted
                )

                if all_exhausted:
                    logger.error(
                        f"All {self.size} account(s) have exhausted their quota. "
                        f"Cannot process any requests until quota resets or new accounts are added."
                    )
                    raise asyncio.TimeoutError(
                        "All accounts exhausted. Service unavailable until quota reset."
                    )
                elif all_cooling:
                    logger.error(
                        f"All {self.size} account(s) are in cooldown due to rate limiting. "
                        f"Cannot process requests until cooldown expires."
                    )
                    raise asyncio.TimeoutError(
                        "All accounts rate-limited. Service temporarily unavailable."
                    )
                else:
                    logger.error(
                        f"Queue timeout after {timeout}s - all {self.size} account(s) busy"
                    )
                    raise asyncio.TimeoutError("All accounts busy. Service overloaded.")

            try:
                slot = await self._idle_wait_popleft(remaining_timeout)
            except asyncio.TimeoutError:
                # Timeout during queue.get() - analyze why and provide specific error message
                if self.size == 0:
                    logger.error("No accounts configured in the pool. Add accounts via Admin UI.")
                    raise asyncio.TimeoutError("No accounts available")

                # Check specific reasons for timeout
                all_exhausted = all(slot.is_exhausted for slot in self._slots)
                all_cooling = all(
                    slot.cooldown_until > time.monotonic()
                    for slot in self._slots
                    if not slot.is_exhausted
                )

                if all_exhausted:
                    logger.error(
                        f"All {self.size} account(s) have exhausted their quota. "
                        f"Cannot process any requests until quota resets or new accounts are added."
                    )
                    raise asyncio.TimeoutError(
                        "All accounts exhausted. Service unavailable until quota reset."
                    )
                elif all_cooling:
                    logger.error(
                        f"All {self.size} account(s) are in cooldown due to rate limiting. "
                        f"Cannot process requests until cooldown expires."
                    )
                    raise asyncio.TimeoutError(
                        "All accounts rate-limited. Service temporarily unavailable."
                    )
                else:
                    logger.error(
                        f"Queue timeout after {timeout}s - all {self.size} account(s) busy"
                    )
                    raise asyncio.TimeoutError("All accounts busy. Service overloaded.")

            # Check quota if interval has elapsed
            if self._quota_check_interval > 0:
                await self._maybe_refresh_quota(slot)

            # If slot became exhausted after quota check, skip it
            if slot.is_exhausted:
                # Check if this is the last available account
                available_slots = [
                    s for s in self._slots
                    if not s.is_exhausted and not s.is_disabled
                ]

                if not available_slots:
                    logger.error(
                        f"Last available account '{slot.name}' is exhausted. "
                        f"All {self.size} account(s) have reached quota limits. "
                        f"Service unavailable until quota resets."
                    )
                    raise asyncio.TimeoutError(
                        "All accounts exhausted. No quota remaining."
                    )
                else:
                    logger.warning(
                        f"Skipping exhausted account '{slot.name}' "
                        f"({slot.email}, quota: {slot.quota_summary}). "
                        f"{len(available_slots)} account(s) still available."
                    )
                # Don't put it back - it's permanently out
                continue

            # If slot was disabled via Admin UI, skip it
            if slot.is_disabled:
                logger.debug(f"Skipping disabled account '{slot.name}'")
                # Don't put it back - will be re-added when enabled
                continue

            # Slot is good, mark as active
            slot.active_requests += 1
            slot.total_requests += 1

            # Build info log with email and quota
            email_str = f" ({slot.email})" if slot.email else ""
            quota_str = f", quota: {slot.quota_summary}" if slot.quota_info else ""

            logger.info(
                f"Account acquired: {slot.name}{email_str} "
                f"(active={slot.active_requests}, total={slot.total_requests}{quota_str})"
            )

            # Broadcast status update after acquire
            await self._broadcast_status_update()

            return slot

    def _is_model_enabled_for_slot(self, slot: AccountSlot, model_id: str) -> bool:
        """
        Check if a model is enabled for the given account slot.

        Uses model control configuration from admin config.

        Logic:
        - If no admin config → all models enabled (backward compatible)
        - If no plan info for slot → all models enabled
        - If plan type has NO model controls configured → all models enabled
        - If plan type HAS model controls → only explicitly listed models are enabled
          (models not in the list are considered disabled for that plan)

        Args:
            slot: The account slot to check
            model_id: The model ID to check

        Returns:
            True if model is enabled for this slot's plan type, False otherwise
        """
        if not self._admin_config:
            return True

        if not slot.quota_info or not slot.quota_info.subscription_plan:
            return True

        plan_type = slot.quota_info.subscription_plan
        controls: Dict[str, Dict[str, bool]] = self._admin_config.get_model_controls()
        plan_controls = controls.get(plan_type, {})

        if not plan_controls:
            return True

        if model_id in plan_controls:
            return bool(plan_controls[model_id])

        return False

    async def acquire_for_model(
        self,
        model_id: Optional[str] = None,
        timeout: float = 300.0,
    ) -> AccountSlot:
        """
        Acquire an available account slot that supports the specified model.

        If model_id is provided, only returns slots where the model is enabled
        for that slot's plan type. Falls back to regular acquire if model_id
        is None or no model controls are configured.

        Args:
            model_id: The model ID to check against plan-specific controls
            timeout: Maximum seconds to wait for an available slot

        Returns:
            An AccountSlot ready for use with the specified model

        Raises:
            asyncio.TimeoutError: If no suitable slot becomes available
        """
        if model_id is None or not self._admin_config:
            return await self.acquire(timeout)

        deadline = time.monotonic() + timeout
        skipped_slots: List[AccountSlot] = []

        try:
            while True:
                remaining_timeout = deadline - time.monotonic()
                if remaining_timeout <= 0:
                    break

                try:
                    slot = await self._idle_wait_popleft(min(remaining_timeout, 1.0))
                except asyncio.TimeoutError:
                    if time.monotonic() >= deadline:
                        break
                    continue

                if self._is_model_enabled_for_slot(slot, model_id):
                    for skipped in skipped_slots:
                        if not skipped._in_queue and not skipped.is_exhausted and not skipped.is_disabled:
                            await self._idle_offer(skipped)
                    skipped_slots.clear()

                    if self._quota_check_interval > 0:
                        await self._maybe_refresh_quota(slot)

                    if slot.is_exhausted or slot.is_disabled:
                        continue

                    slot.active_requests += 1
                    slot.total_requests += 1

                    email_str = f" ({slot.email})" if slot.email else ""
                    quota_str = f", quota: {slot.quota_summary}" if slot.quota_info else ""
                    plan_str = f", plan: {slot.quota_info.subscription_plan}" if slot.quota_info else ""

                    logger.info(
                        f"Account acquired for model '{model_id}': {slot.name}{email_str} "
                        f"(active={slot.active_requests}, total={slot.total_requests}{quota_str}{plan_str})"
                    )

                    await self._broadcast_status_update()
                    return slot

                plan_type = slot.quota_info.subscription_plan if slot.quota_info else "unknown"
                logger.debug(
                    f"Skipping slot '{slot.name}' for model '{model_id}': "
                    f"model disabled for plan '{plan_type}'"
                )
                skipped_slots.append(slot)
        finally:
            for skipped in skipped_slots:
                if not skipped._in_queue and not skipped.is_exhausted and not skipped.is_disabled:
                    await self._idle_offer(skipped)

        raise asyncio.TimeoutError(
            f"No available account supports model '{model_id}' within timeout"
        )

    async def _maybe_refresh_quota(self, slot: AccountSlot) -> None:
        """
        Refresh quota info for a slot if the cached info is stale.

        Args:
            slot: The AccountSlot to check
        """
        # Skip if quota check is disabled
        if self._quota_check_interval <= 0:
            return

        # Skip if quota was checked recently
        if slot.quota_info and slot.quota_info.age_seconds() < self._quota_check_interval:
            return

        # Refresh quota
        await self._check_slot_quota(slot)

    async def _check_slot_quota(self, slot: AccountSlot) -> None:
        """
        Query Kiro Web Portal API for the slot's current quota.

        On success, updates slot.quota_info and may set slot.is_exhausted.
        On failure, logs a warning but does NOT mark the slot as exhausted
        (fail-open: if we can't check, we let the request through).

        Args:
            slot: The AccountSlot to check quota for
        """
        try:
            access_token = await slot.auth_manager.get_access_token()
            provider = getattr(slot.auth_manager, '_provider', 'BuilderId')
            quota = await check_quota(access_token, provider, proxy_url=slot.proxy_url)
            slot.quota_info = quota

            if quota.is_exhausted and not getattr(quota, "overage_enabled", False):
                slot.is_exhausted = True
                logger.error(
                    f"Account '{slot.name}' ({quota.email}) is EXHAUSTED "
                    f"(quota: {quota.usage_summary}, plan: {quota.subscription_plan}). "
                    f"Removed from active pool."
                )
                # Broadcast status update when account becomes exhausted
                await self._broadcast_status_update()
            elif quota.is_exhausted and getattr(quota, "overage_enabled", False):
                slot.is_exhausted = False
                logger.info(
                    f"Account '{slot.name}' ({quota.email}) has overage enabled, "
                    f"ignoring exhausted quota (quota: {quota.usage_summary}). "
                    f"Plan: {quota.subscription_plan}"
                )
            else:
                logger.info(
                    f"Quota check: {slot.name} ({quota.email}) "
                    f"quota: {quota.usage_summary}, "
                    f"plan: {quota.subscription_plan}, "
                    f"next reset: {quota.next_reset}"
                )
        except QuotaCheckError as e:
            logger.warning(f"Quota check failed for '{slot.name}': {e}")
        except Exception as e:
            logger.warning(f"Unexpected error checking quota for '{slot.name}': {e}")

    async def initialize_quota(self, concurrency: int = 5) -> None:
        """
        Query quota for all accounts proactively.

        Uses a semaphore to limit concurrent network requests during startup or refresh,
        preventing system/network overload. This is designed to be safe for
        background execution.
        """
        if not self._slots:
            logger.info("Skipping quota initialization: no accounts configured")
            return

        logger.info(f"Starting proactive quota check for {len(self._slots)} accounts (concurrency={concurrency})...")

        sem = asyncio.Semaphore(concurrency)

        async def _check_with_sem(slot: AccountSlot):
            async with sem:
                await self._check_slot_quota(slot)

        # Check all slots concurrently with limited parallelism
        tasks = [_check_with_sem(slot) for slot in self._slots]
        await asyncio.gather(*tasks, return_exceptions=True)

        active_count = sum(1 for s in self._slots if not s.is_exhausted)
        exhausted_count = len(self._slots) - active_count

        if exhausted_count > 0:
            logger.warning(
                f"Proactive quota check finished: {exhausted_count} exhausted account(s) detected. "
                f"These will be skipped during request acquisition."
            )

        logger.info(
            f"Quota initialization finished: {active_count}/{len(self._slots)} account(s) active"
        )

    async def release(
        self,
        slot: AccountSlot,
        cooldown_seconds: float = 0.0,
        exhausted: bool = False
    ) -> None:
        """
        Release an account slot back to the pool.

        Makes the slot available for the next waiting request.
        Must be called after acquire(), typically in a finally block.

        If exhausted is True, the slot is marked as exhausted and will NOT
        be returned to the queue, effectively disabling it.

        If cooldown_seconds > 0, the slot is put on cooldown and will not
        be available until the cooldown expires. This is used when the
        account hits a 429 rate limit from Kiro API.

        Args:
            slot: The AccountSlot to release
            cooldown_seconds: Seconds to cool down before reuse (0 = immediate)
            exhausted: If True, mark account as out of quota and disable it
        """
        slot.active_requests -= 1

        if exhausted:
            # Account is out of quota - disable it
            slot.is_exhausted = True
            async with self._session_bindings_lock:
                stale_keys = [k for k, v in self._session_bindings.items() if v == slot.name]
                for k in stale_keys:
                    self._session_bindings.pop(k, None)
                    self._session_binding_mono.pop(k, None)
            logger.error(
                f"Account '{slot.name}' is EXHAUSTED (quota reached). "
                f"It has been removed from the active pool."
            )
            # We don't put it back in the queue
            return

        if cooldown_seconds > 0:
            # Put slot on cooldown - delayed reinsertion into queue
            slot.cooldown_until = time.monotonic() + cooldown_seconds
            logger.warning(
                f"Account '{slot.name}' entering cooldown for {cooldown_seconds:.0f}s "
                f"(rate limited by Kiro API)"
            )
            # Schedule delayed reinsertion (fire-and-forget)
            asyncio.create_task(self._delayed_release(slot, cooldown_seconds))
        else:
            # Immediate release - put slot back in queue
            # Check if it was disabled/exhausted while busy
            if slot.is_disabled or slot.is_exhausted:
                logger.info(f"Account '{slot.name}' was disabled/exhausted while busy, not returning to pool")
                return

            await self._idle_offer(slot)
            logger.info(
                f"Account released: {slot.name} "
                f"(available={self.available_count})"
            )
            # Broadcast status update after release
            await self._broadcast_status_update()

    async def _delayed_release(self, slot: AccountSlot, delay: float) -> None:
        """
        Re-insert a slot into the queue after a cooldown delay.

        Args:
            slot: The AccountSlot to re-insert
            delay: Seconds to wait before re-inserting
        """
        try:
            await asyncio.sleep(delay)
            slot.cooldown_until = 0.0  # Clear cooldown
            
            # Check if it was disabled/exhausted while cooling
            if slot.is_disabled or slot.is_exhausted:
                logger.info(f"Account '{slot.name}' was disabled/exhausted while cooling, not returning to pool")
                return

            await self._idle_offer(slot)
            logger.info(
                f"Account '{slot.name}' cooldown expired, back in pool "
                f"(available={self.available_count})"
            )
            # Broadcast status update after cooldown expires
            await self._broadcast_status_update()
        except asyncio.CancelledError:
            # If task is cancelled (e.g., server shutdown), put slot back immediately
            slot.cooldown_until = 0.0
            if not slot.is_disabled and not slot.is_exhausted:
                await self._idle_offer(slot)
            logger.debug(f"Cooldown cancelled for '{slot.name}', slot returned to pool")

    def get_status(self) -> dict:
        """
        Get pool status for health check / monitoring.

        Returns:
            Dictionary with pool status information:
            - total_accounts: Total number of accounts
            - available: Number of idle accounts
            - busy: Number of active accounts
            - cooling_down: Number of accounts in cooldown
            - exhausted: Number of accounts out of quota
            - disabled: Number of manually disabled accounts
            - accounts: Per-account status details with quota info
        """
        accounts_status = []
        cooling_count = 0
        exhausted_count = 0
        disabled_count = 0
        for slot in self._slots:
            is_cooling = slot.is_cooling_down
            if is_cooling:
                cooling_count += 1
            if slot.is_exhausted:
                exhausted_count += 1
            if slot.is_disabled:
                disabled_count += 1

            account_info: dict = {
                "name": slot.name,
                "email": slot.email or None,
                "active_requests": slot.active_requests,
                "total_requests": slot.total_requests,
                "is_exhausted": slot.is_exhausted,
                "is_disabled": slot.is_disabled,
                "is_active_on_host": slot.is_active_on_host,
                "proxy_url": mask_proxy_url(slot.proxy_url),
            }
            # Quota details
            if slot.quota_info:
                account_info["quota"] = {
                    "used": slot.quota_info.total_used,
                    "limit": slot.quota_info.total_limit,
                    "remaining": slot.quota_info.remaining,
                    "plan": slot.quota_info.subscription_plan,
                    "next_reset": slot.quota_info.next_reset,
                    "trial_expiry": slot.quota_info.trial_expiry,
                    "overage_enabled": getattr(slot.quota_info, "overage_enabled", False),
                    "last_checked_age_seconds": round(slot.quota_info.age_seconds()),
                }
            if is_cooling:
                account_info["cooldown_remaining_seconds"] = round(slot.cooldown_remaining)

            # Files available for download
            account_files = []
            if slot.auth_manager and slot.auth_manager.creds_dir:
                try:
                    d = slot.auth_manager.creds_dir
                    if d.exists():
                        for f in d.glob("*.json"):
                            if f.is_file():
                                account_files.append(f.name)
                except Exception as e:
                    logger.debug(f"Failed to list files for account {slot.name}: {e}")
            account_info["files"] = sorted(account_files)

            accounts_status.append(account_info)

        # Calculate total remaining quota (only for enabled accounts with quota info)
        total_remaining = 0.0
        for slot in self._slots:
            if not slot.is_disabled and slot.quota_info:
                total_remaining += slot.quota_info.remaining

        accounts_status.sort(key=lambda account: _account_name_sort_key(account.get("name", "")))

        return {
            "total_accounts": self.size,
            "available": self.available_count,
            "busy": self.busy_count,
            "cooling_down": cooling_count,
            "exhausted": exhausted_count,
            "disabled": disabled_count,
            "total_remaining_quota": round(total_remaining, 1),
            "accounts": accounts_status,
        }

    async def _broadcast_status_update(self) -> None:
        """
        Broadcast current pool status to all SSE clients via LogBroadcaster.

        Uses Throttle + Trailing strategy to limit broadcast frequency:
        - Immediate broadcast if enough time has passed since last broadcast
        - Otherwise, schedule a delayed broadcast to ensure final state is sent
        - Interval is auto-adjusted based on pool size:
          * 1-20 accounts: 1 second interval
          * 21+ accounts: 2 second interval

        This prevents overwhelming SSE clients during high-concurrency scenarios
        while ensuring the dashboard always reflects the final accurate state.
        """
        if self._broadcaster is None:
            return

        # Auto-adjust interval based on pool size
        broadcast_interval = 2.0 if self.size > 20 else 1.0

        now = time.monotonic()
        time_since_last = now - self._last_broadcast_time

        if time_since_last >= broadcast_interval:
            # Enough time has passed, broadcast immediately
            await self._do_broadcast()
            self._pending_broadcast = False
            if self._broadcast_timer:
                self._broadcast_timer.cancel()
                self._broadcast_timer = None
        else:
            # Too soon, mark as pending
            self._pending_broadcast = True

            # Schedule delayed broadcast if not already scheduled
            if not self._broadcast_timer or self._broadcast_timer.done():
                delay = broadcast_interval - time_since_last
                self._broadcast_timer = asyncio.create_task(
                    self._delayed_broadcast(delay)
                )

    async def _delayed_broadcast(self, delay: float) -> None:
        """
        Execute a delayed status broadcast after the specified delay.

        Args:
            delay: Seconds to wait before broadcasting
        """
        try:
            await asyncio.sleep(delay)
            if self._pending_broadcast:
                await self._do_broadcast()
                self._pending_broadcast = False
        except asyncio.CancelledError:
            pass
        finally:
            self._broadcast_timer = None

    async def _do_broadcast(self) -> None:
        """
        Perform the actual status broadcast.

        Updates last broadcast time and sends status to all SSE clients.
        """
        self._last_broadcast_time = time.monotonic()
        try:
            status = self.get_status()
            self._broadcaster.broadcast_status(status)
        except Exception as e:
            logger.debug(f"Failed to broadcast status update: {e}")

    # ------------------------------------------------------------------
    # Dynamic slot management (Admin UI)
    # ------------------------------------------------------------------

    def get_slot_by_name(self, name: str) -> Optional[AccountSlot]:
        """
        Find an account slot by its name.

        Args:
            name: Account directory name (e.g. "account-1")

        Returns:
            AccountSlot if found, None otherwise
        """
        for slot in self._slots:
            if slot.name == name:
                return slot
        return None

    async def add_slot(self, slot: AccountSlot) -> None:
        """
        Hot-add a new account slot to the pool.

        The slot is immediately available for incoming requests.
        Does nothing if a slot with the same name already exists.

        Args:
            slot: The AccountSlot to add
        """
        existing = self.get_slot_by_name(slot.name)
        if existing is not None:
            logger.warning(f"Slot '{slot.name}' already exists in pool, skipping add")
            return

        self._slots.append(slot)
        if not slot.is_exhausted and not slot.is_disabled and not slot._in_queue:
            await self._idle_offer(slot)
        logger.info(
            f"Account slot added: '{slot.name}' "
            f"(pool size: {self.size}, available: {self.available_count})"
        )
        # Broadcast status update after adding slot
        await self._broadcast_status_update()

    async def remove_slot(self, name: str) -> bool:
        """
        Remove an account slot from the pool.

        The slot is marked as exhausted so the queue will naturally
        skip it on next dequeue. The slot is removed from self._slots.

        Safe to call while the slot may be listed in the idle deque —
        marking is_exhausted ensures the acquire() loop skips it.

        Args:
            name: Account directory name to remove

        Returns:
            True if removed, False if slot not found or still active
        """
        slot = self.get_slot_by_name(name)
        if slot is None:
            logger.warning(f"Cannot remove slot '{name}': not found")
            return False

        if slot.active_requests > 0:
            logger.warning(
                f"Cannot remove slot '{name}': "
                f"{slot.active_requests} active request(s) in progress"
            )
            return False

        await self._idle_remove_slot_if_present(slot)

        async with self._session_bindings_lock:
            stale_keys = [k for k, v in self._session_bindings.items() if v == name]
            for k in stale_keys:
                self._session_bindings.pop(k, None)
                self._session_binding_mono.pop(k, None)

        # Mark exhausted so acquire() skips it when it comes off the queue
        slot.is_exhausted = True
        slot.is_disabled = True
        self._slots.remove(slot)
        logger.info(f"Account slot removed: '{name}' (pool size: {self.size})")
        # Broadcast status update after removing slot
        await self._broadcast_status_update()
        return True

    def switch_to_account(self, name: str) -> bool:
        """
        Copy account credentials to the host cache directory.
        
        This allows 'switching' the active account on the machine where
        Kiro IDE/CLI is running, provided the directory mapping is set up correctly.
        
        Args:
            name: Account directory name to switch to
            
        Returns:
            True if switch successful, False otherwise
        """
        if not KIRO_HOST_CACHE_DIR:
            logger.warning("KIRO_HOST_CACHE_DIR not configured, cannot switch account")
            return False

        slot = self.get_slot_by_name(name)
        if not slot:
            logger.error(f"Cannot switch to unknown account: {name}")
            return False

        creds_dir = slot.auth_manager.creds_dir
        if not creds_dir or not creds_dir.exists():
            logger.error(f"Credentials directory not found for account: {name}")
            return False

        host_dir = Path(KIRO_HOST_CACHE_DIR)
        if not host_dir.exists():
            try:
                host_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.error(f"Failed to create host cache dir {KIRO_HOST_CACHE_DIR}: {e}")
                return False

        try:
            # Copy all .json files from the account directory to host cache
            copied_count = 0
            for f in creds_dir.glob("*.json"):
                if f.is_file():
                    target = host_dir / f.name
                    shutil.copy2(str(f), str(target))
                    copied_count += 1
            
            if copied_count > 0:
                logger.info(f"Successfully switched host account to '{name}' ({copied_count} files copied)")
                return True
            else:
                logger.warning(f"No credentials files found to copy for account: {name}")
                return False
                
        except Exception as e:
            logger.error(f"Failed to switch host account to '{name}': {e}")
            return False

    async def disable_slot(self, name: str) -> bool:
        """
        Disable an account slot so it is skipped by acquire().

        The slot stays in self._slots but will not be put back in
        the queue after being dequeued (acquire() discards disabled slots).
        Active requests are not interrupted.

        Args:
            name: Account directory name to disable

        Returns:
            True if disabled, False if slot not found or already disabled
        """
        slot = self.get_slot_by_name(name)
        if slot is None:
            logger.warning(f"Cannot disable slot '{name}': not found")
            return False

        if slot.is_disabled:
            logger.debug(f"Slot '{name}' is already disabled")
            return False

        slot.is_disabled = True
        async with self._session_bindings_lock:
            stale_keys = [k for k, v in self._session_bindings.items() if v == name]
            for k in stale_keys:
                self._session_bindings.pop(k, None)
                self._session_binding_mono.pop(k, None)
        await self._idle_remove_slot_if_present(slot)
        logger.info(f"Account slot disabled: '{name}'")
        # Broadcast status update after disabling slot
        await self._broadcast_status_update()
        return True

    async def enable_slot(self, name: str) -> bool:
        """
        Re-enable a previously disabled account slot.

        The slot is re-inserted into the queue so it can serve requests.

        Args:
            name: Account directory name to enable

        Returns:
            True if enabled, False if slot not found or not disabled
        """
        slot = self.get_slot_by_name(name)
        if slot is None:
            logger.warning(f"Cannot enable slot '{name}': not found")
            return False

        if not slot.is_disabled:
            logger.debug(f"Slot '{name}' is not disabled")
            return False

        slot.is_disabled = False
        # Only re-queue if not also exhausted or actively cooling or already in queue
        if not slot.is_exhausted and not slot.is_cooling_down and not slot._in_queue:
            await self._idle_offer(slot)
        logger.info(
            f"Account slot enabled: '{name}' "
            f"(available: {self.available_count})"
        )
        # Broadcast status update after enabling slot
        await self._broadcast_status_update()
        return True

    async def clear_cooldown(self, name: str) -> bool:
        """
        Immediately clear the cooldown for an account slot.

        The slot is re-inserted into the queue so it can accept
        requests immediately, bypassing the scheduled cooldown delay.

        Args:
            name: Account directory name

        Returns:
            True if cooldown was cleared, False if slot not found or not cooling
        """
        slot = self.get_slot_by_name(name)
        if slot is None:
            logger.warning(f"Cannot clear cooldown for '{name}': slot not found")
            return False

        if not slot.is_cooling_down:
            logger.debug(f"Slot '{name}' is not in cooldown")
            return False

        slot.cooldown_until = 0.0
        if not slot.is_exhausted and not slot.is_disabled and not slot._in_queue:
            await self._idle_offer(slot)
        logger.info(
            f"Cooldown cleared for '{name}' — slot immediately available "
            f"(available: {self.available_count})"
        )
        # Broadcast status update after clearing cooldown
        await self._broadcast_status_update()
        return True

    @classmethod
    def from_single(cls, auth_manager: KiroAuthManager, name: str = "default") -> "AccountPool":
        """
        Create an AccountPool from a single KiroAuthManager.

        This provides backward compatibility with the existing single-account
        configuration. The pool will have concurrency=1.

        Args:
            auth_manager: Existing KiroAuthManager instance
            name: Human-readable name for the account slot

        Returns:
            AccountPool with a single slot
        """
        slot = AccountSlot(name=name, auth_manager=auth_manager)
        logger.info(f"Creating single-account pool: {name}")
        return cls([slot])

    @classmethod
    def from_directory(
        cls,
        directory: str,
        profile_arn: str = "",
        region: str = "us-east-1",
    ) -> "AccountPool":
        """
        Create an AccountPool from a multi-account credentials directory.

        Scans the directory for subdirectories containing Kiro credential files.
        Each subdirectory should contain:
            kiro-auth-token.json      (required)
            {clientIdHash}.json       (optional, for Enterprise Kiro IDE)

        Args:
            directory: Path to the multi-account credentials directory
            profile_arn: AWS CodeWhisperer profile ARN (applied to all accounts)
            region: AWS region (applied to all accounts, may be overridden by creds)

        Returns:
            AccountPool with one slot per valid account directory

        Raises:
            ValueError: If no valid account directories found
        """
        dir_path = Path(directory).expanduser().resolve()

        if not dir_path.exists():
            raise ValueError(
                f"Multi-account credentials directory not found: {directory}\n"
                f"Resolved path: {dir_path}\n"
                f"Please create the directory and add account subdirectories."
            )

        if not dir_path.is_dir():
            raise ValueError(
                f"KIRO_MULTI_CREDS_DIR is not a directory: {directory}"
            )

        slots: List[AccountSlot] = []
        errors: List[str] = []

        # Scan subdirectories (sorted for deterministic order)
        subdirs = sorted(
            [d for d in dir_path.iterdir() if d.is_dir()],
            key=lambda d: _account_name_sort_key(d.name),
        )

        if not subdirs:
            logger.warning(
                f"No account subdirectories found in: {directory}\n"
                f"Starting with empty account pool. Add accounts via Admin UI.\n"
                f"Expected structure:\n"
                f"  {directory}/\n"
                f"    account-1/\n"
                f"      kiro-auth-token.json\n"
                f"    account-2/\n"
                f"      kiro-auth-token.json"
            )
            # Return empty pool instead of raising error
            return cls([])

        for subdir in subdirs:
            account_name = subdir.name
            creds_file = subdir / "kiro-auth-token.json"

            if not creds_file.exists():
                logger.warning(
                    f"Skipping directory '{account_name}': "
                    f"missing kiro-auth-token.json"
                )
                errors.append(
                    f"  {account_name}/: missing kiro-auth-token.json"
                )
                continue

            # Validate JSON file is readable
            try:
                with open(creds_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(
                    f"Skipping directory '{account_name}': "
                    f"invalid credentials file: {e}"
                )
                errors.append(
                    f"  {account_name}/: invalid JSON in kiro-auth-token.json: {e}"
                )
                continue

            # Check required fields
            if "refreshToken" not in data and "accessToken" not in data:
                logger.warning(
                    f"Skipping directory '{account_name}': "
                    f"no refreshToken or accessToken in credentials"
                )
                errors.append(
                    f"  {account_name}/: no refreshToken or accessToken found"
                )
                continue

            # Handle Enterprise Kiro IDE device registration
            # Override the default path to read from the account's sso/ directory
            client_id_override = None
            client_secret_override = None

            if "clientIdHash" in data:
                device_reg_file = subdir / f"{data['clientIdHash']}.json"
                if device_reg_file.exists():
                    try:
                        with open(device_reg_file, "r", encoding="utf-8") as f:
                            device_data = json.load(f)
                        client_id_override = device_data.get("clientId")
                        client_secret_override = device_data.get("clientSecret")
                        logger.debug(
                            f"Account '{account_name}': loaded device registration "
                            f"from {device_reg_file.name}"
                        )
                    except (json.JSONDecodeError, OSError) as e:
                        logger.warning(
                            f"Account '{account_name}': "
                            f"failed to load device registration: {e}"
                        )
                else:
                    logger.debug(
                        f"Account '{account_name}': "
                        f"device registration file not found: {device_reg_file.name}"
                    )

            proxy_u = load_proxy_url_from_account_dir(subdir)
            # Create KiroAuthManager for this account
            # We pass the creds_file path so it loads refreshToken, accessToken, etc.
            auth_manager = KiroAuthManager(
                profile_arn=profile_arn if profile_arn else None,
                region=region,
                creds_file=str(creds_file),
                client_id=client_id_override,
                client_secret=client_secret_override,
                http_proxy_url=proxy_u,
            )

            slot = AccountSlot(name=account_name, auth_manager=auth_manager, proxy_url=proxy_u)
            slots.append(slot)
            logger.info(
                f"Account loaded: {account_name} "
                f"(auth_type={auth_manager.auth_type.value})"
            )

        if not slots:
            error_details = "\n".join(errors) if errors else "No subdirectories found"
            logger.warning(
                f"No valid accounts found in: {directory}\n"
                f"Errors:\n{error_details}\n"
                f"\nStarting with empty account pool. Add accounts via Admin UI.\n"
                f"Expected structure:\n"
                f"  {directory}/\n"
                f"    account-1/\n"
                f"      kiro-auth-token.json\n"
                f"      {{clientIdHash}}.json  (optional)"
            )
            # Return empty pool instead of raising error
            return cls([])

        logger.info(
            f"Multi-account pool ready: {len(slots)} account(s) loaded "
            f"from {directory}"
        )
        if errors:
            logger.warning(
                f"Skipped {len(errors)} invalid account(s):\n"
                + "\n".join(errors)
            )

        return cls(slots)
