# -*- coding: utf-8 -*-
"""Tests for format_request_headers_for_log."""

from kiro.utils import format_request_headers_for_log


def test_redacts_authorization_and_api_key() -> None:
    headers = {
        "Authorization": "Bearer secret-token",
        "x-api-key": "sk-secret",
        "X-Custom": "visible",
    }
    out = format_request_headers_for_log(headers)
    assert "secret-token" not in out
    assert "sk-secret" not in out
    assert "***" in out
    assert "visible" in out
    assert "X-Custom" in out


def test_sorts_and_truncates_long_value() -> None:
    headers = {"Z": "z", "A": "x" * 900}
    out = format_request_headers_for_log(headers, value_max_len=100)
    assert "…(truncated)" in out
    lines = out.split("\n")
    assert lines[0].startswith("  A:")


def test_get_sticky_session_key_prefers_kiro_header(monkeypatch) -> None:
    from unittest.mock import MagicMock

    from starlette.datastructures import Headers

    monkeypatch.setattr("kiro.config.KIRO_SESSION_HEADER", "X-Kiro-Session-Id")
    monkeypatch.setattr("kiro.config.CLAUDE_CODE_SESSION_HEADER", "X-Claude-Code-Session-Id")
    req = MagicMock()
    req.headers = Headers(
        raw=[
            (b"x-claude-code-session-id", b"claude-uuid"),
            (b"x-kiro-session-id", b"kiro-uuid"),
        ]
    )
    from kiro.utils import get_sticky_session_key_from_request

    assert get_sticky_session_key_from_request(req) == "kiro-uuid"


def test_get_sticky_session_key_falls_back_to_claude_code(monkeypatch) -> None:
    from unittest.mock import MagicMock

    from starlette.datastructures import Headers

    monkeypatch.setattr("kiro.config.KIRO_SESSION_HEADER", "X-Kiro-Session-Id")
    monkeypatch.setattr("kiro.config.CLAUDE_CODE_SESSION_HEADER", "X-Claude-Code-Session-Id")
    req = MagicMock()
    req.headers = Headers(raw=[(b"x-claude-code-session-id", b"5eac33f7-1dc1-45ad-9688-4a2a8725e255")])
    from kiro.utils import get_sticky_session_key_from_request

    assert get_sticky_session_key_from_request(req) == "5eac33f7-1dc1-45ad-9688-4a2a8725e255"


def test_get_sticky_session_key_discovers_uuid_from_session_like_header(monkeypatch) -> None:
    from unittest.mock import MagicMock

    from starlette.datastructures import Headers

    monkeypatch.setattr("kiro.config.KIRO_SESSION_HEADER", "X-Kiro-Session-Id")
    monkeypatch.setattr("kiro.config.CLAUDE_CODE_SESSION_HEADER", "X-Claude-Code-Session-Id")
    monkeypatch.setattr("kiro.config.SESSION_STICKY_DISCOVER_HEADERS", True)
    uid = "5eac33f7-1dc1-45ad-9688-4a2a8725e255"
    req = MagicMock()
    req.headers = Headers(raw=[(b"x-vendor-chat-session-id", uid.upper().encode("ascii"))])
    from kiro.utils import get_sticky_session_key_from_request

    assert get_sticky_session_key_from_request(req) == uid


def test_get_sticky_session_key_discovery_sorted_by_header_name(monkeypatch) -> None:
    from unittest.mock import MagicMock

    from starlette.datastructures import Headers

    monkeypatch.setattr("kiro.config.KIRO_SESSION_HEADER", "X-Kiro-Session-Id")
    monkeypatch.setattr("kiro.config.CLAUDE_CODE_SESSION_HEADER", "X-Claude-Code-Session-Id")
    monkeypatch.setattr("kiro.config.SESSION_STICKY_DISCOVER_HEADERS", True)
    req = MagicMock()
    req.headers = Headers(
        raw=[
            (b"z-tool-session-id", b"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            (b"a-tool-session-id", b"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        ]
    )
    from kiro.utils import get_sticky_session_key_from_request

    assert get_sticky_session_key_from_request(req) == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def test_get_sticky_session_key_ignores_session_token_header(monkeypatch) -> None:
    from unittest.mock import MagicMock

    from starlette.datastructures import Headers

    monkeypatch.setattr("kiro.config.KIRO_SESSION_HEADER", "X-Kiro-Session-Id")
    monkeypatch.setattr("kiro.config.CLAUDE_CODE_SESSION_HEADER", "X-Claude-Code-Session-Id")
    monkeypatch.setattr("kiro.config.SESSION_STICKY_DISCOVER_HEADERS", True)
    uid = "5eac33f7-1dc1-45ad-9688-4a2a8725e255"
    req = MagicMock()
    req.headers = Headers(raw=[(b"x-session-token", uid.encode("ascii"))])
    from kiro.utils import get_sticky_session_key_from_request

    assert get_sticky_session_key_from_request(req) is None


def test_get_sticky_session_key_discovery_disabled(monkeypatch) -> None:
    from unittest.mock import MagicMock

    from starlette.datastructures import Headers

    monkeypatch.setattr("kiro.config.KIRO_SESSION_HEADER", "X-Kiro-Session-Id")
    monkeypatch.setattr("kiro.config.CLAUDE_CODE_SESSION_HEADER", "X-Claude-Code-Session-Id")
    monkeypatch.setattr("kiro.config.SESSION_STICKY_DISCOVER_HEADERS", False)
    req = MagicMock()
    req.headers = Headers(
        raw=[(b"x-vendor-chat-session-id", b"5eac33f7-1dc1-45ad-9688-4a2a8725e255")]
    )
    from kiro.utils import get_sticky_session_key_from_request

    assert get_sticky_session_key_from_request(req) is None
