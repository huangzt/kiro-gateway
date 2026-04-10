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
Log broadcaster for SSE streaming with admin log filtering.

Maintains a ring buffer of recent log entries and distributes them
to connected SSE clients. Registered as a loguru sink at startup.

Architecture:
    - LogBroadcaster acts as a loguru sink (write method)
    - Each log entry is stored in a deque ring buffer
    - Admin route logs are filtered from SSE streams (but kept in history)
    - SSE clients subscribe() to get (queue, history_snapshot)
    - New entries are fan-out pushed to all subscriber queues
    - Clients unsubscribe() when they disconnect

Admin Log Filtering:
    The following logs are filtered from SSE streams to reduce noise:
    - Admin route access logs (GET /admin/status, POST /admin/accounts, etc.)
    - Admin UI static file requests (/admin-static/*, admin.html)
    - Admin module INFO/DEBUG logs (kiro.routes_admin)
    - Successful admin authentication checks

    The following logs are NOT filtered (important for monitoring):
    - ERROR and WARNING level logs from admin routes
    - Authentication failures (security relevant)
    - All non-admin API logs (v1/models, v1/chat/completions, etc.)

    Note: Filtered logs are still stored in the full history buffer
    for debugging and audit purposes.

Usage:
    broadcaster = LogBroadcaster(history_size=200)
    logger.add(broadcaster.write, format="{message}")

    # In SSE endpoint
    queue, history = await broadcaster.subscribe()
    try:
        for entry in history:
            yield f"data: {json.dumps(entry)}\\n\\n"
        while True:
            entry = await asyncio.wait_for(queue.get(), timeout=30)
            yield f"data: {json.dumps(entry)}\\n\\n"
    finally:
        await broadcaster.unsubscribe(queue)
"""

import asyncio
from collections import deque
from typing import Any, Dict, List, Optional, Set, Tuple

from loguru import logger


class LogBroadcaster:
    """
    Broadcasts log entries to SSE clients with a ring buffer history.

    Acts as a loguru sink, collecting structured log entries into a
    fixed-size ring buffer. New SSE clients receive the history snapshot
    followed by a real-time stream of future entries.

    Attributes:
        history_size: Current maximum history buffer size
        subscriber_count: Number of active SSE connections
    """

    def __init__(self, history_size: int = 200) -> None:
        """
        Initialize LogBroadcaster.

        Args:
            history_size: Maximum number of log entries to keep in buffer.
                          Must be >= 1.

        Raises:
            ValueError: If history_size < 1
        """
        if history_size < 1:
            raise ValueError(f"history_size must be >= 1, got {history_size}")

        self._history: deque = deque(maxlen=history_size)
        self._queues: Set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Loguru sink interface
    # ------------------------------------------------------------------

    def write(self, message: Any) -> None:
        """
        Loguru sink callback. Called for each log entry.

        Adds the entry to the ring buffer and fans out to all
        active subscriber queues. Non-blocking; full queues are discarded.

        Args:
            message: Loguru message object (has .record attribute)
        """
        record = message.record
        entry = {
            "time": record["time"].strftime("%Y-%m-%d %H:%M:%S"),
            "level": record["level"].name,
            "message": record["message"],
            "module": record["name"],
            "function": record["function"],
            "line": record["line"],
        }

        # Filter out admin route logs from frontend streaming
        # Admin logs are still stored in history but not pushed to SSE clients
        should_broadcast = not self._is_admin_log(entry)

        # Add to ring buffer (always store, regardless of broadcast filter)
        self._history.append(entry)

        # Fan-out broadcast to all subscriber queues (non-blocking)
        # Skip broadcasting for admin logs
        if should_broadcast:
            dead_queues: Set[asyncio.Queue] = set()
            for q in self._queues:
                try:
                    q.put_nowait(entry)
                except (asyncio.QueueFull, Exception):
                    dead_queues.add(q)

            # Prune dead/full queues
            if dead_queues:
                self._queues -= dead_queues

    # ------------------------------------------------------------------
    # Admin log filtering
    # ------------------------------------------------------------------

    def _is_admin_log(self, entry: Dict) -> bool:
        """
        Check if a log entry is related to admin routes and should be filtered.

        Filters out logs from:
        - Admin route handlers (GET /admin/status, POST /admin/accounts, etc.)
        - Admin UI static file serving
        - Admin authentication checks

        Args:
            entry: Log entry dictionary with message, module, function, etc.

        Returns:
            True if this is an admin log that should be filtered from SSE stream
        """
        message = entry.get("message", "").lower()
        module = entry.get("module", "")
        function = entry.get("function", "")

        # Filter admin route access logs (uvicorn format and direct paths)
        admin_patterns = [
            "get /admin/status",
            "post /admin/accounts",
            "delete /admin/accounts",
            "get /admin/config",
            "patch /admin/config",
            "get /admin/logs/stream",
            "/admin/accounts/",  # Covers all account-specific endpoints
            "quota-refresh",
            "cooldown-clear",
            "disable",
            "enable",
            "switch",
            '"get /admin',        # Uvicorn format: "GET /admin HTTP/1.1"
            '"post /admin',       # Uvicorn format: "POST /admin/... HTTP/1.1"
            '"delete /admin',     # Uvicorn format: "DELETE /admin/... HTTP/1.1"
            '"patch /admin',      # Uvicorn format: "PATCH /admin/... HTTP/1.1"
        ]

        # Check message content for admin route patterns
        for pattern in admin_patterns:
            if pattern in message:
                return True

        # Filter logs from admin route module
        if module == "kiro.routes_admin":
            # Allow error logs from admin routes to pass through
            # Only filter INFO/DEBUG level routine operations
            level = entry.get("level", "INFO")
            if level in ["ERROR", "WARNING"]:
                return False
            return True

        # Filter admin static file serving logs
        if "/admin-static/" in message or "admin.html" in message:
            return True

        # Filter admin authentication logs (but allow auth failures)
        if function == "verify_admin_key" and "invalid" not in message.lower():
            return True

        return False

    # ------------------------------------------------------------------
    # SSE subscription API
    # ------------------------------------------------------------------

    async def subscribe(self) -> Tuple[asyncio.Queue, List[Dict]]:
        """
        Subscribe to the real-time log stream.

        Returns a dedicated queue for new log entries plus a snapshot
        of the current history buffer with admin logs filtered out.
        The caller should iterate the history first, then drain the queue.

        Returns:
            Tuple of:
                - asyncio.Queue: Receives new log entries (Dict) - admin logs filtered
                - List[Dict]: Snapshot of current history (chronological) - admin logs filtered
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        # Filter admin logs from history snapshot
        history = [entry for entry in self._history if not self._is_admin_log(entry)]
        async with self._lock:
            self._queues.add(q)
        return q, history

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        """
        Unsubscribe from the log stream and clean up resources.

        Args:
            queue: The queue previously returned by subscribe()
        """
        async with self._lock:
            self._queues.discard(queue)

    # ------------------------------------------------------------------
    # Dynamic resize
    # ------------------------------------------------------------------

    def resize(self, new_size: int) -> None:
        """
        Resize the ring buffer, preserving the most recent entries.

        Args:
            new_size: New maximum buffer size (must be >= 1)

        Raises:
            ValueError: If new_size < 1
        """
        if new_size < 1:
            raise ValueError(f"new_size must be >= 1, got {new_size}")

        old_entries = list(self._history)
        keep = old_entries[-new_size:] if len(old_entries) > new_size else old_entries
        self._history = deque(keep, maxlen=new_size)
        logger.debug(f"Log history buffer resized: {len(old_entries)} → {len(keep)} entries kept, maxlen={new_size}")

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def history_size(self) -> int:
        """Current maximum history buffer size."""
        return self._history.maxlen or 0

    @property
    def subscriber_count(self) -> int:
        """Number of currently active SSE connections."""
        return len(self._queues)

    def get_history(self) -> List[Dict]:
        """
        Return a snapshot of the current history buffer.

        Returns:
            List of log entry dicts in chronological order
        """
        return list(self._history)


# ------------------------------------------------------------------
# Module-level singleton (populated by main.py at startup)
# ------------------------------------------------------------------

log_broadcaster: Optional[LogBroadcaster] = None
