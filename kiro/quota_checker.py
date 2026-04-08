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
Quota checker for Kiro accounts.

Queries the Kiro Web Portal API to proactively detect account usage
and remaining quota. This prevents wasting requests on exhausted accounts
and provides visibility into account health.

API Endpoint:
    POST https://app.kiro.dev/service/KiroWebPortalService/operation/GetUserUsageAndLimits
    Content-Type: application/cbor
    smithy-protocol: rpc-v2-cbor

Quota Model (Kiro credit system):
    - Each account has a monthly base quota (e.g., 50 credits for Free)
    - Free trial adds bonus quota (e.g., 500 credits) with an expiry date
    - Total available = base quota + free trial quota (when active)
    - Usage is tracked in credits (unit: INVOCATIONS)
"""

import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from loguru import logger


# Kiro Web Portal API endpoint
KIRO_PORTAL_URL = "https://app.kiro.dev/service/KiroWebPortalService/operation/GetUserUsageAndLimits"


@dataclass
class QuotaInfo:
    """
    Parsed quota information for a Kiro account.

    Attributes:
        email: Account email address
        user_id: Kiro user ID
        subscription_plan: Subscription plan name (e.g., "KIRO FREE")
        base_used: Credits used in the base (monthly) quota
        base_limit: Total credits in the base quota
        trial_used: Credits used in free trial quota (0 if no trial)
        trial_limit: Total credits in free trial quota (0 if no trial)
        trial_active: Whether the free trial is currently active
        trial_expiry: Free trial expiry date string (empty if no trial)
        next_reset: Next monthly quota reset date
        checked_at: Monotonic timestamp when this info was fetched
    """

    email: str = ""
    user_id: str = ""
    subscription_plan: str = ""
    base_used: float = 0.0
    base_limit: float = 0.0
    trial_used: float = 0.0
    trial_limit: float = 0.0
    trial_active: bool = False
    trial_expiry: str = ""
    next_reset: str = ""
    checked_at: float = field(default_factory=time.monotonic)

    @property
    def total_used(self) -> float:
        """Total credits used across all quota sources."""
        used = self.base_used
        if self.trial_active:
            used += self.trial_used
        return used

    @property
    def total_limit(self) -> float:
        """Total credits available across all quota sources."""
        limit = self.base_limit
        if self.trial_active:
            limit += self.trial_limit
        return limit

    @property
    def remaining(self) -> float:
        """Remaining credits across all quota sources."""
        return max(0.0, self.total_limit - self.total_used)

    @property
    def is_exhausted(self) -> bool:
        """Whether all quota is used up."""
        return self.remaining <= 0

    @property
    def usage_summary(self) -> str:
        """Human-readable usage summary like '3.5 / 550'."""
        return f"{self.total_used:.1f} / {self.total_limit:.0f}"

    def age_seconds(self) -> float:
        """Seconds since this info was fetched."""
        return time.monotonic() - self.checked_at


def _parse_usage_response(data: dict) -> QuotaInfo:
    """
    Parse the CBOR-decoded response from GetUserUsageAndLimits into QuotaInfo.

    Handles the credit-based quota system where:
    - usageBreakdownList contains one entry per resource type (CREDIT)
    - freeTrialInfo is nested inside the breakdown when a trial is active
    - currentUsage/usageLimit provide base quota numbers
    - freeTrialInfo provides trial quota numbers

    Args:
        data: Decoded CBOR response dictionary

    Returns:
        QuotaInfo with parsed quota details
    """
    info = QuotaInfo()

    # User info
    user_info = data.get("userInfo", {})
    info.email = user_info.get("email", "")
    info.user_id = user_info.get("userId", "")

    # Subscription info
    sub_info = data.get("subscriptionInfo", {})
    info.subscription_plan = sub_info.get("subscriptionTitle", "")

    # Reset date
    info.next_reset = str(data.get("nextDateReset", ""))

    # Usage breakdown (credit-based)
    breakdown_list = data.get("usageBreakdownList", [])
    for breakdown in breakdown_list:
        if breakdown.get("resourceType") == "CREDIT":
            # Base quota
            info.base_used = float(breakdown.get("currentUsageWithPrecision", 0.0))
            info.base_limit = float(breakdown.get("usageLimitWithPrecision", 0.0))

            # Free trial info
            trial = breakdown.get("freeTrialInfo")
            if trial and trial.get("freeTrialStatus") == "ACTIVE":
                info.trial_active = True
                info.trial_used = float(trial.get("currentUsageWithPrecision", 0.0))
                info.trial_limit = float(trial.get("usageLimitWithPrecision", 0.0))
                info.trial_expiry = str(trial.get("freeTrialExpiry", ""))
            break  # Only process first CREDIT breakdown

    info.checked_at = time.monotonic()
    return info


async def check_quota(access_token: str, provider: str = "BuilderId") -> QuotaInfo:
    """
    Query Kiro Web Portal API for account usage and limits.

    Makes a CBOR-encoded POST request to the GetUserUsageAndLimits endpoint.
    This is a lightweight API call that doesn't count against the usage quota.

    Args:
        access_token: Valid Kiro access token
        provider: Auth provider name (e.g., "BuilderId", "IAM_Identity_Center")

    Returns:
        QuotaInfo with parsed usage data

    Raises:
        QuotaCheckError: If the API request fails or response is invalid
    """
    try:
        import cbor2
    except ImportError:
        raise QuotaCheckError(
            "cbor2 library is required for quota checking. "
            "Install it with: pip install cbor2"
        )

    request_body = cbor2.dumps({
        "isEmailRequired": True,
        "origin": "KIRO_IDE",
    })

    headers = {
        "Content-Type": "application/cbor",
        "Accept": "application/cbor",
        "smithy-protocol": "rpc-v2-cbor",
        "Authorization": f"Bearer {access_token}",
        "Cookie": f"Idp={provider}; AccessToken={access_token}",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(KIRO_PORTAL_URL, content=request_body, headers=headers)

    if response.status_code == 401:
        raise QuotaCheckError("Token expired (401). Will retry after token refresh.")
    elif response.status_code == 403:
        raise QuotaCheckError("Token invalid or account suspended (403).")
    elif response.status_code == 429:
        raise QuotaCheckError("Quota check rate limited (429). Will retry later.")
    elif response.status_code != 200:
        raise QuotaCheckError(f"Unexpected status code: {response.status_code}")

    try:
        data = cbor2.loads(response.content)
    except Exception as e:
        raise QuotaCheckError(f"Failed to decode CBOR response: {e}")

    return _parse_usage_response(data)


class QuotaCheckError(Exception):
    """Raised when quota check fails (network error, auth error, etc.)."""
    pass
