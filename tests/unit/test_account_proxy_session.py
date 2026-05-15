# -*- coding: utf-8 -*-
"""Tests for per-account proxy helpers and session-bound account acquisition."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from kiro.account_pool import AccountPool, AccountSlot
from kiro.auth import KiroAuthManager
from kiro.account_proxy import (
    httpx_client_kwargs_for_proxy,
    load_proxy_url_from_account_dir,
    mask_proxy_url,
    normalize_proxy_url,
    save_proxy_url_to_account_dir,
)


def _mock_auth(name: str) -> MagicMock:
    manager = MagicMock(spec=KiroAuthManager)
    manager.auth_type = MagicMock()
    manager.auth_type.value = "kiro_desktop"
    manager.profile_arn = "arn:aws:test"
    manager.fingerprint = f"fingerprint-{name}"
    manager.q_host = "https://q.example.com"
    manager.api_host = "https://api.example.com"
    manager.get_access_token = AsyncMock(return_value=f"token-{name}")
    return manager


def test_normalize_proxy_url_adds_scheme() -> None:
    assert normalize_proxy_url("127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert normalize_proxy_url("socks5://127.0.0.1:1080") == "socks5://127.0.0.1:1080"


def test_mask_proxy_url_hides_userinfo() -> None:
    masked = mask_proxy_url("http://user:secret@proxy.example.com:8080/p")
    assert "***" in masked
    assert "proxy.example.com:8080" in masked


def test_mask_proxy_url_none() -> None:
    assert mask_proxy_url(None) is None
    assert mask_proxy_url("") is None


def test_account_config_roundtrip(tmp_path) -> None:
    d = tmp_path / "acc1"
    d.mkdir()
    save_proxy_url_to_account_dir(d, "socks5://127.0.0.1:1080")
    assert load_proxy_url_from_account_dir(d) == "socks5://127.0.0.1:1080"
    save_proxy_url_to_account_dir(d, None)
    assert load_proxy_url_from_account_dir(d) is None


def test_httpx_client_kwargs_without_proxy() -> None:
    kw = httpx_client_kwargs_for_proxy(timeout=30.0, proxy_url=None)
    assert kw.get("trust_env") is not False


def test_httpx_client_kwargs_with_proxy_sets_trust_env_false() -> None:
    kw = httpx_client_kwargs_for_proxy(timeout=30.0, proxy_url="http://127.0.0.1:9")
    assert kw["trust_env"] is False
    assert "proxy" in kw or "proxies" in kw


class _FakeAdminConfig:
    def __init__(self, controls: dict) -> None:
        self._controls = controls

    def get_model_controls(self) -> dict:
        return self._controls


@pytest.mark.asyncio
async def test_acquire_for_session_reuses_account_after_release(monkeypatch) -> None:
    monkeypatch.setattr("kiro.config.SESSION_STICKY_PRO_MODELS_ONLY", False)
    slots = [
        AccountSlot(name="x", auth_manager=_mock_auth("x")),
        AccountSlot(name="y", auth_manager=_mock_auth("y")),
    ]
    pool = AccountPool(slots, quota_check_interval=0)
    session_id = "same-session-key"
    first = await pool.acquire_for_session(session_id, model_id=None, timeout=2.0)
    name1 = first.name
    await pool.release(first)
    second = await pool.acquire_for_session(session_id, model_id=None, timeout=2.0)
    assert second.name == name1
    await pool.release(second)


@pytest.mark.asyncio
async def test_acquire_for_session_pro_model_only_sticky(monkeypatch) -> None:
    """Default mode: stickiness only for models enabled under KIRO PRO in controls."""
    monkeypatch.setattr("kiro.config.SESSION_STICKY_PRO_MODELS_ONLY", True)
    slots = [
        AccountSlot(name="pro-slot", auth_manager=_mock_auth("pro-slot")),
        AccountSlot(name="other", auth_manager=_mock_auth("other")),
    ]
    pool = AccountPool(slots, quota_check_interval=0)
    pool._admin_config = _FakeAdminConfig(
        {
            "KIRO FREE": {"claude-sonnet-4.5": True},
            "KIRO PRO": {"claude-opus-4.6": True},
        }
    )
    session_id = "claude-session"

    first = await pool.acquire_for_session(
        session_id, model_id="claude-opus-4.6", timeout=2.0
    )
    pro_name = first.name
    await pool.release(first)

    second = await pool.acquire_for_session(
        session_id, model_id="claude-opus-4.6", timeout=2.0
    )
    assert second.name == pro_name
    await pool.release(second)


def test_effective_session_key_pro_models_only(monkeypatch) -> None:
    monkeypatch.setattr("kiro.config.SESSION_STICKY_PRO_MODELS_ONLY", True)
    pool = AccountPool([], quota_check_interval=0)
    pool._admin_config = _FakeAdminConfig(
        {"KIRO PRO": {"claude-opus-4.6": True}, "KIRO FREE": {"claude-sonnet-4.5": True}}
    )
    assert pool._effective_session_key("sess-1", "claude-opus-4.6") == "sess-1"
    assert pool._effective_session_key("sess-1", "claude-sonnet-4.5") is None


def test_effective_session_key_all_models_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr("kiro.config.SESSION_STICKY_PRO_MODELS_ONLY", False)
    pool = AccountPool([], quota_check_interval=0)
    assert pool._effective_session_key("sess-1", "claude-sonnet-4.5") == "sess-1"


@pytest.mark.asyncio
async def test_acquire_for_session_empty_key_uses_fair_acquire() -> None:
    slots = [
        AccountSlot(name="only", auth_manager=_mock_auth("only")),
    ]
    pool = AccountPool(slots, quota_check_interval=0)
    s = await pool.acquire_for_session("", model_id=None, timeout=2.0)
    assert s.name == "only"
    await pool.release(s)
