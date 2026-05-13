# -*- coding: utf-8 -*-

"""
Unit tests for kiro.quota_checker module.

Tests cover:
- QuotaInfo data model properties
- CBOR response parsing
- Error handling for API failures
"""

import time
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from kiro.quota_checker import (
    QuotaInfo,
    QuotaCheckError,
    _parse_usage_response,
    check_quota,
)


# ==============================================================================
# QuotaInfo Data Model Tests
# ==============================================================================


class TestQuotaInfoProperties:
    """Test QuotaInfo computed properties."""

    def test_total_used_base_only(self):
        """Base usage only (no trial)."""
        info = QuotaInfo(base_used=10.0, base_limit=50.0)
        assert info.total_used == 10.0

    def test_total_used_with_active_trial(self):
        """Base + trial usage when trial is active."""
        info = QuotaInfo(
            base_used=5.0, base_limit=50.0,
            trial_used=3.5, trial_limit=500.0, trial_active=True,
        )
        assert info.total_used == 8.5

    def test_total_used_with_inactive_trial(self):
        """Trial usage not counted when trial is inactive."""
        info = QuotaInfo(
            base_used=5.0, base_limit=50.0,
            trial_used=100.0, trial_limit=500.0, trial_active=False,
        )
        assert info.total_used == 5.0

    def test_total_limit_base_only(self):
        """Base limit only (no trial)."""
        info = QuotaInfo(base_limit=50.0)
        assert info.total_limit == 50.0

    def test_total_limit_with_active_trial(self):
        """Base + trial limit when trial is active."""
        info = QuotaInfo(base_limit=50.0, trial_limit=500.0, trial_active=True)
        assert info.total_limit == 550.0

    def test_total_limit_with_inactive_trial(self):
        """Trial limit not counted when trial is inactive."""
        info = QuotaInfo(base_limit=50.0, trial_limit=500.0, trial_active=False)
        assert info.total_limit == 50.0

    def test_remaining_with_credits_left(self):
        """Remaining is total_limit - total_used."""
        info = QuotaInfo(
            base_used=3.5, base_limit=50.0,
            trial_used=3.5, trial_limit=500.0, trial_active=True,
        )
        assert info.remaining == 543.0

    def test_remaining_zero_when_exhausted(self):
        """Remaining is 0 when all credits used."""
        info = QuotaInfo(base_used=50.0, base_limit=50.0)
        assert info.remaining == 0.0

    def test_remaining_never_negative(self):
        """Remaining can't go below 0."""
        info = QuotaInfo(base_used=100.0, base_limit=50.0)
        assert info.remaining == 0.0

    def test_is_exhausted_when_at_limit(self):
        """Account is exhausted when used == limit."""
        info = QuotaInfo(base_used=50.0, base_limit=50.0)
        assert info.is_exhausted is True

    def test_is_exhausted_when_over_limit(self):
        """Account is exhausted when used > limit."""
        info = QuotaInfo(base_used=55.0, base_limit=50.0)
        assert info.is_exhausted is True

    def test_not_exhausted_when_credits_remain(self):
        """Account is not exhausted when credits remain."""
        info = QuotaInfo(base_used=49.0, base_limit=50.0)
        assert info.is_exhausted is False

    def test_usage_summary_format(self):
        """Usage summary is 'used / limit' format."""
        info = QuotaInfo(
            base_used=3.5, base_limit=50.0,
            trial_used=3.5, trial_limit=500.0, trial_active=True,
        )
        assert info.usage_summary == "7.0 / 550"

    def test_age_seconds(self):
        """Age should be close to 0 for freshly created QuotaInfo."""
        info = QuotaInfo()
        age = info.age_seconds()
        assert 0.0 <= age < 1.0

    def test_age_seconds_stale(self):
        """Age increases as time passes."""
        info = QuotaInfo(checked_at=time.monotonic() - 60.0)
        age = info.age_seconds()
        assert 59.0 <= age <= 62.0


# ==============================================================================
# Response Parsing Tests
# ==============================================================================


class TestParseUsageResponse:
    """Test _parse_usage_response with various API response shapes."""

    def test_full_response_with_trial(self):
        """Parse complete response with active free trial."""
        data = {
            "userInfo": {"email": "user@test.com", "userId": "uid-123"},
            "subscriptionInfo": {"subscriptionTitle": "KIRO FREE"},
            "nextDateReset": "2026-05-01 00:00:00+00:00",
            "usageBreakdownList": [{
                "resourceType": "CREDIT",
                "currentUsageWithPrecision": 0.0,
                "usageLimitWithPrecision": 50.0,
                "freeTrialInfo": {
                    "freeTrialStatus": "ACTIVE",
                    "currentUsageWithPrecision": 3.5,
                    "usageLimitWithPrecision": 500.0,
                    "freeTrialExpiry": "2026-05-08 08:22:41+00:00",
                },
            }],
        }
        result = _parse_usage_response(data)

        assert result.email == "user@test.com"
        assert result.user_id == "uid-123"
        assert result.subscription_plan == "KIRO FREE"
        assert result.base_used == 0.0
        assert result.base_limit == 50.0
        assert result.trial_active is True
        assert result.trial_used == 3.5
        assert result.trial_limit == 500.0
        assert result.total_limit == 550.0
        assert result.total_used == 3.5
        assert result.remaining == 546.5
        assert result.is_exhausted is False

    def test_response_without_trial(self):
        """Parse response when no free trial is present."""
        data = {
            "userInfo": {"email": "paid@test.com"},
            "subscriptionInfo": {"subscriptionTitle": "KIRO PRO"},
            "overageConfiguration": {"overageEnabled": False},
            "usageBreakdownList": [{
                "resourceType": "CREDIT",
                "currentUsageWithPrecision": 25.0,
                "usageLimitWithPrecision": 1000.0,
            }],
        }
        result = _parse_usage_response(data)

        assert result.email == "paid@test.com"
        assert result.trial_active is False
        assert result.total_limit == 1000.0
        assert result.total_used == 25.0
        assert result.overage_enabled is False

    def test_overage_enabled_bool_true(self):
        """Parse overageConfiguration.overageEnabled as bool."""
        data = {
            "userInfo": {"email": "user@test.com"},
            "overageConfiguration": {"overageEnabled": True},
            "usageBreakdownList": [{
                "resourceType": "CREDIT",
                "currentUsageWithPrecision": 50.0,
                "usageLimitWithPrecision": 50.0,
            }],
        }
        result = _parse_usage_response(data)
        assert result.overage_enabled is True

    def test_overage_enabled_string_true(self):
        """Parse overageConfiguration.overageEnabled as 'true' string."""
        data = {
            "userInfo": {"email": "user@test.com"},
            "overageConfiguration": {"overageEnabled": "true"},
            "usageBreakdownList": [{
                "resourceType": "CREDIT",
                "currentUsageWithPrecision": 50.0,
                "usageLimitWithPrecision": 50.0,
            }],
        }
        result = _parse_usage_response(data)
        assert result.overage_enabled is True

    def test_overage_enabled_string_false(self):
        """Parse overageConfiguration.overageEnabled as 'false' string."""
        data = {
            "userInfo": {"email": "user@test.com"},
            "overageConfiguration": {"overageEnabled": "false"},
            "usageBreakdownList": [{
                "resourceType": "CREDIT",
                "currentUsageWithPrecision": 50.0,
                "usageLimitWithPrecision": 50.0,
            }],
        }
        result = _parse_usage_response(data)
        assert result.overage_enabled is False

    def test_response_with_expired_trial(self):
        """Parse response when trial has expired."""
        data = {
            "userInfo": {},
            "usageBreakdownList": [{
                "resourceType": "CREDIT",
                "currentUsageWithPrecision": 45.0,
                "usageLimitWithPrecision": 50.0,
                "freeTrialInfo": {
                    "freeTrialStatus": "EXPIRED",
                    "currentUsageWithPrecision": 500.0,
                    "usageLimitWithPrecision": 500.0,
                },
            }],
        }
        result = _parse_usage_response(data)

        assert result.trial_active is False
        assert result.total_limit == 50.0  # Only base quota
        assert result.total_used == 45.0

    def test_empty_usage_breakdown(self):
        """Handle empty usageBreakdownList gracefully."""
        data = {"userInfo": {}, "usageBreakdownList": []}
        result = _parse_usage_response(data)

        assert result.base_used == 0.0
        assert result.base_limit == 0.0
        assert result.is_exhausted is True  # 0/0 = exhausted

    def test_missing_fields_graceful(self):
        """Handle missing top-level fields gracefully."""
        data = {}
        result = _parse_usage_response(data)

        assert result.email == ""
        assert result.subscription_plan == ""

    def test_non_credit_resource_type_ignored(self):
        """Non-CREDIT resource types are ignored."""
        data = {
            "userInfo": {},
            "usageBreakdownList": [{
                "resourceType": "SOMETHING_ELSE",
                "currentUsageWithPrecision": 999.0,
                "usageLimitWithPrecision": 1000.0,
            }],
        }
        result = _parse_usage_response(data)

        assert result.base_used == 0.0  # Not parsed


# ==============================================================================
# check_quota Function Tests
# ==============================================================================


class TestCheckQuota:
    """Test the async check_quota function."""

    @pytest.mark.asyncio
    async def test_successful_quota_check(self):
        """Test successful API call returns valid QuotaInfo."""
        import cbor2

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = cbor2.dumps({
            "userInfo": {"email": "test@example.com"},
            "usageBreakdownList": [{
                "resourceType": "CREDIT",
                "currentUsageWithPrecision": 10.0,
                "usageLimitWithPrecision": 50.0,
            }],
        })

        with patch("kiro.quota_checker.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await check_quota("fake-token", "BuilderId")
            assert result.email == "test@example.com"
            assert result.base_used == 10.0

    @pytest.mark.asyncio
    async def test_401_raises_error(self):
        """Test 401 response raises QuotaCheckError."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.content = b""

        with patch("kiro.quota_checker.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            with pytest.raises(QuotaCheckError, match="401"):
                await check_quota("expired-token")

    @pytest.mark.asyncio
    async def test_403_raises_error(self):
        """Test 403 response raises QuotaCheckError."""
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.content = b""

        with patch("kiro.quota_checker.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            with pytest.raises(QuotaCheckError, match="403"):
                await check_quota("bad-token")

    @pytest.mark.asyncio
    async def test_429_raises_error(self):
        """Test 429 response raises QuotaCheckError."""
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.content = b""

        with patch("kiro.quota_checker.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            with pytest.raises(QuotaCheckError, match="429"):
                await check_quota("token")
