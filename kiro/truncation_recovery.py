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
Truncation recovery system for handling upstream Kiro API limitations.

Generates synthetic messages to inform the model about truncation.
ONLY activates when truncation is actually detected.

This module addresses Issue #56 - Kiro API truncates large tool call payloads
and content mid-stream. Since this is an upstream limitation that cannot be
prevented, we inform the model about the truncation so it can adapt its approach.
"""

from typing import Dict, Any

from loguru import logger


def should_inject_recovery() -> bool:
    """
    Check if truncation recovery is enabled.
    
    Returns:
        True if recovery should be injected, False otherwise
    """
    from kiro.config import TRUNCATION_RECOVERY
    return TRUNCATION_RECOVERY


def generate_truncation_tool_result(
    tool_name: str,
    tool_use_id: str,
    truncation_info: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Generate synthetic tool_result for truncated tool call.

    Message is carefully worded to:
    - Use strong language to prevent retries (UNRECOVERABLE ERROR)
    - Explicitly state DO NOT retry to stop infinite retry loops
    - Provide specific truncation details for debugging
    - Warn about quota waste to discourage retries

    Args:
        tool_name: Name of the truncated tool
        tool_use_id: ID of the truncated tool call
        truncation_info: Diagnostic information about truncation

    Returns:
        Synthetic tool_result in unified format

    Example:
        >>> generate_truncation_tool_result("Write", "call_123", {"size_bytes": 5000, "reason": "missing 2 closing braces"})
        {'type': 'tool_result', 'tool_use_id': 'call_123', 'content': '[UNRECOVERABLE ERROR] ...', 'is_error': True}
    """
    size_bytes = truncation_info.get('size_bytes', 'unknown')
    reason = truncation_info.get('reason', 'unknown')

    content = (
        "[UNRECOVERABLE ERROR] Tool call exceeded Kiro API size limits and was truncated.\n\n"
        f"Truncation details:\n"
        f"- Size: {size_bytes} bytes\n"
        f"- Reason: {reason}\n"
        f"- Tool: {tool_name}\n\n"
        "This operation CANNOT succeed with the current parameters. "
        "The API has hard limits on tool call size.\n\n"
        "DO NOT retry this exact operation. You must either:\n"
        "1. Significantly reduce the operation size (e.g., read/write much smaller chunks)\n"
        "2. Use a completely different approach that avoids large tool calls\n\n"
        "Retrying the same operation will fail again and waste quota."
    )

    logger.error(
        f"[TRUNCATION] Tool '{tool_name}' truncated at {size_bytes} bytes ({reason}). "
        f"This is an UNRECOVERABLE error. Client must change approach."
    )

    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": True
    }


def generate_truncation_user_message() -> str:
    """
    Generate synthetic user message for content truncation.

    Message is carefully worded to:
    - Use strong language to prevent retries (UNRECOVERABLE ERROR)
    - Explicitly state DO NOT retry to stop infinite retry loops
    - Warn about quota waste to discourage retries
    - Provide clear guidance on what to do instead

    Returns:
        Synthetic user message text

    Example:
        >>> generate_truncation_user_message()
        '[UNRECOVERABLE ERROR] Your previous response was truncated...'
    """
    return (
        "[UNRECOVERABLE ERROR] Your previous response was truncated by the API due to "
        "output size limitations.\n\n"
        "DO NOT attempt to continue or complete the truncated response. "
        "You must use a fundamentally different approach that produces shorter output.\n\n"
        "Retrying will fail again and waste quota."
    )
