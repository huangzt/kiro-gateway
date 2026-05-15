# -*- coding: utf-8 -*-
"""Per-account outbound proxy URL: load/save and httpx client kwargs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Union

import inspect

import httpx

ACCOUNT_CONFIG_FILENAME = "account-config.json"


def _httpx_supports_proxy_kwarg() -> bool:
    """Return True if httpx.AsyncClient accepts ``proxy=`` (httpx 0.28+)."""
    try:
        return "proxy" in inspect.signature(httpx.AsyncClient.__init__).parameters
    except (TypeError, ValueError):
        return True


def normalize_proxy_url(url: str) -> str:
    """
    Normalize a proxy URL string (strip; add http:// if scheme missing).

    Args:
        url: Raw proxy URL from config or UI.

    Returns:
        Normalized URL suitable for httpx.
    """
    u = (url or "").strip()
    if not u:
        return ""
    return u if "://" in u else f"http://{u}"


def load_proxy_url_from_account_dir(account_dir: Path) -> Optional[str]:
    """
    Load optional per-account proxy URL from account-config.json.

    Args:
        account_dir: Directory containing kiro-auth-token.json for one account.

    Returns:
        Normalized proxy URL, or None if missing/empty/invalid.
    """
    path = account_dir / ACCOUNT_CONFIG_FILENAME
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("proxy_url") or data.get("VPN_PROXY_URL")
    if raw is None or not isinstance(raw, str):
        return None
    norm = normalize_proxy_url(raw)
    return norm if norm else None


def mask_proxy_url(url: Optional[str]) -> Optional[str]:
    """
    Return a redacted proxy URL for admin/status responses (hide userinfo).

    Args:
        url: Raw proxy URL or None.

    Returns:
        Masked string like ``http://***@host:port``, or None if empty.
    """
    if not url or not str(url).strip():
        return None
    u = str(url).strip()
    if "@" not in u:
        return u
    try:
        scheme, rest = u.split("://", 1)
        if "@" in rest:
            hostpart = rest.split("@", 1)[-1]
            return f"{scheme}://***@{hostpart}"
    except (ValueError, IndexError):
        pass
    return "***"


def save_proxy_url_to_account_dir(account_dir: Path, proxy_url: Optional[str]) -> None:
    """
    Persist per-account proxy URL (or clear it) to account-config.json.

    Merges with existing JSON keys if the file already exists.

    Args:
        account_dir: Account credentials directory.
        proxy_url: Full URL string, or None/empty to remove proxy_url key.

    Raises:
        OSError: If the directory is missing or not writable.
    """
    account_dir = Path(account_dir).expanduser().resolve()
    if not account_dir.is_dir():
        raise OSError(f"Account directory does not exist: {account_dir}")

    path = account_dir / ACCOUNT_CONFIG_FILENAME
    data: Dict[str, Any] = {}
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if isinstance(existing, dict):
                data = dict(existing)
        except (json.JSONDecodeError, OSError):
            data = {}

    if proxy_url and str(proxy_url).strip():
        data["proxy_url"] = normalize_proxy_url(str(proxy_url).strip())
    else:
        data.pop("proxy_url", None)
        data.pop("VPN_PROXY_URL", None)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def httpx_client_kwargs_for_proxy(
    *,
    timeout: Union[float, httpx.Timeout],
    proxy_url: Optional[str],
    follow_redirects: bool = True,
) -> Dict[str, Any]:
    """
    Build kwargs for httpx.AsyncClient respecting optional per-request proxy.

    When proxy_url is set, trust_env is False so process-wide HTTP_PROXY does
    not stack with the explicit proxy.

    Args:
        timeout: httpx timeout configuration.
        proxy_url: Optional normalized proxy URL.
        follow_redirects: Passed through to httpx.

    Returns:
        Keyword arguments for httpx.AsyncClient.
    """
    kwargs: Dict[str, Any] = {
        "timeout": timeout,
        "follow_redirects": follow_redirects,
    }
    if proxy_url:
        kwargs["trust_env"] = False
        if _httpx_supports_proxy_kwarg():
            kwargs["proxy"] = proxy_url
        else:
            kwargs["proxies"] = proxy_url
    return kwargs
