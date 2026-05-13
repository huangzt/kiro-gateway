# -*- coding: utf-8 -*-

"""
Unit tests for AccountPool - Multi-account queue-based request management.

Tests cover:
- AccountSlot creation and representation
- AccountPool initialization and validation
- Single-account pool (backward compatibility)
- Multi-account directory loading
- Queue acquire/release mechanics
- FIFO fair scheduling
- Timeout handling
- Pool status monitoring
- Edge cases and error handling
"""

import asyncio
import json
import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from kiro.account_pool import AccountSlot, AccountPool
from kiro.auth import KiroAuthManager
from kiro.quota_checker import QuotaInfo


# =============================================================================
# Helper Fixtures
# =============================================================================

@pytest.fixture
def mock_auth_manager_factory():
    """Factory for creating mock KiroAuthManager instances."""
    def _create(name: str = "test"):
        manager = MagicMock(spec=KiroAuthManager)
        manager.auth_type = MagicMock()
        manager.auth_type.value = "kiro_desktop"
        manager.profile_arn = "arn:aws:test"
        manager.fingerprint = f"fingerprint-{name}"
        manager.q_host = "https://q.example.com"
        manager.api_host = "https://api.example.com"
        manager.get_access_token = AsyncMock(return_value=f"token-{name}")
        return manager
    return _create


@pytest.fixture
def single_slot(mock_auth_manager_factory):
    """Creates a single AccountSlot for testing."""
    return AccountSlot(name="default", auth_manager=mock_auth_manager_factory("default"))


@pytest.fixture
def multi_slots(mock_auth_manager_factory):
    """Creates multiple AccountSlots for testing."""
    return [
        AccountSlot(name=f"account-{i}", auth_manager=mock_auth_manager_factory(f"account-{i}"))
        for i in range(3)
    ]


@pytest.fixture
def single_pool(single_slot):
    """Creates an AccountPool with one slot."""
    return AccountPool([single_slot])


@pytest.fixture
def multi_pool(multi_slots):
    """Creates an AccountPool with multiple slots."""
    return AccountPool(multi_slots)


@pytest.fixture
def temp_multi_creds_dir(tmp_path):
    """
    Creates a temporary multi-account credentials directory structure.

    Structure:
        tmp_path/
            account-1/
                kiro-auth-token.json
            account-2/
                kiro-auth-token.json
                abcdef123456.json  (device registration)
    """
    # Account 1 - simple (no device registration)
    acct1_dir = tmp_path / "account-1"
    acct1_dir.mkdir(parents=True)
    acct1_creds = {
        "accessToken": "token_acct1",
        "refreshToken": "refresh_acct1",
        "expiresAt": "2099-01-01T00:00:00.000Z",
        "region": "us-east-1",
    }
    (acct1_dir / "kiro-auth-token.json").write_text(json.dumps(acct1_creds))

    # Account 2 - with device registration (Enterprise Kiro IDE)
    acct2_dir = tmp_path / "account-2"
    acct2_dir.mkdir(parents=True)
    acct2_creds = {
        "accessToken": "token_acct2",
        "refreshToken": "refresh_acct2",
        "expiresAt": "2099-01-01T00:00:00.000Z",
        "region": "us-east-1",
        "clientIdHash": "abcdef123456",
    }
    (acct2_dir / "kiro-auth-token.json").write_text(json.dumps(acct2_creds))

    # Device registration file for account 2
    device_reg = {
        "clientId": "device_client_id",
        "clientSecret": "device_client_secret",
    }
    (acct2_dir / "abcdef123456.json").write_text(json.dumps(device_reg))

    return tmp_path


# =============================================================================
# AccountSlot Tests
# =============================================================================

class TestAccountSlotCreation:
    """Tests for AccountSlot creation and attributes."""

    def test_slot_creation_with_defaults(self, mock_auth_manager_factory):
        """Test that AccountSlot initializes with correct defaults."""
        manager = mock_auth_manager_factory("test")
        slot = AccountSlot(name="test-account", auth_manager=manager)

        assert slot.name == "test-account"
        assert slot.auth_manager is manager
        assert slot.active_requests == 0
        assert slot.total_requests == 0
        assert isinstance(slot.semaphore, asyncio.Semaphore)

    def test_slot_repr(self, mock_auth_manager_factory):
        """Test AccountSlot string representation."""
        manager = mock_auth_manager_factory("test")
        slot = AccountSlot(name="my-account", auth_manager=manager)
        slot.active_requests = 1
        slot.total_requests = 42

        repr_str = repr(slot)
        assert "my-account" in repr_str
        assert "active=1" in repr_str
        assert "total=42" in repr_str

    def test_slot_semaphore_is_unique_per_instance(self, mock_auth_manager_factory):
        """Test that each slot gets its own semaphore."""
        manager = mock_auth_manager_factory("test")
        slot1 = AccountSlot(name="a", auth_manager=manager)
        slot2 = AccountSlot(name="b", auth_manager=manager)

        assert slot1.semaphore is not slot2.semaphore


# =============================================================================
# AccountPool Initialization Tests
# =============================================================================

class TestAccountPoolInitialization:
    """Tests for AccountPool construction and validation."""

    def test_pool_creation_with_single_slot(self, single_slot):
        """Test pool creation with one slot."""
        pool = AccountPool([single_slot])

        assert pool.size == 1
        assert pool.available_count == 1
        assert pool.busy_count == 0
        assert pool.slots == [single_slot]

    def test_pool_creation_with_multiple_slots(self, multi_slots):
        """Test pool creation with multiple slots."""
        pool = AccountPool(multi_slots)

        assert pool.size == 3
        assert pool.available_count == 3
        assert pool.busy_count == 0

    def test_pool_rejects_empty_slots(self):
        """Test that AccountPool allows empty slot list (for dynamic account addition via Admin UI)."""
        # Empty pool is now allowed - accounts can be added dynamically via Admin UI
        pool = AccountPool([])

        assert pool.size == 0
        assert pool.available_count == 0
        assert pool.busy_count == 0


# =============================================================================
# AccountPool.from_single Tests
# =============================================================================

class TestFromSingle:
    """Tests for backward-compatible single-account pool creation."""

    def test_from_single_creates_one_slot_pool(self, mock_auth_manager_factory):
        """Test that from_single creates a pool with one slot."""
        manager = mock_auth_manager_factory("default")
        pool = AccountPool.from_single(manager)

        assert pool.size == 1
        assert pool.slots[0].name == "default"
        assert pool.slots[0].auth_manager is manager

    def test_from_single_with_custom_name(self, mock_auth_manager_factory):
        """Test from_single with a custom account name."""
        manager = mock_auth_manager_factory("custom")
        pool = AccountPool.from_single(manager, name="my-account")

        assert pool.slots[0].name == "my-account"


# =============================================================================
# AccountPool.from_directory Tests
# =============================================================================

class TestFromDirectory:
    """Tests for multi-account directory loading."""

    def test_from_directory_loads_accounts(self, temp_multi_creds_dir):
        """Test loading accounts from a valid directory structure."""
        pool = AccountPool.from_directory(
            str(temp_multi_creds_dir),
            region="us-east-1",
        )

        assert pool.size == 2
        # Sorted order: account-1, account-2
        assert pool.slots[0].name == "account-1"
        assert pool.slots[1].name == "account-2"

    def test_from_directory_rejects_nonexistent_path(self):
        """Test that a nonexistent directory raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            AccountPool.from_directory("/nonexistent/path/x9y8z7")

    def test_from_directory_rejects_empty_directory(self, tmp_path):
        """Test that an empty directory returns empty pool (for dynamic account addition)."""
        # Empty directory now returns empty pool instead of raising error
        pool = AccountPool.from_directory(str(tmp_path))

        assert pool.size == 0
        assert pool.available_count == 0

    def test_from_directory_rejects_file_path(self, tmp_path):
        """Test that a file path (not directory) raises ValueError."""
        file_path = tmp_path / "not_a_directory.txt"
        file_path.write_text("test")

        with pytest.raises(ValueError, match="not a directory"):
            AccountPool.from_directory(str(file_path))

    def test_from_directory_skips_invalid_subdirs(self, tmp_path):
        """Test that directories without kiro-auth-token.json are skipped."""
        # Valid account
        valid_dir = tmp_path / "valid-account"
        valid_dir.mkdir(parents=True)
        valid_creds = {
            "accessToken": "token1",
            "refreshToken": "refresh1",
            "expiresAt": "2099-01-01T00:00:00.000Z",
            "region": "us-east-1",
        }
        (valid_dir / "kiro-auth-token.json").write_text(json.dumps(valid_creds))

        # Invalid account (only directory, no kiro-auth-token.json)
        (tmp_path / "invalid-account").mkdir()

        pool = AccountPool.from_directory(str(tmp_path), region="us-east-1")
        assert pool.size == 1
        assert pool.slots[0].name == "valid-account"

    def test_from_directory_skips_invalid_json(self, tmp_path):
        """Test that accounts with invalid JSON are skipped."""
        # Valid account
        valid_dir = tmp_path / "valid"
        valid_dir.mkdir(parents=True)
        valid_creds = {
            "accessToken": "token1",
            "refreshToken": "refresh1",
            "expiresAt": "2099-01-01T00:00:00.000Z",
            "region": "us-east-1",
        }
        (valid_dir / "kiro-auth-token.json").write_text(json.dumps(valid_creds))

        # Account with invalid JSON
        broken_dir = tmp_path / "broken"
        broken_dir.mkdir(parents=True)
        (broken_dir / "kiro-auth-token.json").write_text("{invalid json!@#$")

        pool = AccountPool.from_directory(str(tmp_path), region="us-east-1")
        assert pool.size == 1
        assert pool.slots[0].name == "valid"

    def test_from_directory_skips_missing_tokens(self, tmp_path):
        """Test that accounts without refreshToken or accessToken are skipped."""
        # Valid account
        valid_dir = tmp_path / "valid"
        valid_dir.mkdir(parents=True)
        valid_creds = {
            "accessToken": "token1",
            "refreshToken": "refresh1",
            "region": "us-east-1",
        }
        (valid_dir / "kiro-auth-token.json").write_text(json.dumps(valid_creds))

        # Account with no token fields
        broken_dir = tmp_path / "no-tokens"
        broken_dir.mkdir(parents=True)
        no_token_creds = {
            "region": "us-east-1",
            "something_else": "irrelevant",
        }
        (broken_dir / "kiro-auth-token.json").write_text(json.dumps(no_token_creds))

        pool = AccountPool.from_directory(str(tmp_path), region="us-east-1")
        assert pool.size == 1
        assert pool.slots[0].name == "valid"

    def test_from_directory_all_invalid_raises_error(self, tmp_path):
        """Test that empty pool is returned if all subdirs are invalid (for dynamic account addition)."""
        # Account with invalid JSON
        broken_dir = tmp_path / "broken"
        broken_dir.mkdir(parents=True)
        (broken_dir / "kiro-auth-token.json").write_text("{invalid")

        # All invalid accounts now returns empty pool instead of raising error
        pool = AccountPool.from_directory(str(tmp_path), region="us-east-1")

        assert pool.size == 0
        assert pool.available_count == 0

    def test_from_directory_ignores_files_in_root(self, tmp_path):
        """Test that files (not directories) in root are ignored."""
        # File in root (should be ignored)
        (tmp_path / "some-file.json").write_text("{}")

        # Valid account directory
        valid_dir = tmp_path / "account-1"
        valid_dir.mkdir(parents=True)
        valid_creds = {
            "accessToken": "t",
            "refreshToken": "r",
            "region": "us-east-1",
        }
        (valid_dir / "kiro-auth-token.json").write_text(json.dumps(valid_creds))

        pool = AccountPool.from_directory(str(tmp_path), region="us-east-1")
        assert pool.size == 1


# =============================================================================
# Queue Acquire/Release Tests
# =============================================================================

class TestQueueAcquireRelease:
    """Tests for the acquire/release queue mechanics."""

    @pytest.mark.asyncio
    async def test_acquire_returns_slot(self, single_pool, single_slot):
        """Test that acquire returns an available slot."""
        slot = await single_pool.acquire(timeout=1.0)

        assert slot is single_slot
        assert slot.active_requests == 1
        assert slot.total_requests == 1

    @pytest.mark.asyncio
    async def test_release_returns_slot_to_pool(self, single_pool, single_slot):
        """Test that release makes the slot available again."""
        slot = await single_pool.acquire(timeout=1.0)
        assert single_pool.available_count == 0

        await single_pool.release(slot)
        assert single_pool.available_count == 1
        assert slot.active_requests == 0

    @pytest.mark.asyncio
    async def test_acquire_blocks_when_all_busy(self, single_pool):
        """Test that acquire blocks when all slots are in use."""
        # Acquire the only slot
        slot = await single_pool.acquire(timeout=1.0)

        # Second acquire should timeout
        with pytest.raises(asyncio.TimeoutError):
            await single_pool.acquire(timeout=0.1)

        # Release and try again
        await single_pool.release(slot)
        slot2 = await single_pool.acquire(timeout=1.0)
        assert slot2 is slot

    @pytest.mark.asyncio
    async def test_multi_slot_concurrent_acquire(self, multi_pool):
        """Test that multiple slots can be acquired concurrently."""
        slot1 = await multi_pool.acquire(timeout=1.0)
        slot2 = await multi_pool.acquire(timeout=1.0)
        slot3 = await multi_pool.acquire(timeout=1.0)

        # All 3 should be different accounts
        names = {slot1.name, slot2.name, slot3.name}
        assert len(names) == 3
        assert multi_pool.available_count == 0
        assert multi_pool.busy_count == 3

        # 4th should timeout
        with pytest.raises(asyncio.TimeoutError):
            await multi_pool.acquire(timeout=0.1)

        # Release one
        await multi_pool.release(slot1)
        assert multi_pool.available_count == 1

        slot4 = await multi_pool.acquire(timeout=1.0)
        assert slot4 is slot1  # Same slot recycled

        # Cleanup
        await multi_pool.release(slot2)
        await multi_pool.release(slot3)
        await multi_pool.release(slot4)

    @pytest.mark.asyncio
    async def test_acquire_timeout_raises_timeout_error(self, single_pool):
        """Test that acquire raises TimeoutError on timeout."""
        # Exhaust the pool
        slot = await single_pool.acquire(timeout=1.0)

        # Verify TimeoutError is raised
        with pytest.raises(asyncio.TimeoutError):
            await single_pool.acquire(timeout=0.05)

        await single_pool.release(slot)

    @pytest.mark.asyncio
    async def test_request_counters_increment(self, single_pool, single_slot):
        """Test that active and total counters work correctly."""
        # First acquire
        slot = await single_pool.acquire(timeout=1.0)
        assert slot.active_requests == 1
        assert slot.total_requests == 1

        await single_pool.release(slot)
        assert slot.active_requests == 0
        assert slot.total_requests == 1

        # Second acquire
        slot = await single_pool.acquire(timeout=1.0)
        assert slot.active_requests == 1
        assert slot.total_requests == 2

        await single_pool.release(slot)
        assert slot.active_requests == 0
        assert slot.total_requests == 2


# =============================================================================
# FIFO Fair Scheduling Tests
# =============================================================================

class TestFIFOScheduling:
    """Tests for fair, first-in-first-out request scheduling."""

    @pytest.mark.asyncio
    async def test_fifo_order_with_waiting_requests(self, single_pool, single_slot):
        """
        Test that waiting requests are served in FIFO order.

        Scenario:
        - Request A acquires the slot
        - Request B starts waiting
        - Request A releases
        - Request B should get the slot
        """
        events = []

        async def request_a():
            slot = await single_pool.acquire(timeout=5.0)
            events.append("A_acquired")
            await asyncio.sleep(0.1)  # Simulate work
            events.append("A_releasing")
            await single_pool.release(slot)

        async def request_b():
            await asyncio.sleep(0.02)  # Give A time to acquire first
            events.append("B_waiting")
            slot = await single_pool.acquire(timeout=5.0)
            events.append("B_acquired")
            await single_pool.release(slot)

        await asyncio.gather(request_a(), request_b())

        # B should have waited for A
        assert events.index("A_acquired") < events.index("B_waiting")
        assert events.index("A_releasing") < events.index("B_acquired")

    @pytest.mark.asyncio
    async def test_round_robin_distribution(self, multi_pool):
        """
        Test that slots are distributed in round-robin fashion.

        With 3 accounts, sequential acquire/release cycles should
        rotate through accounts.
        """
        seen_names = []
        for _ in range(6):  # Two full rotations
            slot = await multi_pool.acquire(timeout=1.0)
            seen_names.append(slot.name)
            await multi_pool.release(slot)

        # First 3 should be the 3 different accounts
        assert len(set(seen_names[:3])) == 3
        # Second 3 should also be the same 3 accounts (round-robin)
        assert len(set(seen_names[3:])) == 3


# =============================================================================
# Pool Status Tests
# =============================================================================

class TestPoolStatus:
    """Tests for pool status monitoring."""

    def test_status_returns_correct_info(self, multi_pool, multi_slots):
        """Test get_status returns correct summary."""
        status = multi_pool.get_status()

        assert status["total_accounts"] == 3
        assert status["available"] == 3
        assert status["busy"] == 0
        assert len(status["accounts"]) == 3

        for i, acct in enumerate(status["accounts"]):
            assert acct["name"] == f"account-{i}"
            assert acct["active_requests"] == 0
            assert acct["total_requests"] == 0

    def test_status_accounts_sorted_by_numeric_suffix(self, mock_auth_manager_factory):
        """Account list should use natural numeric sorting for account-N names."""
        slots = [
            AccountSlot(name="account-10", auth_manager=mock_auth_manager_factory("10")),
            AccountSlot(name="account-2", auth_manager=mock_auth_manager_factory("2")),
            AccountSlot(name="account-1", auth_manager=mock_auth_manager_factory("1")),
        ]
        pool = AccountPool(slots)

        status = pool.get_status()
        names = [acct["name"] for acct in status["accounts"]]
        assert names == ["account-1", "account-2", "account-10"]

    @pytest.mark.asyncio
    async def test_status_reflects_active_requests(self, multi_pool):
        """Test that status reflects acquired slots."""
        slot = await multi_pool.acquire(timeout=1.0)

        status = multi_pool.get_status()
        assert status["available"] == 2
        assert status["busy"] == 1

        # Find the active account in status
        active_accounts = [a for a in status["accounts"] if a["active_requests"] > 0]
        assert len(active_accounts) == 1
        assert active_accounts[0]["name"] == slot.name

        await multi_pool.release(slot)

    @pytest.mark.asyncio
    async def test_status_after_multiple_requests(self, single_pool, single_slot):
        """Test that status tracks total_requests correctly."""
        for _ in range(5):
            slot = await single_pool.acquire(timeout=1.0)
            await single_pool.release(slot)

        status = single_pool.get_status()
        assert status["accounts"][0]["total_requests"] == 5
        assert status["accounts"][0]["active_requests"] == 0


# =============================================================================
# Model control tests
# =============================================================================


class _FakeAdminConfig:
    def __init__(self, controls):
        self._controls = controls

    def get_model_controls(self):
        return self._controls


class TestModelControls:
    def test_is_model_enabled_no_admin_config_allows(self, single_pool, single_slot):
        assert single_pool._is_model_enabled_for_slot(single_slot, "m1") is True

    def test_is_model_enabled_no_quota_info_allows(self, single_pool, single_slot):
        single_pool._admin_config = _FakeAdminConfig({"KIRO FREE": {"m1": True}})
        single_slot.quota_info = None
        assert single_pool._is_model_enabled_for_slot(single_slot, "m1") is True

    def test_is_model_enabled_plan_without_controls_allows(self, single_pool, single_slot):
        single_pool._admin_config = _FakeAdminConfig({})
        single_slot.quota_info = QuotaInfo(subscription_plan="KIRO FREE")
        assert single_pool._is_model_enabled_for_slot(single_slot, "m1") is True

    def test_is_model_enabled_plan_with_controls_only_allows_listed_true(self, single_pool, single_slot):
        single_pool._admin_config = _FakeAdminConfig({"KIRO FREE": {"m1": True, "m2": False}})
        single_slot.quota_info = QuotaInfo(subscription_plan="KIRO FREE")
        assert single_pool._is_model_enabled_for_slot(single_slot, "m1") is True
        assert single_pool._is_model_enabled_for_slot(single_slot, "m2") is False
        assert single_pool._is_model_enabled_for_slot(single_slot, "m3") is False

    @pytest.mark.asyncio
    async def test_acquire_for_model_skips_disabled_model_slots(self, mock_auth_manager_factory):
        slot_free = AccountSlot(name="account-0", auth_manager=mock_auth_manager_factory("0"))
        slot_pro = AccountSlot(name="account-1", auth_manager=mock_auth_manager_factory("1"))
        slot_free.quota_info = QuotaInfo(subscription_plan="KIRO FREE")
        slot_pro.quota_info = QuotaInfo(subscription_plan="KIRO PRO")

        pool = AccountPool([slot_free, slot_pro], quota_check_interval=0.0)
        pool._admin_config = _FakeAdminConfig({"KIRO FREE": {"m1": True}, "KIRO PRO": {"m1": False}})

        got = await pool.acquire_for_model(model_id="m1", timeout=1.0)
        assert got.name == "account-0"
        await pool.release(got)


# =============================================================================
# Properties Tests
# =============================================================================

class TestPoolProperties:
    """Tests for pool property accessors."""

    def test_size_property(self, multi_pool):
        """Test size returns number of slots."""
        assert multi_pool.size == 3

    def test_available_count_property(self, multi_pool):
        """Test available_count returns queue size."""
        assert multi_pool.available_count == 3

    def test_busy_count_property(self, multi_pool):
        """Test busy_count returns total - available."""
        assert multi_pool.busy_count == 0

    @pytest.mark.asyncio
    async def test_properties_update_on_acquire_release(self, multi_pool):
        """Test that properties correctly reflect state changes."""
        slot1 = await multi_pool.acquire(timeout=1.0)
        assert multi_pool.available_count == 2
        assert multi_pool.busy_count == 1

        slot2 = await multi_pool.acquire(timeout=1.0)
        assert multi_pool.available_count == 1
        assert multi_pool.busy_count == 2

        await multi_pool.release(slot1)
        assert multi_pool.available_count == 2
        assert multi_pool.busy_count == 1

        await multi_pool.release(slot2)
        assert multi_pool.available_count == 3
        assert multi_pool.busy_count == 0


# =============================================================================
# Concurrency Safety Tests
# =============================================================================

class TestConcurrencySafety:
    """Tests for thread-safety and concurrent access patterns."""

    @pytest.mark.asyncio
    async def test_concurrent_acquire_release_stress(self, multi_pool):
        """
        Stress test: many concurrent requests competing for slots.

        Ensures no slot is double-acquired and all requests complete.
        """
        completed = []

        async def worker(worker_id: int):
            slot = await multi_pool.acquire(timeout=5.0)
            # Simulate some work
            await asyncio.sleep(0.01)
            completed.append((worker_id, slot.name))
            await multi_pool.release(slot)

        # 10 workers competing for 3 slots
        tasks = [worker(i) for i in range(10)]
        await asyncio.gather(*tasks)

        assert len(completed) == 10
        # After all, pool should be fully available
        assert multi_pool.available_count == 3

    @pytest.mark.asyncio
    async def test_serialization_per_account(self, mock_auth_manager_factory):
        """
        Test that requests to the same account are serialized.

        With a single-slot pool, concurrent requests should execute
        one at a time, never overlapping.
        """
        execution_log = []
        pool = AccountPool.from_single(mock_auth_manager_factory("serialized"))

        async def request(request_id: int):
            slot = await pool.acquire(timeout=5.0)
            execution_log.append(f"start-{request_id}")
            await asyncio.sleep(0.05)  # Simulate work
            execution_log.append(f"end-{request_id}")
            await pool.release(slot)

        # Launch 3 concurrent requests
        await asyncio.gather(
            request(1),
            request(2),
            request(3),
        )

        # Verify no overlap: each start should be followed by its end
        # before the next start (serial execution)
        for i in range(0, len(execution_log), 2):
            start = execution_log[i]
            end = execution_log[i + 1]
            # Extract IDs
            start_id = start.split("-")[1]
            end_id = end.split("-")[1]
            assert start_id == end_id, f"Overlap detected: {start} followed by {end}"


# =============================================================================
# Edge Cases
# =============================================================================

class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_acquire_with_zero_timeout(self, single_pool):
        """Test acquire with 0 timeout on empty pool."""
        # First acquire succeeds
        slot = await single_pool.acquire(timeout=1.0)

        # Zero-timeout acquire should raise immediately
        with pytest.raises(asyncio.TimeoutError):
            await single_pool.acquire(timeout=0.0)

        await single_pool.release(slot)

    @pytest.mark.asyncio
    async def test_release_decrements_active_below_zero_guard(self, single_pool, single_slot):
        """Test that active_requests can go below 0 if misused (no crash)."""
        # This tests robustness - double release shouldn't crash
        slot = await single_pool.acquire(timeout=1.0)
        await single_pool.release(slot)

        # Double release - active goes to -1, but doesn't crash
        await single_pool.release(slot)
        assert slot.active_requests == -1

    @pytest.mark.asyncio
    async def test_acquire_release_maintains_pool_integrity(self, multi_pool):
        """Test that repeated acquire/release cycles maintain pool integrity."""
        for _ in range(20):
            slots = []
            for _ in range(3):
                slot = await multi_pool.acquire(timeout=1.0)
                slots.append(slot)

            assert multi_pool.available_count == 0

            for slot in slots:
                await multi_pool.release(slot)

            assert multi_pool.available_count == 3

    def test_slots_property_returns_original_list(self, multi_pool, multi_slots):
        """Test that slots property returns the original list."""
        assert multi_pool.slots == multi_slots

    def test_pool_from_directory_with_tilde_expansion(self, tmp_path):
        """Test that directory path supports ~ expansion."""
        # Create a path that doesn't actually use ~ but tests Path handling
        test_dir = tmp_path / "test-home"
        test_dir.mkdir(parents=True)
        creds = {
            "accessToken": "t",
            "refreshToken": "r",
            "region": "us-east-1",
        }
        (test_dir / "kiro-auth-token.json").write_text(json.dumps(creds))

        # from_directory should handle the path correctly
        pool = AccountPool.from_directory(str(tmp_path), region="us-east-1")
        assert pool.size == 1


# =============================================================================
# Cooldown Tests
# =============================================================================

class TestAccountSlotCooldown:
    """Tests for AccountSlot cooldown properties."""

    def test_is_cooling_down_false_by_default(self, mock_auth_manager_factory):
        """Test that a new slot is not cooling down."""
        slot = AccountSlot(name="test", auth_manager=mock_auth_manager_factory())
        assert slot.is_cooling_down is False
        assert slot.cooldown_remaining == 0.0

    def test_is_cooling_down_true_when_set(self, mock_auth_manager_factory):
        """Test that cooldown_until in the future means cooling down."""
        import time
        slot = AccountSlot(name="test", auth_manager=mock_auth_manager_factory())
        slot.cooldown_until = time.monotonic() + 300
        assert slot.is_cooling_down is True
        assert slot.cooldown_remaining > 299

    def test_is_cooling_down_false_when_expired(self, mock_auth_manager_factory):
        """Test that cooldown_until in the past means not cooling down."""
        import time
        slot = AccountSlot(name="test", auth_manager=mock_auth_manager_factory())
        slot.cooldown_until = time.monotonic() - 1
        assert slot.is_cooling_down is False
        assert slot.cooldown_remaining == 0.0

    def test_repr_shows_cooldown(self, mock_auth_manager_factory):
        """Test that repr includes cooldown info when cooling."""
        import time
        slot = AccountSlot(name="test", auth_manager=mock_auth_manager_factory())
        slot.cooldown_until = time.monotonic() + 60
        repr_str = repr(slot)
        assert "cooldown=" in repr_str

    def test_repr_no_cooldown_when_not_cooling(self, mock_auth_manager_factory):
        """Test that repr does not show cooldown when not cooling."""
        slot = AccountSlot(name="test", auth_manager=mock_auth_manager_factory())
        repr_str = repr(slot)
        assert "cooldown=" not in repr_str


class TestAccountPoolCooldown:
    """Tests for AccountPool cooldown release behavior."""

    @pytest.mark.asyncio
    async def test_release_with_cooldown_removes_from_queue(self, single_pool, single_slot):
        """Test that releasing with cooldown does NOT immediately put slot back."""
        slot = await single_pool.acquire(timeout=1.0)
        assert single_pool.available_count == 0

        # Release with cooldown - slot should NOT be immediately available
        await single_pool.release(slot, cooldown_seconds=300.0)

        # Queue should be empty (slot is cooling down, not in queue)
        assert single_pool.available_count == 0

    @pytest.mark.asyncio
    async def test_release_without_cooldown_puts_back_immediately(self, single_pool, single_slot):
        """Test that normal release puts slot back immediately."""
        slot = await single_pool.acquire(timeout=1.0)
        assert single_pool.available_count == 0

        await single_pool.release(slot, cooldown_seconds=0.0)
        assert single_pool.available_count == 1

    @pytest.mark.asyncio
    async def test_release_default_no_cooldown(self, single_pool, single_slot):
        """Test that default release (no cooldown arg) puts back immediately."""
        slot = await single_pool.acquire(timeout=1.0)
        await single_pool.release(slot)
        assert single_pool.available_count == 1

    @pytest.mark.asyncio
    async def test_delayed_release_reinserts_after_delay(self, single_pool, single_slot):
        """Test that a cooled-down slot returns to the pool after delay."""
        slot = await single_pool.acquire(timeout=1.0)

        # Release with very short cooldown
        await single_pool.release(slot, cooldown_seconds=0.1)

        # Initially unavailable
        assert single_pool.available_count == 0

        # Wait for cooldown to expire
        await asyncio.sleep(0.2)

        # Now should be back in the queue
        assert single_pool.available_count == 1

    @pytest.mark.asyncio
    async def test_cooled_slot_can_be_acquired_after_cooldown(self, single_pool, single_slot):
        """Test that a slot can be re-acquired after cooldown expires."""
        slot = await single_pool.acquire(timeout=1.0)
        await single_pool.release(slot, cooldown_seconds=0.1)

        # Should timeout immediately since slot is cooling
        with pytest.raises(asyncio.TimeoutError):
            await single_pool.acquire(timeout=0.05)

        # Wait for cooldown
        await asyncio.sleep(0.15)

        # Now should be acquirable
        slot2 = await single_pool.acquire(timeout=0.1)
        assert slot2 is slot
        await single_pool.release(slot2)

    @pytest.mark.asyncio
    async def test_cooldown_sets_cooldown_until(self, single_pool, single_slot):
        """Test that release with cooldown sets cooldown_until on the slot."""
        import time
        slot = await single_pool.acquire(timeout=1.0)
        assert slot.cooldown_until == 0.0

        await single_pool.release(slot, cooldown_seconds=300.0)
        assert slot.cooldown_until > time.monotonic()
        assert slot.is_cooling_down is True

    @pytest.mark.asyncio
    async def test_cooldown_clears_after_delay(self, single_pool, single_slot):
        """Test that cooldown_until is cleared after delayed release."""
        slot = await single_pool.acquire(timeout=1.0)
        await single_pool.release(slot, cooldown_seconds=0.1)
        assert slot.is_cooling_down is True

        await asyncio.sleep(0.2)
        assert slot.cooldown_until == 0.0
        assert slot.is_cooling_down is False

    @pytest.mark.asyncio
    async def test_multi_pool_cooldown_one_account(self, multi_pool, multi_slots):
        """Test that cooling one account still allows others to work."""
        # Acquire all 3
        slot1 = await multi_pool.acquire(timeout=1.0)
        slot2 = await multi_pool.acquire(timeout=1.0)
        slot3 = await multi_pool.acquire(timeout=1.0)
        assert multi_pool.available_count == 0

        # Release slot1 with cooldown, slot2 and slot3 without
        await multi_pool.release(slot1, cooldown_seconds=300.0)
        await multi_pool.release(slot2)
        await multi_pool.release(slot3)

        # Only 2 available (slot1 is cooling)
        assert multi_pool.available_count == 2

    @pytest.mark.asyncio
    async def test_get_status_shows_cooldown_info(self, single_pool, single_slot):
        """Test that get_status includes cooldown information."""
        slot = await single_pool.acquire(timeout=1.0)
        await single_pool.release(slot, cooldown_seconds=300.0)

        status = single_pool.get_status()
        assert status["cooling_down"] == 1
        assert "cooldown_remaining_seconds" in status["accounts"][0]
        assert status["accounts"][0]["cooldown_remaining_seconds"] > 0

    @pytest.mark.asyncio
    async def test_get_status_no_cooldown_info_when_not_cooling(self, single_pool, single_slot):
        """Test that get_status does not include cooldown info when not cooling."""
        status = single_pool.get_status()
        assert status["cooling_down"] == 0
        assert "cooldown_remaining_seconds" not in status["accounts"][0]

    @pytest.mark.asyncio
    async def test_active_requests_decremented_on_cooldown_release(self, single_pool, single_slot):
        """Test that active_requests is decremented even with cooldown."""
        slot = await single_pool.acquire(timeout=1.0)
        assert slot.active_requests == 1

        await single_pool.release(slot, cooldown_seconds=300.0)
        assert slot.active_requests == 0
