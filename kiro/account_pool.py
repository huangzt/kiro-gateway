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
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from loguru import logger

from kiro.auth import KiroAuthManager
from kiro.quota_checker import QuotaInfo, check_quota, QuotaCheckError


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
        quota_info: Latest quota info from Kiro Web Portal API
    """

    name: str
    auth_manager: KiroAuthManager
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))
    active_requests: int = 0
    total_requests: int = 0
    cooldown_until: float = 0.0
    is_exhausted: bool = False
    quota_info: Optional[QuotaInfo] = None

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
        quota_str = f", quota={self.quota_summary}" if self.quota_info else ""
        return (
            f"AccountSlot(name={self.name!r}, "
            f"active={self.active_requests}, "
            f"total={self.total_requests}{quota_str}{cooldown_str}{exhausted_str})"
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
            slots: List of AccountSlot instances
            quota_check_interval: Seconds between quota checks per account.
                                  Default is 300 (5 minutes). Set to 0 to disable.

        Raises:
            ValueError: If slots list is empty
        """
        if not slots:
            raise ValueError("AccountPool requires at least one account slot")

        self._slots = slots
        self._queue: asyncio.Queue[AccountSlot] = asyncio.Queue()
        self._quota_check_interval = quota_check_interval

        # Pre-populate queue with all available slots
        for slot in self._slots:
            self._queue.put_nowait(slot)

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
        """Number of currently available (idle) account slots."""
        return self._queue.qsize()

    @property
    def busy_count(self) -> int:
        """Number of currently busy account slots."""
        # Busy = Total - Available - CoolingDown - Exhausted
        # But qsize() only includes idle slots.
        # So Busy = slots that have active_requests > 0
        return sum(1 for slot in self._slots if slot.active_requests > 0)

    @property
    def exhausted_count(self) -> int:
        """Number of accounts that have reached their quota."""
        return sum(1 for slot in self._slots if slot.is_exhausted)

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
            f"queue_waiting={self._queue.qsize()})"
        )

        deadline = time.monotonic() + timeout

        while True:
            remaining_timeout = deadline - time.monotonic()
            if remaining_timeout <= 0:
                logger.warning(
                    f"Queue timeout after {timeout}s - all {self.size} account(s) busy or exhausted"
                )
                raise asyncio.TimeoutError()

            try:
                slot = await asyncio.wait_for(
                    self._queue.get(), timeout=remaining_timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"Queue timeout after {timeout}s - all {self.size} account(s) busy or exhausted"
                )
                raise

            # Check quota if interval has elapsed
            if self._quota_check_interval > 0:
                await self._maybe_refresh_quota(slot)

            # If slot became exhausted after quota check, skip it
            if slot.is_exhausted:
                logger.warning(
                    f"Skipping exhausted account '{slot.name}' "
                    f"({slot.email}, quota: {slot.quota_summary})"
                )
                # Don't put it back - it's permanently out
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

            return slot

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
            quota = await check_quota(access_token, provider)
            slot.quota_info = quota

            if quota.is_exhausted:
                slot.is_exhausted = True
                logger.error(
                    f"Account '{slot.name}' ({quota.email}) is EXHAUSTED "
                    f"(quota: {quota.usage_summary}, plan: {quota.subscription_plan}). "
                    f"Removed from active pool."
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

    async def initialize_quota(self) -> None:
        """
        Query quota for all accounts during startup.

        This is called once when the server starts to get initial quota info.
        Accounts that are found to be exhausted are removed from the pool.
        Failures are logged but don't prevent the server from starting.
        """
        logger.info("Checking quota for all accounts...")

        # Check all slots concurrently
        tasks = [self._check_slot_quota(slot) for slot in self._slots]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Remove exhausted slots from queue
        # Since we can't remove from asyncio.Queue, we rebuild it
        exhausted_names = [s.name for s in self._slots if s.is_exhausted]
        if exhausted_names:
            # Drain the queue and re-add only non-exhausted slots
            remaining_slots = []
            while not self._queue.empty():
                try:
                    s = self._queue.get_nowait()
                    if not s.is_exhausted:
                        remaining_slots.append(s)
                except asyncio.QueueEmpty:
                    break
            for s in remaining_slots:
                self._queue.put_nowait(s)

            logger.warning(
                f"Removed {len(exhausted_names)} exhausted account(s) from pool: "
                f"{', '.join(exhausted_names)}"
            )

        active_count = sum(1 for s in self._slots if not s.is_exhausted)
        logger.info(
            f"Quota initialization complete: "
            f"{active_count}/{len(self._slots)} account(s) active"
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
            await self._queue.put(slot)
            logger.info(
                f"Account released: {slot.name} "
                f"(available={self.available_count})"
            )

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
            await self._queue.put(slot)
            logger.info(
                f"Account '{slot.name}' cooldown expired, back in pool "
                f"(available={self.available_count})"
            )
        except asyncio.CancelledError:
            # If task is cancelled (e.g., server shutdown), put slot back immediately
            slot.cooldown_until = 0.0
            await self._queue.put(slot)
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
            - accounts: Per-account status details with quota info
        """
        accounts_status = []
        cooling_count = 0
        exhausted_count = 0
        for slot in self._slots:
            is_cooling = slot.is_cooling_down
            if is_cooling:
                cooling_count += 1
            if slot.is_exhausted:
                exhausted_count += 1

            account_info: dict = {
                "name": slot.name,
                "email": slot.email or None,
                "active_requests": slot.active_requests,
                "total_requests": slot.total_requests,
                "is_exhausted": slot.is_exhausted,
            }
            # Quota details
            if slot.quota_info:
                account_info["quota"] = {
                    "used": slot.quota_info.total_used,
                    "limit": slot.quota_info.total_limit,
                    "remaining": slot.quota_info.remaining,
                    "plan": slot.quota_info.subscription_plan,
                    "next_reset": slot.quota_info.next_reset,
                    "last_checked_age_seconds": round(slot.quota_info.age_seconds()),
                }
            if is_cooling:
                account_info["cooldown_remaining_seconds"] = round(slot.cooldown_remaining)
            accounts_status.append(account_info)

        return {
            "total_accounts": self.size,
            "available": self.available_count,
            "busy": self.busy_count,
            "cooling_down": cooling_count,
            "exhausted": exhausted_count,
            "accounts": accounts_status,
        }

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
        subdirs = sorted([d for d in dir_path.iterdir() if d.is_dir()])

        if not subdirs:
            raise ValueError(
                f"No account subdirectories found in: {directory}\n"
                f"Expected structure:\n"
                f"  {directory}/\n"
                f"    account-1/\n"
                f"      kiro-auth-token.json\n"
                f"    account-2/\n"
                f"      kiro-auth-token.json"
            )

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

            # Create KiroAuthManager for this account
            # We pass the creds_file path so it loads refreshToken, accessToken, etc.
            auth_manager = KiroAuthManager(
                profile_arn=profile_arn if profile_arn else None,
                region=region,
                creds_file=str(creds_file),
                client_id=client_id_override,
                client_secret=client_secret_override,
            )

            slot = AccountSlot(name=account_name, auth_manager=auth_manager)
            slots.append(slot)
            logger.info(
                f"Account loaded: {account_name} "
                f"(auth_type={auth_manager.auth_type.value})"
            )

        if not slots:
            error_details = "\n".join(errors) if errors else "No subdirectories found"
            raise ValueError(
                f"No valid accounts found in: {directory}\n"
                f"Errors:\n{error_details}\n"
                f"\nExpected structure:\n"
                f"  {directory}/\n"
                f"    account-1/\n"
                f"      kiro-auth-token.json\n"
                f"      {{clientIdHash}}.json  (optional)"
            )

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
