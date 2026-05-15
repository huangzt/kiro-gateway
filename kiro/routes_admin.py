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
Admin API routes for Kiro Gateway.

Provides management endpoints for:
- Account pool status monitoring
- Dynamic account management (add/remove/disable/enable)
- Hot-reload configuration changes via gateway.yml
- Real-time log streaming via SSE

All endpoints require X-Admin-Key header matching PROXY_API_KEY.

Security note: Sensitive values (tokens, API keys) are never returned
by these endpoints; they are replaced with '***'.
"""

import asyncio
import io
import json
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Request, Security, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from fastapi.security import APIKeyHeader
from loguru import logger
from pydantic import BaseModel

from kiro.config import PROXY_API_KEY, KIRO_MULTI_CREDS_DIR, PROFILE_ARN, REGION
from kiro.account_pool import AccountPool, AccountSlot
from kiro.admin_config import AdminConfig
from kiro.account_proxy import load_proxy_url_from_account_dir, mask_proxy_url, normalize_proxy_url, save_proxy_url_to_account_dir
from kiro.log_broadcaster import LogBroadcaster


# ---------------------------------------------------------------------------
# Security — same key as main proxy, passed via X-Admin-Key header
# ---------------------------------------------------------------------------

_admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


async def verify_admin_key(key: str = Security(_admin_key_header)) -> bool:
    """
    Verify X-Admin-Key header equals PROXY_API_KEY.

    Args:
        key: Value of X-Admin-Key header

    Returns:
        True if valid

    Raises:
        HTTPException: 401 if key is missing or invalid
    """
    if not key or key != PROXY_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing admin key")
    return True


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/admin", tags=["admin"])

# Sensitive config keys — replace value with '***' in GET /admin/config
_SENSITIVE_KEYS = {
    "PROXY_API_KEY", "REFRESH_TOKEN", "KIRO_CREDS_FILE",
    "KIRO_CLI_DB_FILE", "KIRO_MULTI_CREDS_DIR",
}


# ---------------------------------------------------------------------------
# Admin UI page
# ---------------------------------------------------------------------------

@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def admin_ui():
    """Serve the admin HTML page with cache-busting version."""
    html_path = Path(__file__).parent / "static" / "admin.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="Admin UI not found")

    # Use file modification time as version for cache busting
    js_path = Path(__file__).parent / "static" / "admin.js"
    version = int(js_path.stat().st_mtime) if js_path.exists() else 1

    # Read and inject version into HTML
    html_content = html_path.read_text(encoding="utf-8")
    html_content = html_content.replace(
        'src="/admin-static/admin.js"',
        f'src="/admin-static/admin.js?v={version}"'
    )

    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html_content)


# ---------------------------------------------------------------------------
# Pool status
# ---------------------------------------------------------------------------

@router.get("/status", dependencies=[Depends(verify_admin_key)])
async def get_status(request: Request) -> JSONResponse:
    """
    Return real-time account pool status.

    Returns:
        Pool summary including per-account quota/cooldown/request info
    """
    pool: AccountPool = request.app.state.account_pool
    status = pool.get_status()

    # Attach pool mode info
    admin_cfg: AdminConfig = request.app.state.admin_config
    multi_dir = admin_cfg.get_multi_creds_dir() or KIRO_MULTI_CREDS_DIR
    status["mode"] = "multi" if multi_dir else "single"
    status["multi_creds_dir"] = multi_dir or None

    return JSONResponse(content=status)


# ---------------------------------------------------------------------------
# Account management
# ---------------------------------------------------------------------------

@router.post("/accounts", dependencies=[Depends(verify_admin_key)])
async def add_account(
    request: Request,
    auth_token_file: UploadFile = File(..., description="kiro-auth-token.json"),
    device_reg_file: UploadFile = File(..., description="{hash}.json (Enterprise SSO)"),
) -> JSONResponse:
    """
    Add a new account to the pool by uploading credential files.

    Creates a new subdirectory under KIRO_MULTI_CREDS_DIR with auto-incrementing
    name (account-1, account-2, ...). The new account is immediately
    added to the pool without a server restart.

    Args:
        request: FastAPI request
        auth_token_file: Required kiro-auth-token.json upload
        device_reg_file: Required {hash}.json for Enterprise SSO

    Returns:
        JSON with the new account name and initial status

    Raises:
        HTTPException: 400 if multi-account mode is not configured
        HTTPException: 400 if the uploaded file is not valid JSON
        HTTPException: 422 if required fields are missing from the JSON
    """
    admin_cfg: AdminConfig = request.app.state.admin_config
    multi_dir = admin_cfg.get_multi_creds_dir() or KIRO_MULTI_CREDS_DIR

    if not multi_dir:
        raise HTTPException(
            status_code=400,
            detail=(
                "Multi-account mode is not configured. "
                "Set KIRO_MULTI_CREDS_DIR in .env or configure "
                "accounts.multi_creds_dir in gateway.yml first."
            ),
        )

    base_dir = Path(multi_dir).expanduser().resolve()
    base_dir.mkdir(parents=True, exist_ok=True)

    # Validate auth token file content
    try:
        auth_content = await auth_token_file.read()
        auth_data = json.loads(auth_content)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid JSON in kiro-auth-token.json: {exc}",
        )

    if "refreshToken" not in auth_data and "accessToken" not in auth_data:
        raise HTTPException(
            status_code=422,
            detail="kiro-auth-token.json must contain 'refreshToken' or 'accessToken'",
        )

    # Determine next account directory name
    new_name = _next_account_dir_name(base_dir)
    new_dir = base_dir / new_name
    new_dir.mkdir(parents=True, exist_ok=False)

    # Write auth token file
    (new_dir / "kiro-auth-token.json").write_bytes(auth_content)
    logger.info(f"Written kiro-auth-token.json to {new_dir}")

    # Write optional device registration file
    client_id_override = None
    client_secret_override = None

    if device_reg_file and device_reg_file.filename:
        try:
            dev_content = await device_reg_file.read()
            dev_data = json.loads(dev_content)
        except json.JSONDecodeError as exc:
            shutil.rmtree(new_dir, ignore_errors=True)
            raise HTTPException(
                status_code=400,
                detail=f"Invalid JSON in device registration file: {exc}",
            )

        # Validate filename is a hash.json
        filename = Path(device_reg_file.filename).name
        client_id_hash = auth_data.get("clientIdHash", "")

        if client_id_hash:
            target_filename = f"{client_id_hash}.json"
            (new_dir / target_filename).write_bytes(dev_content)
            client_id_override = dev_data.get("clientId")
            client_secret_override = dev_data.get("clientSecret")
            logger.info(f"Written device registration file {target_filename} to {new_dir}")
        else:
            # Use original filename
            (new_dir / filename).write_bytes(dev_content)
            client_id_override = dev_data.get("clientId")
            client_secret_override = dev_data.get("clientSecret")
            logger.info(f"Written device registration file {filename} to {new_dir}")

    # Create KiroAuthManager and AccountSlot
    from kiro.auth import KiroAuthManager

    proxy_u = load_proxy_url_from_account_dir(new_dir)
    auth_manager = KiroAuthManager(
        profile_arn=PROFILE_ARN if PROFILE_ARN else None,
        region=REGION,
        creds_file=str(new_dir / "kiro-auth-token.json"),
        client_id=client_id_override,
        client_secret=client_secret_override,
        http_proxy_url=proxy_u,
    )

    pool: AccountPool = request.app.state.account_pool
    new_email = await _fetch_email_for_duplicate_check(auth_manager)
    if new_email:
        existing = _find_existing_account_by_email(pool, new_email)
        if existing:
            shutil.rmtree(new_dir, ignore_errors=True)
            raise HTTPException(
                status_code=409,
                detail=(
                    f"账号已存在：邮箱 {new_email} 已绑定账号 '{existing}'，"
                    "无需重复导入。"
                ),
            )

    slot = AccountSlot(name=new_name, auth_manager=auth_manager, proxy_url=proxy_u)

    # Add to pool
    await pool.add_slot(slot)

    # Trigger initial quota check
    try:
        await pool._check_slot_quota(slot)
    except Exception as exc:
        logger.warning(f"Initial quota check for '{new_name}' failed (non-fatal): {exc}")

    logger.info(f"Account '{new_name}' added successfully")
    return JSONResponse(
        content={
            "success": True,
            "account_name": new_name,
            "message": f"Account '{new_name}' added successfully",
            "auth_type": auth_manager.auth_type.value,
        }
    )


class ImportHostCacheRequest(BaseModel):
    """Request body for importing credentials from host SSO cache."""

    delete_source_files: bool = False


class BatchExportAccountsRequest(BaseModel):
    """Request body for batch exporting accounts as a zip file."""

    account_names: List[str]
    delete_exported_accounts: bool = False


class AccountProxyUpdateRequest(BaseModel):
    """Request body for PATCH /admin/accounts/{name}/proxy."""

    proxy_url: Optional[str] = None


class ImportedAccountResult(BaseModel):
    """Import result item for a single account in zip import."""

    source_account: str
    imported_account: Optional[str] = None
    status: str
    reason: Optional[str] = None


@router.post(
    "/accounts/import-host-cache",
    dependencies=[Depends(verify_admin_key)],
)
async def import_host_cache_account(
    request: Request,
    body: ImportHostCacheRequest,
) -> JSONResponse:
    """
    Import account credentials from host SSO cache into the multi-account pool.

    Reads `kiro-auth-token.json` (required) and `{clientIdHash}.json` (optional)
    from `KIRO_HOST_CACHE_DIR`, creates a new account directory, hot-adds it to
    the pool, and optionally deletes source files after successful import.
    """
    admin_cfg: AdminConfig = request.app.state.admin_config
    multi_dir = admin_cfg.get_multi_creds_dir() or KIRO_MULTI_CREDS_DIR
    if not multi_dir:
        raise HTTPException(
            status_code=400,
            detail=(
                "Multi-account mode is not configured. "
                "Set KIRO_MULTI_CREDS_DIR in .env or configure "
                "accounts.multi_creds_dir in gateway.yml first."
            ),
        )

    from kiro.config import KIRO_HOST_CACHE_DIR

    if not KIRO_HOST_CACHE_DIR:
        raise HTTPException(
            status_code=400,
            detail=(
                "KIRO_HOST_CACHE_DIR is not configured in .env. "
                "Setup volume mapping first."
            ),
        )

    host_cache_dir = Path(KIRO_HOST_CACHE_DIR).expanduser().resolve()
    if not host_cache_dir.exists() or not host_cache_dir.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"Host cache directory not found: {host_cache_dir}",
        )

    auth_path, device_reg_path, _ = _resolve_host_cache_files(host_cache_dir)

    base_dir = Path(multi_dir).expanduser().resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    new_name = _next_account_dir_name(base_dir)
    new_dir = base_dir / new_name
    new_dir.mkdir(parents=True, exist_ok=False)

    copied_files: list[str] = []
    deleted_files: list[str] = []

    try:
        target_auth_path = new_dir / "kiro-auth-token.json"
        shutil.copy2(str(auth_path), str(target_auth_path))
        copied_files.append(target_auth_path.name)

        client_id_override = None
        client_secret_override = None

        if device_reg_path:
            target_device_path = new_dir / device_reg_path.name
            shutil.copy2(str(device_reg_path), str(target_device_path))
            copied_files.append(target_device_path.name)
            with open(device_reg_path, "r", encoding="utf-8") as f:
                dev_data = json.load(f)
            client_id_override = dev_data.get("clientId")
            client_secret_override = dev_data.get("clientSecret")

        from kiro.auth import KiroAuthManager

        proxy_u = load_proxy_url_from_account_dir(new_dir)
        auth_manager = KiroAuthManager(
            profile_arn=PROFILE_ARN if PROFILE_ARN else None,
            region=REGION,
            creds_file=str(target_auth_path),
            client_id=client_id_override,
            client_secret=client_secret_override,
            http_proxy_url=proxy_u,
        )

        pool: AccountPool = request.app.state.account_pool
        new_email = await _fetch_email_for_duplicate_check(auth_manager)
        if new_email:
            existing = _find_existing_account_by_email(pool, new_email)
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"账号已存在：邮箱 {new_email} 已绑定账号 '{existing}'，"
                        "无需重复导入。"
                    ),
                )

        slot = AccountSlot(name=new_name, auth_manager=auth_manager, proxy_url=proxy_u)
        await pool.add_slot(slot)

        try:
            await pool._check_slot_quota(slot)
        except Exception as exc:
            logger.warning(f"Initial quota check for '{new_name}' failed (non-fatal): {exc}")

        if body.delete_source_files:
            deleted_files = _delete_host_cache_files(auth_path, device_reg_path)

    except Exception:
        shutil.rmtree(new_dir, ignore_errors=True)
        raise

    logger.info(
        "Imported host cache credentials as account '{}' (copied={}, deleted={})",
        new_name,
        copied_files,
        deleted_files,
    )
    return JSONResponse(
        content={
            "success": True,
            "account_name": new_name,
            "copied_files": copied_files,
            "deleted_source_files": deleted_files,
            "message": f"Account '{new_name}' imported from host cache successfully",
        }
    )


@router.post(
    "/accounts/export-zip",
    dependencies=[Depends(verify_admin_key)],
)
async def export_accounts_zip(
    request: Request,
    body: BatchExportAccountsRequest,
) -> Response:
    """
    Export selected account credential directories as a ZIP archive.

    Optionally deletes exported accounts after a successful ZIP build.
    """
    admin_cfg: AdminConfig = request.app.state.admin_config
    multi_dir = admin_cfg.get_multi_creds_dir() or KIRO_MULTI_CREDS_DIR
    if not multi_dir:
        raise HTTPException(status_code=400, detail="Multi-account mode not configured")

    names = [n.strip() for n in body.account_names if n and n.strip()]
    if not names:
        raise HTTPException(status_code=400, detail="请至少选择一个账号")

    unique_names = list(dict.fromkeys(names))
    pool: AccountPool = request.app.state.account_pool
    base_dir = Path(multi_dir).expanduser().resolve()

    account_dirs: list[Path] = []
    for name in unique_names:
        slot = pool.get_slot_by_name(name)
        if slot is None:
            raise HTTPException(status_code=404, detail=f"Account '{name}' not found")
        if slot.active_requests > 0:
            raise HTTPException(
                status_code=409,
                detail=f"Account '{name}' has active requests and cannot be exported/deleted now.",
            )
        account_dir = base_dir / name
        if not account_dir.exists():
            raise HTTPException(status_code=404, detail=f"Account directory not found: {name}")
        account_dirs.append(account_dir)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, account_dir in zip(unique_names, account_dirs):
            json_files = sorted([f for f in account_dir.glob("*.json") if f.is_file()])
            if not json_files:
                logger.warning(f"No json files found for account '{name}' during export")
            for file_path in json_files:
                arcname = f"{name}/{file_path.name}"
                zf.write(file_path, arcname=arcname)

    if body.delete_exported_accounts:
        for name, account_dir in zip(unique_names, account_dirs):
            removed = await pool.remove_slot(name)
            if not removed:
                raise HTTPException(
                    status_code=409,
                    detail=f"Failed to remove '{name}' from pool during delete-after-export",
                )
            if account_dir.exists():
                shutil.rmtree(account_dir)
            admin_cfg.remove_disabled_account(name)
        await admin_cfg.save()

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"kiro-accounts-{ts}.zip"
    zip_bytes = zip_buffer.getvalue()
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=zip_bytes, media_type="application/zip", headers=headers)


@router.post(
    "/accounts/import-zip",
    dependencies=[Depends(verify_admin_key)],
)
async def import_accounts_zip(
    request: Request,
    zip_file: UploadFile = File(..., description="ZIP exported from /admin/accounts/export-zip"),
) -> JSONResponse:
    """
    Import accounts from exported ZIP archive.

    ZIP layout is expected as:
        account-1/kiro-auth-token.json
        account-1/{clientIdHash}.json (optional)
        account-2/...
    """
    admin_cfg: AdminConfig = request.app.state.admin_config
    multi_dir = admin_cfg.get_multi_creds_dir() or KIRO_MULTI_CREDS_DIR
    if not multi_dir:
        raise HTTPException(
            status_code=400,
            detail=(
                "Multi-account mode is not configured. "
                "Set KIRO_MULTI_CREDS_DIR in .env or configure "
                "accounts.multi_creds_dir in gateway.yml first."
            ),
        )

    filename = zip_file.filename or ""
    if not filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="请选择 .zip 压缩包文件")

    try:
        zip_bytes = await zip_file.read()
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            account_files = _collect_account_files_from_zip(zf)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="ZIP 文件损坏或格式不正确")

    if not account_files:
        raise HTTPException(status_code=400, detail="ZIP 中未找到可导入的账号文件")

    base_dir = Path(multi_dir).expanduser().resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    pool: AccountPool = request.app.state.account_pool

    imported: list[ImportedAccountResult] = []
    skipped: list[ImportedAccountResult] = []

    source_accounts = sorted(account_files.keys(), key=_account_dir_name_sort_key)
    for source_name in source_accounts:
        files_map = account_files[source_name]
        auth_content = files_map.get("kiro-auth-token.json")
        if not auth_content:
            skipped.append(
                ImportedAccountResult(
                    source_account=source_name,
                    status="skipped",
                    reason="缺少 kiro-auth-token.json",
                )
            )
            continue

        try:
            auth_data = json.loads(auth_content)
        except json.JSONDecodeError:
            skipped.append(
                ImportedAccountResult(
                    source_account=source_name,
                    status="skipped",
                    reason="kiro-auth-token.json 不是有效 JSON",
                )
            )
            continue

        if "refreshToken" not in auth_data and "accessToken" not in auth_data:
            skipped.append(
                ImportedAccountResult(
                    source_account=source_name,
                    status="skipped",
                    reason="kiro-auth-token.json 缺少 refreshToken/accessToken",
                )
            )
            continue

        client_id_override = None
        client_secret_override = None
        device_filename = None
        device_content = None
        client_id_hash = auth_data.get("clientIdHash")
        if client_id_hash:
            candidate = f"{client_id_hash}.json"
            if candidate in files_map:
                device_filename = candidate
                device_content = files_map[candidate]
                try:
                    dev_data = json.loads(device_content)
                    client_id_override = dev_data.get("clientId")
                    client_secret_override = dev_data.get("clientSecret")
                except json.JSONDecodeError:
                    skipped.append(
                        ImportedAccountResult(
                            source_account=source_name,
                            status="skipped",
                            reason=f"{candidate} 不是有效 JSON",
                        )
                    )
                    continue

        new_name = _next_account_dir_name(base_dir)
        new_dir = base_dir / new_name
        new_dir.mkdir(parents=True, exist_ok=False)
        try:
            target_auth = new_dir / "kiro-auth-token.json"
            target_auth.write_bytes(auth_content)
            if device_filename and device_content:
                (new_dir / device_filename).write_bytes(device_content)

            from kiro.auth import KiroAuthManager

            proxy_u = load_proxy_url_from_account_dir(new_dir)
            auth_manager = KiroAuthManager(
                profile_arn=PROFILE_ARN if PROFILE_ARN else None,
                region=REGION,
                creds_file=str(target_auth),
                client_id=client_id_override,
                client_secret=client_secret_override,
                http_proxy_url=proxy_u,
            )

            new_email = await _fetch_email_for_duplicate_check(auth_manager)
            if new_email:
                existing = _find_existing_account_by_email(pool, new_email)
                if existing:
                    skipped.append(
                        ImportedAccountResult(
                            source_account=source_name,
                            status="skipped",
                            reason=f"邮箱 {new_email} 已存在于账号 {existing}",
                        )
                    )
                    shutil.rmtree(new_dir, ignore_errors=True)
                    continue

            slot = AccountSlot(name=new_name, auth_manager=auth_manager, proxy_url=proxy_u)
            await pool.add_slot(slot)
            try:
                await pool._check_slot_quota(slot)
            except Exception as exc:
                logger.warning(f"Initial quota check for '{new_name}' failed (non-fatal): {exc}")

            imported.append(
                ImportedAccountResult(
                    source_account=source_name,
                    imported_account=new_name,
                    status="imported",
                )
            )
        except Exception:
            shutil.rmtree(new_dir, ignore_errors=True)
            raise

    return JSONResponse(
        content={
            "success": True,
            "total": len(source_accounts),
            "imported_count": len(imported),
            "skipped_count": len(skipped),
            "imported": [item.model_dump() for item in imported],
            "skipped": [item.model_dump() for item in skipped],
        }
    )


@router.post(
    "/accounts/{account_name}/quota-refresh",
    dependencies=[Depends(verify_admin_key)],
)
async def refresh_quota(account_name: str, request: Request) -> JSONResponse:
    """
    Manually trigger a quota refresh for a specific account.

    Args:
        account_name: The account directory name
        request: FastAPI request

    Returns:
        Updated quota information

    Raises:
        HTTPException: 404 if account not found
    """
    pool: AccountPool = request.app.state.account_pool
    slot = pool.get_slot_by_name(account_name)
    if slot is None:
        raise HTTPException(status_code=404, detail=f"Account '{account_name}' not found")

    await pool._check_slot_quota(slot)

    quota = None
    if slot.quota_info:
        quota = {
            "used": slot.quota_info.total_used,
            "limit": slot.quota_info.total_limit,
            "remaining": slot.quota_info.remaining,
            "plan": slot.quota_info.subscription_plan,
            "next_reset": slot.quota_info.next_reset,
            "trial_expiry": slot.quota_info.trial_expiry,
        }

    return JSONResponse(
        content={
            "success": True,
            "account_name": account_name,
            "quota": quota,
            "is_exhausted": slot.is_exhausted,
        }
    )


@router.patch(
    "/accounts/{account_name}/proxy",
    dependencies=[Depends(verify_admin_key)],
)
async def update_account_proxy(
    account_name: str,
    body: AccountProxyUpdateRequest,
    request: Request,
) -> JSONResponse:
    """
    Set or clear per-account outbound HTTP proxy (persisted to account-config.json).

    Same URL format as VPN_PROXY_URL (http, https, socks5). Empty string clears.
    """
    pool: AccountPool = request.app.state.account_pool
    slot = pool.get_slot_by_name(account_name)
    if slot is None:
        raise HTTPException(status_code=404, detail=f"Account '{account_name}' not found")

    creds_dir = slot.auth_manager.creds_dir
    if creds_dir is None or not creds_dir.is_dir():
        raise HTTPException(
            status_code=400,
            detail="Account has no credentials directory on disk; cannot store proxy.",
        )

    raw = body.proxy_url
    if raw is None:
        new_proxy: Optional[str] = None
    else:
        stripped = raw.strip()
        new_proxy = normalize_proxy_url(stripped) if stripped else None

    save_proxy_url_to_account_dir(creds_dir, new_proxy)
    slot.proxy_url = new_proxy
    slot.auth_manager.set_http_proxy_url(new_proxy)
    logger.info(f"Updated outbound proxy for account '{account_name}' (enabled={bool(new_proxy)})")

    return JSONResponse(
        content={
            "success": True,
            "account_name": account_name,
            "proxy_url": mask_proxy_url(new_proxy),
        }
    )


@router.post(
    "/accounts/quota-refresh-all",
    dependencies=[Depends(verify_admin_key)],
)
async def refresh_all_quotas(request: Request) -> JSONResponse:
    """
    Manually trigger quota refresh for all accounts in the pool.

    This endpoint:
    1. Rescans the multi-account directory for new accounts (if configured)
    2. Refreshes quota information for all accounts concurrently

    This allows users to add new account directories manually and have them
    automatically discovered without restarting the server.

    Args:
        request: FastAPI request

    Returns:
        Summary of refresh results including success/failure counts

    Example response:
        {
            "success": true,
            "total": 5,
            "refreshed": 4,
            "failed": 1,
            "new_accounts": 2,
            "results": [
                {"account": "account-1", "success": true},
                {"account": "account-2", "success": false, "error": "Network timeout"}
            ]
        }
    """
    pool: AccountPool = request.app.state.account_pool
    admin_cfg: AdminConfig = request.app.state.admin_config
    multi_dir = admin_cfg.get_multi_creds_dir() or KIRO_MULTI_CREDS_DIR

    new_accounts_count = 0

    # Step 1: Rescan directory for new accounts (only in multi-account mode)
    if multi_dir:
        from pathlib import Path
        import json
        from kiro.auth import KiroAuthManager
        from kiro.config import PROFILE_ARN, REGION

        dir_path = Path(multi_dir).expanduser().resolve()
        if dir_path.exists() and dir_path.is_dir():
            # Get existing account names
            existing_names = {slot.name for slot in pool.slots}

            # Scan for new subdirectories
            subdirs = sorted([d for d in dir_path.iterdir() if d.is_dir()])
            for subdir in subdirs:
                account_name = subdir.name

                # Skip if already exists
                if account_name in existing_names:
                    continue

                creds_file = subdir / "kiro-auth-token.json"
                if not creds_file.exists():
                    logger.debug(f"Skipping '{account_name}': missing kiro-auth-token.json")
                    continue

                # Validate credentials file
                try:
                    with open(creds_file, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    if "refreshToken" not in data and "accessToken" not in data:
                        logger.debug(f"Skipping '{account_name}': no refreshToken or accessToken")
                        continue

                    # Handle Enterprise SSO device registration
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
                            except Exception as e:
                                logger.warning(f"Failed to load device registration for '{account_name}': {e}")

                    # Create auth manager and slot
                    proxy_u = load_proxy_url_from_account_dir(subdir)
                    auth_manager = KiroAuthManager(
                        profile_arn=PROFILE_ARN if PROFILE_ARN else None,
                        region=REGION,
                        creds_file=str(creds_file),
                        client_id=client_id_override,
                        client_secret=client_secret_override,
                        http_proxy_url=proxy_u,
                    )

                    slot = AccountSlot(name=account_name, auth_manager=auth_manager, proxy_url=proxy_u)

                    # Add to pool
                    await pool.add_slot(slot)
                    new_accounts_count += 1
                    logger.info(f"Auto-discovered and added account: {account_name}")

                except Exception as e:
                    logger.warning(f"Failed to add account '{account_name}': {e}")
                    continue

    # Step 2: Refresh all quotas (including newly added accounts)
    all_slots = pool.slots
    if not all_slots:
        return JSONResponse(
            content={
                "success": True,
                "total": 0,
                "refreshed": 0,
                "failed": 0,
                "new_accounts": new_accounts_count,
                "results": [],
            }
        )

    # Refresh all quotas concurrently
    results = []
    tasks = []

    async def refresh_single(slot: AccountSlot) -> dict:
        """Refresh a single slot and return result."""
        try:
            await pool._check_slot_quota(slot)
            return {"account": slot.name, "success": True}
        except Exception as exc:
            logger.warning(f"Failed to refresh quota for '{slot.name}': {exc}")
            return {"account": slot.name, "success": False, "error": str(exc)}

    # Create tasks for all slots
    for slot in all_slots:
        tasks.append(refresh_single(slot))

    # Execute all refreshes concurrently
    results = await asyncio.gather(*tasks, return_exceptions=False)

    # Count successes and failures
    refreshed = sum(1 for r in results if r.get("success"))
    failed = len(results) - refreshed

    return JSONResponse(
        content={
            "success": True,
            "total": len(results),
            "refreshed": refreshed,
            "failed": failed,
            "new_accounts": new_accounts_count,
            "results": results,
        }
    )


@router.post(
    "/accounts/{account_name}/cooldown-clear",
    dependencies=[Depends(verify_admin_key)],
)
async def clear_cooldown(account_name: str, request: Request) -> JSONResponse:
    """
    Immediately clear the cooldown for an account, making it available.

    Args:
        account_name: The account directory name
        request: FastAPI request

    Returns:
        Success status

    Raises:
        HTTPException: 404 if account not found
        HTTPException: 400 if account is not in cooldown
    """
    pool: AccountPool = request.app.state.account_pool
    success = await pool.clear_cooldown(account_name)
    if not success:
        slot = pool.get_slot_by_name(account_name)
        if slot is None:
            raise HTTPException(status_code=404, detail=f"Account '{account_name}' not found")
        raise HTTPException(
            status_code=400,
            detail=f"Account '{account_name}' is not in cooldown",
        )

    return JSONResponse(content={"success": True, "account_name": account_name})


@router.post(
    "/accounts/{account_name}/disable",
    dependencies=[Depends(verify_admin_key)],
)
async def disable_account(account_name: str, request: Request) -> JSONResponse:
    """
    Disable an account slot (hot-update, no restart required).

    The account is removed from the active queue but remains in pool
    memory. Its disabled status is persisted to gateway.yml.

    Args:
        account_name: The account directory name
        request: FastAPI request

    Returns:
        Success status

    Raises:
        HTTPException: 404 if account not found
        HTTPException: 400 if already disabled
    """
    pool: AccountPool = request.app.state.account_pool
    success = await pool.disable_slot(account_name)
    if not success:
        slot = pool.get_slot_by_name(account_name)
        if slot is None:
            raise HTTPException(status_code=404, detail=f"Account '{account_name}' not found")
        raise HTTPException(
            status_code=400,
            detail=f"Account '{account_name}' is already disabled",
        )

    # Persist disabled state to gateway.yml
    admin_cfg: AdminConfig = request.app.state.admin_config
    admin_cfg.add_disabled_account(account_name)
    await admin_cfg.save()

    return JSONResponse(content={"success": True, "account_name": account_name})


@router.post(
    "/accounts/{account_name}/enable",
    dependencies=[Depends(verify_admin_key)],
)
async def enable_account(account_name: str, request: Request) -> JSONResponse:
    """
    Re-enable a previously disabled account slot (hot-update).

    The account is re-inserted into the active queue. Its enabled
    status is persisted by removing it from gateway.yml disabled list.

    Args:
        account_name: The account directory name
        request: FastAPI request

    Returns:
        Success status

    Raises:
        HTTPException: 404 if account not found
        HTTPException: 400 if not disabled
    """
    pool: AccountPool = request.app.state.account_pool
    success = await pool.enable_slot(account_name)
    if not success:
        slot = pool.get_slot_by_name(account_name)
        if slot is None:
            raise HTTPException(status_code=404, detail=f"Account '{account_name}' not found")
        raise HTTPException(
            status_code=400,
            detail=f"Account '{account_name}' is not disabled",
        )

    # Remove from gateway.yml disabled list
    admin_cfg: AdminConfig = request.app.state.admin_config
    admin_cfg.remove_disabled_account(account_name)
    await admin_cfg.save()

    return JSONResponse(content={"success": True, "account_name": account_name})


@router.delete(
    "/accounts/{account_name}",
    dependencies=[Depends(verify_admin_key)],
)
async def delete_account(account_name: str, request: Request) -> JSONResponse:
    """
    Delete an account: removes from pool and deletes its directory.

    Refuses to delete if the account has active requests.
    This operation is irreversible.

    Args:
        account_name: The account directory name
        request: FastAPI request

    Returns:
        Success status

    Raises:
        HTTPException: 404 if account not found
        HTTPException: 409 if account has active requests
        HTTPException: 400 if multi-account mode not configured
    """
    admin_cfg: AdminConfig = request.app.state.admin_config
    multi_dir = admin_cfg.get_multi_creds_dir() or KIRO_MULTI_CREDS_DIR

    if not multi_dir:
        raise HTTPException(status_code=400, detail="Multi-account mode not configured")

    pool: AccountPool = request.app.state.account_pool
    slot = pool.get_slot_by_name(account_name)
    if slot is None:
        raise HTTPException(status_code=404, detail=f"Account '{account_name}' not found")

    if slot.active_requests > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Account '{account_name}' has {slot.active_requests} active request(s). "
                "Disable it first and wait for requests to complete."
            ),
        )

    # Remove from pool
    removed = await pool.remove_slot(account_name)
    if not removed:
        raise HTTPException(
            status_code=409,
            detail=f"Failed to remove '{account_name}' from pool",
        )

    # Delete directory
    account_dir = Path(multi_dir).expanduser().resolve() / account_name
    if account_dir.exists():
        shutil.rmtree(account_dir)
        logger.info(f"Deleted account directory: {account_dir}")
    else:
        logger.warning(f"Account directory not found on disk: {account_dir}")

    # Remove from disabled list if present
    admin_cfg.remove_disabled_account(account_name)
    await admin_cfg.save()

    return JSONResponse(
        content={
            "success": True,
            "account_name": account_name,
            "message": f"Account '{account_name}' deleted",
        }
    )


@router.get("/accounts/{account_name}/files", dependencies=[Depends(verify_admin_key)])
async def list_account_files(account_name: str, request: Request) -> JSONResponse:
    """
    List downloadable credential files for an account.

    Args:
        account_name: The account directory name
        request: FastAPI request

    Returns:
        List of JSON files found in the account directory
    """
    admin_cfg: AdminConfig = request.app.state.admin_config
    multi_dir = admin_cfg.get_multi_creds_dir() or KIRO_MULTI_CREDS_DIR
    if not multi_dir:
        raise HTTPException(status_code=400, detail="Multi-account mode not configured")

    account_dir = Path(multi_dir).expanduser().resolve() / account_name
    if not account_dir.exists():
        raise HTTPException(status_code=404, detail="Account directory not found")

    files = []
    for f in account_dir.glob("*.json"):
        if f.is_file():
            files.append(f.name)

    return JSONResponse(content={"account": account_name, "files": sorted(files)})


@router.get("/accounts/{account_name}/files/{filename}", dependencies=[Depends(verify_admin_key)])
async def download_account_file(account_name: str, filename: str, request: Request):
    """
    Download a specific credential file from an account directory.

    Args:
        account_name: The account directory name
        filename: Name of the file to download
        request: FastAPI request
    """
    admin_cfg: AdminConfig = request.app.state.admin_config
    multi_dir = admin_cfg.get_multi_creds_dir() or KIRO_MULTI_CREDS_DIR
    if not multi_dir:
        raise HTTPException(status_code=400, detail="Multi-account mode not configured")

    account_dir = Path(multi_dir).expanduser().resolve() / account_name
    file_path = (account_dir / filename).resolve()

    # Security check: ensure path is within account_dir to prevent directory traversal
    if not file_path.is_relative_to(account_dir.resolve()):
        raise HTTPException(status_code=403, detail="Access denied")

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        str(file_path),
        filename=filename,
        media_type="application/json"
    )


@router.post(
    "/accounts/{account_name}/switch",
    dependencies=[Depends(verify_admin_key)],
)
async def switch_account(account_name: str, request: Request) -> JSONResponse:
    """
    Switch the host's active Kiro account to the specified account.
    
    This copies the account's credentials to the host cache directory 
    mapped via KIRO_HOST_CACHE_DIR.
    
    Args:
        account_name: The account directory name
        request: FastAPI request
        
    Returns:
        Success status
        
    Raises:
        HTTPException: 404 if account not found
        HTTPException: 400 if KIRO_HOST_CACHE_DIR is not configured
        HTTPException: 500 if file copy fails
    """
    pool: AccountPool = request.app.state.account_pool
    success = pool.switch_to_account(account_name)
    
    if not success:
        from kiro.config import KIRO_HOST_CACHE_DIR
        if not KIRO_HOST_CACHE_DIR:
            raise HTTPException(
                status_code=400, 
                detail="KIRO_HOST_CACHE_DIR is not configured in .env. Setup volume mapping first."
            )
        
        slot = pool.get_slot_by_name(account_name)
        if slot is None:
            raise HTTPException(status_code=404, detail=f"Account '{account_name}' not found")
            
        raise HTTPException(
            status_code=500,
            detail=f"Failed to switch to account '{account_name}'. Check server logs for details."
        )

    return JSONResponse(
        content={
            "success": True, 
            "account_name": account_name,
            "message": f"Successfully switched host account to '{account_name}'"
        }
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@router.get("/config", dependencies=[Depends(verify_admin_key)])
async def get_config(request: Request) -> JSONResponse:
    """
    Return current runtime configuration.

    Sensitive values (tokens, API keys) are replaced with '***'.
    Returns three sections:
    - gateway_yml: raw values stored in gateway.yml (may be null = use .env)
    - effective: actual running values after gateway.yml overrides .env
    - env_config: static .env values (read-only, secrets masked)

    Returns:
        Configuration grouped by section, with source labels
    """
    import kiro.config as cfg_module

    admin_cfg: AdminConfig = request.app.state.admin_config
    yml_config = admin_cfg.get_full_config()

    # Mask sensitive tokens in yml_config
    if yml_config.get("accounts", {}).get("refresh_token"):
        yml_config["accounts"]["refresh_token"] = "***"

    # Effective running values (gateway.yml overrides merged on top of .env)
    effective = {
        "pool": {
            "cooldown_seconds": getattr(cfg_module, "COOLDOWN_SECONDS", 300),
            "queue_timeout": getattr(cfg_module, "QUEUE_TIMEOUT", 300),
            "quota_check_interval": getattr(cfg_module, "QUOTA_CHECK_INTERVAL", 300),
        },
        "timeout": {
            "first_token_timeout": getattr(cfg_module, "FIRST_TOKEN_TIMEOUT", 15),
            "first_token_max_retries": getattr(cfg_module, "FIRST_TOKEN_MAX_RETRIES", 3),
            "streaming_read_timeout": getattr(cfg_module, "STREAMING_READ_TIMEOUT", 300),
        },
        "reasoning": {
            "fake_reasoning": admin_cfg.get("reasoning", "fake_reasoning") if admin_cfg.get("reasoning", "fake_reasoning") is not None else getattr(cfg_module, "FAKE_REASONING_ENABLED", True),
            "fake_reasoning_max_tokens": admin_cfg.get("reasoning", "fake_reasoning_max_tokens") if admin_cfg.get("reasoning", "fake_reasoning_max_tokens") is not None else getattr(cfg_module, "FAKE_REASONING_MAX_TOKENS", 4000),
            "fake_reasoning_handling": admin_cfg.get("reasoning", "fake_reasoning_handling") if admin_cfg.get("reasoning", "fake_reasoning_handling") is not None else getattr(cfg_module, "FAKE_REASONING_HANDLING", "as_reasoning_content"),
        },
        "logging": {
            "log_level": getattr(cfg_module, "LOG_LEVEL", "INFO"),
            "debug_mode": getattr(cfg_module, "DEBUG_MODE", "off"),
            "log_history_size": getattr(request.app.state, "log_broadcaster", None) and
                                request.app.state.log_broadcaster.history_size or 200,
        },
        "accounts": {
            "multi_creds_dir": admin_cfg.get_multi_creds_dir() or getattr(cfg_module, "KIRO_MULTI_CREDS_DIR", None) or "",
            "disabled": admin_cfg.get_disabled_accounts(),
            "refresh_token": "***" if (admin_cfg.get("accounts", "refresh_token") or getattr(cfg_module, "REFRESH_TOKEN", None)) else "",
            "kiro_creds_file": admin_cfg.get("accounts", "kiro_creds_file") or getattr(cfg_module, "KIRO_CREDS_FILE", None) or "",
            "kiro_cli_db_file": admin_cfg.get("accounts", "kiro_cli_db_file") or getattr(cfg_module, "KIRO_CLI_DB_FILE", None) or "",
            "profile_arn": admin_cfg.get("accounts", "profile_arn") or getattr(cfg_module, "PROFILE_ARN", None) or "",
            "region": admin_cfg.get("accounts", "region") or getattr(cfg_module, "REGION", "us-east-1"),
        },
    }

    # Build env config (read-only, sensitive masked)
    env_config = {
        "server": {
            "SERVER_HOST": getattr(cfg_module, "SERVER_HOST", "0.0.0.0"),
            "SERVER_PORT": getattr(cfg_module, "SERVER_PORT", 8000),
            "PROXY_API_KEY": "***",
        },
        "proxy": {
            "VPN_PROXY_URL": getattr(cfg_module, "VPN_PROXY_URL", None) or "(not set)",
        },
    }

    return JSONResponse(
        content={
            "gateway_yml": yml_config,
            "effective": effective,
            "env_config": env_config,
        }
    )



@router.patch("/config", dependencies=[Depends(verify_admin_key)])
async def update_config(request: Request) -> JSONResponse:
    """
    Update hot-reload configuration parameters.

    Writes changes to gateway.yml and applies them immediately to the
    running server (no restart required).

    Special handling for:
    - log_history_size: resizes the log broadcaster ring buffer
    - accounts.multi_creds_dir: triggers pool reinitialization
    - accounts.disabled: syncs disabled state with pool

    Request body example:
        {
            "pool": {"cooldown_seconds": 120},
            "logging": {"log_level": "DEBUG", "log_history_size": 500}
        }

    Returns:
        Updated configuration sections

    Raises:
        HTTPException: 400 if unknown section or key
        HTTPException: 422 if value type is invalid
    """
    body: Dict[str, Any] = await request.json()

    admin_cfg: AdminConfig = request.app.state.admin_config
    pool: AccountPool = request.app.state.account_pool

    # Whitelist of allowed hot-reload sections/keys
    _ALLOWED_KEYS: Dict[str, set] = {
        "pool": {"cooldown_seconds", "queue_timeout", "quota_check_interval"},
        "timeout": {"first_token_timeout", "first_token_max_retries", "streaming_read_timeout"},
        "reasoning": {"fake_reasoning", "fake_reasoning_max_tokens", "fake_reasoning_handling"},
        "logging": {"log_level", "debug_mode", "log_history_size"},
        "accounts": {"disabled", "multi_creds_dir", "refresh_token", "kiro_creds_file", "kiro_cli_db_file", "profile_arn", "region"},
    }

    validated: Dict[str, Dict] = {}
    need_pool_reinit = False

    for section, values in body.items():
        if section not in _ALLOWED_KEYS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown configuration section: '{section}'",
            )
        if not isinstance(values, dict):
            raise HTTPException(
                status_code=422,
                detail=f"Section '{section}' must be a JSON object",
            )
        validated[section] = {}
        for key, value in values.items():
            if key not in _ALLOWED_KEYS[section]:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown configuration key: '{section}.{key}'",
                )
            # Ignore masked values sent back from frontend
            if value == "***":
                continue

            # Check if auth fields changed — need pool reinit
            if section == "accounts" and key in ("multi_creds_dir", "refresh_token", "kiro_creds_file", "kiro_cli_db_file", "profile_arn", "region"):
                old_val = admin_cfg.get(section, key)
                if old_val != value:
                    need_pool_reinit = True
            validated[section][key] = value

    # Apply updates in memory + save
    admin_cfg.apply_updates(validated)
    await admin_cfg.save()

    # Apply immediate side-effects -------------------------------------------

    # Resize log history buffer if changed
    new_history_size = validated.get("logging", {}).get("log_history_size")
    if new_history_size is not None:
        broadcaster: Optional[LogBroadcaster] = getattr(request.app.state, "log_broadcaster", None)
        if broadcaster:
            try:
                broadcaster.resize(int(new_history_size))
            except (ValueError, TypeError) as exc:
                logger.warning(f"Failed to resize log buffer: {exc}")



    # Apply memory overrides to global config so they take effect immediately
    import kiro.config as cfg_module
    _TYPE_MAP = {
        "cooldown_seconds": (float, "COOLDOWN_SECONDS"),
        "queue_timeout": (float, "QUEUE_TIMEOUT"),
        "quota_check_interval": (float, "QUOTA_CHECK_INTERVAL"),
        "first_token_timeout": (int, "FIRST_TOKEN_TIMEOUT"),
        "first_token_max_retries": (int, "FIRST_TOKEN_MAX_RETRIES"),
        "streaming_read_timeout": (float, "STREAMING_READ_TIMEOUT"),
        "fake_reasoning": (bool, "FAKE_REASONING_ENABLED"),
        "fake_reasoning_max_tokens": (int, "FAKE_REASONING_MAX_TOKENS"),
        "fake_reasoning_handling": (str, "FAKE_REASONING_HANDLING"),
        "log_level": (str, "LOG_LEVEL"),
        "debug_mode": (str, "DEBUG_MODE"),
    }

    pool: AccountPool = getattr(request.app.state, "account_pool", None)
    
    for section, fields in validated.items():
        if section == "accounts": continue
        for key, val in fields.items():
            if key in _TYPE_MAP and val is not None:
                typ, target_attr = _TYPE_MAP[key]
                try:
                    # Update global module namespace
                    setattr(cfg_module, target_attr, typ(val))
                    # Also update pool specific active attributes if applicable
                    if key == "quota_check_interval" and pool:
                        pool._quota_check_interval = float(val)
                except (ValueError, TypeError):
                    pass

    # If multi_creds_dir changed: reinitialize pool
    if need_pool_reinit:
        try:
            await _reinit_pool(request)
            logger.info("Account pool reinitialized after multi_creds_dir change")
        except Exception as exc:
            logger.error(f"Pool reinitialization failed: {exc}")
            raise HTTPException(
                status_code=500,
                detail=f"Pool reinitialization failed: {exc}",
            )

    return JSONResponse(
        content={
            "success": True,
            "updated": validated,
            "pool_reinitialized": need_pool_reinit,
        }
    )


# ---------------------------------------------------------------------------
# SSE log stream
# ---------------------------------------------------------------------------

@router.get("/logs/stream", dependencies=[Depends(verify_admin_key)])
async def logs_stream(request: Request):
    """
    Stream real-time logs and status updates via Server-Sent Events (SSE).

    On connect, sends the full log history buffer first, followed by
    the current account pool status, then streams new log entries and
    status updates in real time.

    Event types:
    - log: Log entry with time, level, message, module, function, line
    - status: Account pool status update (triggered by state changes)

    Clients should reconnect on disconnect (SSE auto-reconnect).

    Returns:
        StreamingResponse with text/event-stream content type
    """
    broadcaster: Optional[LogBroadcaster] = getattr(request.app.state, "log_broadcaster", None)
    if broadcaster is None:
        raise HTTPException(status_code=503, detail="Log broadcaster not initialized")

    pool: AccountPool = request.app.state.account_pool
    admin_cfg: AdminConfig = request.app.state.admin_config

    async def event_stream():
        queue, history = await broadcaster.subscribe()
        try:
            # Send log history first
            for entry in history:
                if await request.is_disconnected():
                    return
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"

            # Send initial status snapshot
            if await request.is_disconnected():
                return
            status = pool.get_status()
            multi_dir = admin_cfg.get_multi_creds_dir() or KIRO_MULTI_CREDS_DIR
            status["mode"] = "multi" if multi_dir else "single"
            status["multi_creds_dir"] = multi_dir or None
            status_event = {"type": "status", "data": status}
            yield f"data: {json.dumps(status_event, ensure_ascii=False)}\n\n"

            # Stream new entries (logs and status updates)
            while True:
                if await request.is_disconnected():
                    return
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive comment every 25s to prevent proxy timeouts
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            await broadcaster.unsubscribe(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _next_account_dir_name(base_dir: Path) -> str:
    """
    Find the next available account-N directory name.

    Scans existing subdirectories for names matching 'account-N' and
    returns 'account-{max+1}'. Falls back to 'account-1' if none exist.

    Args:
        base_dir: The KIRO_MULTI_CREDS_DIR path

    Returns:
        Next available directory name (e.g. 'account-3')
    """
    pattern = re.compile(r"^account-(\d+)$")
    max_n = 0
    for entry in base_dir.iterdir():
        if entry.is_dir():
            m = pattern.match(entry.name)
            if m:
                max_n = max(max_n, int(m.group(1)))
    return f"account-{max_n + 1}"


def _account_dir_name_sort_key(name: str) -> tuple[int, int, str]:
    """Natural sort for names like account-1/account-10."""
    m = re.match(r"^account-(\d+)$", name)
    if m:
        return (0, int(m.group(1)), name)
    return (1, 0, name)


def _collect_account_files_from_zip(zf: zipfile.ZipFile) -> Dict[str, Dict[str, bytes]]:
    """
    Collect account json files from ZIP.

    Only accepts files exactly under one top-level account directory:
        <account_name>/<file>.json
    """
    account_files: Dict[str, Dict[str, bytes]] = {}
    for info in zf.infolist():
        if info.is_dir():
            continue

        path = info.filename.strip("/")
        parts = path.split("/")
        if len(parts) != 2:
            continue

        account_name, filename = parts
        if not filename.lower().endswith(".json"):
            continue

        content = zf.read(info.filename)
        account_files.setdefault(account_name, {})[filename] = content
    return account_files


def _find_existing_account_by_email(pool: AccountPool, email: str) -> Optional[str]:
    """Find existing account name by email (case-insensitive)."""
    normalized = email.strip().lower()
    if not normalized:
        return None
    for slot in pool.slots:
        slot_email = (slot.email or "").strip().lower()
        if slot_email and slot_email == normalized:
            return slot.name
    return None


async def _fetch_email_for_duplicate_check(
    auth_manager: Any,
) -> Optional[str]:
    """
    Try to fetch account email for duplicate checking before importing.

    Returns None if quota API is unavailable or email is absent.
    """
    try:
        from kiro.quota_checker import check_quota

        access_token = await auth_manager.get_access_token()
        provider = getattr(auth_manager, "_provider", "BuilderId")
        quota = await check_quota(access_token, provider)
        return quota.email or None
    except Exception as exc:
        logger.warning(f"Email pre-check skipped due to quota lookup failure: {exc}")
        return None


def _resolve_host_cache_files(host_cache_dir: Path) -> Tuple[Path, Optional[Path], Dict[str, Any]]:
    """
    Resolve importable credential files from host SSO cache directory.

    Args:
        host_cache_dir: Host cache directory path

    Returns:
        Tuple of (auth_path, device_reg_path, auth_data)
    """
    auth_path = host_cache_dir / "kiro-auth-token.json"
    if not auth_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Required file not found in host cache: {auth_path.name}",
        )

    try:
        with open(auth_path, "r", encoding="utf-8") as f:
            auth_data = json.load(f)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid JSON in {auth_path.name}: {exc}",
        )

    if "refreshToken" not in auth_data and "accessToken" not in auth_data:
        raise HTTPException(
            status_code=422,
            detail=f"{auth_path.name} must contain 'refreshToken' or 'accessToken'",
        )

    device_reg_path: Optional[Path] = None
    client_id_hash = auth_data.get("clientIdHash")
    if client_id_hash:
        candidate = host_cache_dir / f"{client_id_hash}.json"
        if candidate.exists():
            device_reg_path = candidate
        else:
            logger.warning(
                "Host cache import: '{}' references missing device file '{}'",
                auth_path.name,
                candidate.name,
            )

    return auth_path, device_reg_path, auth_data


def _delete_host_cache_files(auth_path: Path, device_reg_path: Optional[Path]) -> list[str]:
    """
    Delete host cache source files after successful import.

    Args:
        auth_path: Path to kiro-auth-token.json in host cache
        device_reg_path: Optional path to {clientIdHash}.json in host cache

    Returns:
        List of deleted filenames
    """
    deleted: list[str] = []
    for file_path in (auth_path, device_reg_path):
        if not file_path or not file_path.exists():
            continue
        try:
            file_path.unlink()
            deleted.append(file_path.name)
        except Exception as exc:
            logger.warning(f"Failed to delete imported source file '{file_path}': {exc}")
    return deleted


async def _reinit_pool(request: Request) -> None:
    """
    Reinitialize the account pool after KIRO_MULTI_CREDS_DIR changes.

    This replaces app.state.account_pool with a fresh instance while
    keeping ongoing requests on the old pool (best-effort).

    Args:
        request: FastAPI request (used to access app.state)
    """
    from kiro.config import PROFILE_ARN, REGION, REFRESH_TOKEN, KIRO_CREDS_FILE, KIRO_CLI_DB_FILE, QUOTA_CHECK_INTERVAL, KIRO_MULTI_CREDS_DIR
    from kiro.account_pool import AccountPool
    from kiro.auth import KiroAuthManager

    admin_cfg: AdminConfig = request.app.state.admin_config
    multi_dir = admin_cfg.get_multi_creds_dir() or KIRO_MULTI_CREDS_DIR
    
    # Read auth overrides
    eff_profile = admin_cfg.get("accounts", "profile_arn") or PROFILE_ARN
    eff_region = admin_cfg.get("accounts", "region") or REGION
    eff_rt = admin_cfg.get("accounts", "refresh_token") or REFRESH_TOKEN
    eff_creds = admin_cfg.get("accounts", "kiro_creds_file") or KIRO_CREDS_FILE
    eff_db = admin_cfg.get("accounts", "kiro_cli_db_file") or KIRO_CLI_DB_FILE

    disabled = admin_cfg.get_disabled_accounts()

    if multi_dir:
        new_pool = AccountPool.from_directory(
            directory=multi_dir,
            profile_arn=eff_profile,
            region=eff_region,
        )
        new_pool._quota_check_interval = admin_cfg.get("pool", "quota_check_interval") or QUOTA_CHECK_INTERVAL

        # Apply disabled accounts
        for name in disabled:
            slot = new_pool.get_slot_by_name(name)
            if slot:
                slot.is_disabled = True
    else:
        auth_manager = KiroAuthManager(
            refresh_token=eff_rt,
            profile_arn=eff_profile,
            region=eff_region,
            creds_file=eff_creds if eff_creds else None,
            sqlite_db=eff_db if eff_db else None,
        )
        new_pool = AccountPool.from_single(auth_manager)

    # Swap the pool (old pool continues draining active requests)
    request.app.state.account_pool = new_pool

    # Link broadcaster and admin_config to new pool
    broadcaster = getattr(request.app.state, "log_broadcaster", None)
    if broadcaster:
        new_pool._broadcaster = broadcaster
    new_pool._admin_config = admin_cfg

    # Update auth_manager for backward compatibility
    if new_pool.size > 0:
        request.app.state.auth_manager = new_pool.slots[0].auth_manager
    else:
        request.app.state.auth_manager = None
        logger.warning("Pool reloaded with 0 accounts. Add accounts to start processing requests.")

    # Initial quota check in background
    asyncio.create_task(new_pool.initialize_quota())
    logger.info("Account pool reinitialized, quota check started in background")


# ---------------------------------------------------------------------------
# Model Controls - Per-plan model enable/disable management
# ---------------------------------------------------------------------------


@router.get("/models/control", dependencies=[Depends(verify_admin_key)])
async def get_model_controls(request: Request) -> JSONResponse:
    """
    Get model control configuration and available models grouped by plan type.

    Returns:
        - controls: {plan_type: {model_id: bool}}
        - plan_types: list[str]
        - models_by_plan: {plan_type: list[model_id]}
    """
    admin_cfg: AdminConfig = request.app.state.admin_config
    pool: AccountPool = request.app.state.account_pool

    controls = admin_cfg.get_model_controls()

    plan_types: set[str] = set()
    for slot in pool.slots:
        if slot.quota_info and slot.quota_info.subscription_plan:
            plan_types.add(slot.quota_info.subscription_plan)

    models_by_plan: Dict[str, List[str]] = {}
    for plan_type in plan_types:
        sample_slot = next(
            (
                s
                for s in pool.slots
                if s.quota_info and s.quota_info.subscription_plan == plan_type
            ),
            None,
        )
        if not sample_slot:
            models_by_plan[plan_type] = []
            continue

        try:
            access_token = await sample_slot.auth_manager.get_access_token()
            models = await _fetch_available_models(sample_slot.auth_manager, access_token)
            models_by_plan[plan_type] = [m.get("modelId", "") for m in models if m.get("modelId")]
        except Exception as exc:
            logger.warning(f"Failed to fetch models for {plan_type}: {exc}")
            models_by_plan[plan_type] = []

    return JSONResponse(
        {
            "controls": controls,
            "plan_types": sorted(plan_types),
            "models_by_plan": models_by_plan,
        }
    )


@router.post("/models/control", dependencies=[Depends(verify_admin_key)])
async def save_model_controls(request: Request) -> JSONResponse:
    """
    Save model control configuration to gateway.yml.

    Body:
        {"controls": {plan_type: {model_id: bool}}}
    """
    body = await request.json()
    controls = body.get("controls", {})
    if not isinstance(controls, dict):
        raise HTTPException(status_code=400, detail="controls must be a dictionary")

    admin_cfg: AdminConfig = request.app.state.admin_config
    admin_cfg.set_model_controls(controls)
    await admin_cfg.save()

    logger.info("Model controls saved")
    return JSONResponse({"success": True})


async def _fetch_available_models(auth_manager: Any, access_token: str) -> list[dict]:
    """
    Fetch available models from Kiro API.
    """
    from kiro.utils import get_kiro_headers
    from kiro.auth import AuthType

    headers = get_kiro_headers(auth_manager, access_token)
    params: Dict[str, Any] = {"origin": "AI_EDITOR"}
    if auth_manager.auth_type == AuthType.KIRO_DESKTOP and auth_manager.profile_arn:
        params["profileArn"] = auth_manager.profile_arn

    list_models_url = f"{auth_manager.q_host}/ListAvailableModels"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(list_models_url, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        models = data.get("models", [])
        if isinstance(models, list):
            return models
        return []
