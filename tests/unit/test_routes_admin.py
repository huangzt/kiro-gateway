import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zipfile
import io

import pytest
from fastapi import HTTPException

from kiro import routes_admin


class _FakeAdminConfig:
    def __init__(self, multi_dir: str) -> None:
        self._multi_dir = multi_dir

    def get_multi_creds_dir(self) -> str:
        return self._multi_dir

    def remove_disabled_account(self, _name: str) -> None:
        return None

    async def save(self) -> None:
        return None


class _FakeAuthManager:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.auth_type = SimpleNamespace(value="kiro_desktop")
        self.get_access_token = AsyncMock(return_value="token")


class _FakePool:
    def __init__(self) -> None:
        self.add_slot = AsyncMock()
        self._check_slot_quota = AsyncMock()
        self.remove_slot = AsyncMock(return_value=True)
        self._slots = {}

    @property
    def slots(self):
        return list(self._slots.values())

    def add_existing_slot(self, name: str, email: str = "") -> None:
        quota_info = SimpleNamespace(email=email) if email else None
        slot = SimpleNamespace(
            name=name,
            email=email,
            quota_info=quota_info,
            active_requests=0,
        )
        self._slots[name] = slot

    def get_slot_by_name(self, name: str):
        return self._slots.get(name)


def _build_request_state(multi_dir: str) -> SimpleNamespace:
    pool = _FakePool()
    app_state = SimpleNamespace(
        admin_config=_FakeAdminConfig(multi_dir),
        account_pool=pool,
    )
    return SimpleNamespace(app=SimpleNamespace(state=app_state))


class TestResolveHostCacheFiles:
    def test_resolve_host_cache_files_with_device_file(self, tmp_path):
        host_dir = tmp_path / "host-cache"
        host_dir.mkdir(parents=True)
        (host_dir / "kiro-auth-token.json").write_text(
            json.dumps({"refreshToken": "rt-1", "clientIdHash": "abc123"}),
            encoding="utf-8",
        )
        (host_dir / "abc123.json").write_text(
            json.dumps({"clientId": "cid", "clientSecret": "secret"}),
            encoding="utf-8",
        )

        auth_path, device_path, auth_data = routes_admin._resolve_host_cache_files(host_dir)

        assert auth_path.name == "kiro-auth-token.json"
        assert device_path is not None
        assert device_path.name == "abc123.json"
        assert auth_data["refreshToken"] == "rt-1"

    def test_resolve_host_cache_files_missing_auth_raises(self, tmp_path):
        with pytest.raises(HTTPException) as exc_info:
            routes_admin._resolve_host_cache_files(tmp_path)
        assert "Required file not found" in str(exc_info.value.detail)


class TestImportHostCacheAccount:
    @pytest.mark.asyncio
    async def test_import_host_cache_account_with_delete_source_files(self, tmp_path, monkeypatch):
        host_dir = tmp_path / "host-cache"
        host_dir.mkdir(parents=True)
        (host_dir / "kiro-auth-token.json").write_text(
            json.dumps({"refreshToken": "rt-1", "clientIdHash": "hash001"}),
            encoding="utf-8",
        )
        (host_dir / "hash001.json").write_text(
            json.dumps({"clientId": "cid-1", "clientSecret": "sec-1"}),
            encoding="utf-8",
        )

        multi_dir = tmp_path / "multi"
        request = _build_request_state(str(multi_dir))

        monkeypatch.setattr("kiro.config.KIRO_HOST_CACHE_DIR", str(host_dir))
        monkeypatch.setattr("kiro.auth.KiroAuthManager", _FakeAuthManager)
        monkeypatch.setattr("kiro.routes_admin._fetch_email_for_duplicate_check", AsyncMock(return_value=None))

        response = await routes_admin.import_host_cache_account(
            request,
            routes_admin.ImportHostCacheRequest(delete_source_files=True),
        )
        payload = json.loads(response.body)

        assert payload["success"] is True
        assert payload["account_name"] == "account-1"
        assert sorted(payload["copied_files"]) == ["hash001.json", "kiro-auth-token.json"]
        assert sorted(payload["deleted_source_files"]) == ["hash001.json", "kiro-auth-token.json"]
        assert (multi_dir / "account-1" / "kiro-auth-token.json").exists()
        assert not (host_dir / "kiro-auth-token.json").exists()
        assert not (host_dir / "hash001.json").exists()

    @pytest.mark.asyncio
    async def test_import_host_cache_account_without_delete_source_files(self, tmp_path, monkeypatch):
        host_dir = tmp_path / "host-cache"
        host_dir.mkdir(parents=True)
        (host_dir / "kiro-auth-token.json").write_text(
            json.dumps({"refreshToken": "rt-2"}),
            encoding="utf-8",
        )

        multi_dir = tmp_path / "multi"
        request = _build_request_state(str(multi_dir))

        monkeypatch.setattr("kiro.config.KIRO_HOST_CACHE_DIR", str(host_dir))
        monkeypatch.setattr("kiro.auth.KiroAuthManager", _FakeAuthManager)
        monkeypatch.setattr("kiro.routes_admin._fetch_email_for_duplicate_check", AsyncMock(return_value=None))

        response = await routes_admin.import_host_cache_account(
            request,
            routes_admin.ImportHostCacheRequest(delete_source_files=False),
        )
        payload = json.loads(response.body)

        assert payload["success"] is True
        assert payload["deleted_source_files"] == []
        assert (host_dir / "kiro-auth-token.json").exists()

    @pytest.mark.asyncio
    async def test_import_host_cache_account_duplicate_email(self, tmp_path, monkeypatch):
        host_dir = tmp_path / "host-cache"
        host_dir.mkdir(parents=True)
        (host_dir / "kiro-auth-token.json").write_text(
            json.dumps({"refreshToken": "rt-3"}),
            encoding="utf-8",
        )

        multi_dir = tmp_path / "multi"
        request = _build_request_state(str(multi_dir))
        request.app.state.account_pool.add_existing_slot("account-8", email="dup@example.com")

        monkeypatch.setattr("kiro.config.KIRO_HOST_CACHE_DIR", str(host_dir))
        monkeypatch.setattr("kiro.auth.KiroAuthManager", _FakeAuthManager)
        monkeypatch.setattr(
            "kiro.routes_admin._fetch_email_for_duplicate_check",
            AsyncMock(return_value="dup@example.com"),
        )

        with pytest.raises(HTTPException) as exc_info:
            await routes_admin.import_host_cache_account(
                request,
                routes_admin.ImportHostCacheRequest(delete_source_files=False),
            )
        assert exc_info.value.status_code == 409
        assert "账号已存在" in str(exc_info.value.detail)


class TestExportAccountsZip:
    @pytest.mark.asyncio
    async def test_export_accounts_zip_without_delete(self, tmp_path):
        multi_dir = tmp_path / "multi"
        account1 = multi_dir / "account-1"
        account2 = multi_dir / "account-2"
        account1.mkdir(parents=True)
        account2.mkdir(parents=True)
        (account1 / "kiro-auth-token.json").write_text(json.dumps({"refreshToken": "a1"}), encoding="utf-8")
        (account2 / "kiro-auth-token.json").write_text(json.dumps({"refreshToken": "a2"}), encoding="utf-8")

        request = _build_request_state(str(multi_dir))
        request.app.state.account_pool.add_existing_slot("account-1", email="a1@example.com")
        request.app.state.account_pool.add_existing_slot("account-2", email="a2@example.com")

        response = await routes_admin.export_accounts_zip(
            request,
            routes_admin.BatchExportAccountsRequest(
                account_names=["account-1", "account-2"],
                delete_exported_accounts=False,
            ),
        )

        assert response.media_type == "application/zip"
        assert request.app.state.account_pool.remove_slot.await_count == 0

        with zipfile.ZipFile(routes_admin.io.BytesIO(response.body), "r") as zf:
            names = sorted(zf.namelist())
            assert names == [
                "account-1/kiro-auth-token.json",
                "account-2/kiro-auth-token.json",
            ]

    @pytest.mark.asyncio
    async def test_export_accounts_zip_with_delete(self, tmp_path):
        multi_dir = tmp_path / "multi"
        account1 = multi_dir / "account-1"
        account1.mkdir(parents=True)
        (account1 / "kiro-auth-token.json").write_text(json.dumps({"refreshToken": "a1"}), encoding="utf-8")

        request = _build_request_state(str(multi_dir))
        request.app.state.account_pool.add_existing_slot("account-1", email="a1@example.com")

        response = await routes_admin.export_accounts_zip(
            request,
            routes_admin.BatchExportAccountsRequest(
                account_names=["account-1"],
                delete_exported_accounts=True,
            ),
        )

        assert response.media_type == "application/zip"
        request.app.state.account_pool.remove_slot.assert_awaited_once_with("account-1")
        assert not account1.exists()


class TestImportAccountsZip:
    @pytest.mark.asyncio
    async def test_import_accounts_zip_success(self, tmp_path, monkeypatch):
        multi_dir = tmp_path / "multi"
        request = _build_request_state(str(multi_dir))

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("account-1/kiro-auth-token.json", json.dumps({"refreshToken": "r1"}))
            zf.writestr("account-2/kiro-auth-token.json", json.dumps({"refreshToken": "r2"}))

        upload = SimpleNamespace(filename="accounts.zip", read=AsyncMock(return_value=zip_buf.getvalue()))
        monkeypatch.setattr("kiro.auth.KiroAuthManager", _FakeAuthManager)
        monkeypatch.setattr("kiro.routes_admin._fetch_email_for_duplicate_check", AsyncMock(return_value=None))

        response = await routes_admin.import_accounts_zip(request, upload)
        payload = json.loads(response.body)

        assert payload["success"] is True
        assert payload["total"] == 2
        assert payload["imported_count"] == 2
        assert payload["skipped_count"] == 0
        assert (multi_dir / "account-1" / "kiro-auth-token.json").exists()
        assert (multi_dir / "account-2" / "kiro-auth-token.json").exists()

    @pytest.mark.asyncio
    async def test_import_accounts_zip_skips_duplicate_email(self, tmp_path, monkeypatch):
        multi_dir = tmp_path / "multi"
        request = _build_request_state(str(multi_dir))
        request.app.state.account_pool.add_existing_slot("account-9", email="dup@example.com")

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("account-1/kiro-auth-token.json", json.dumps({"refreshToken": "r1"}))

        upload = SimpleNamespace(filename="accounts.zip", read=AsyncMock(return_value=zip_buf.getvalue()))
        monkeypatch.setattr("kiro.auth.KiroAuthManager", _FakeAuthManager)
        monkeypatch.setattr(
            "kiro.routes_admin._fetch_email_for_duplicate_check",
            AsyncMock(return_value="dup@example.com"),
        )

        response = await routes_admin.import_accounts_zip(request, upload)
        payload = json.loads(response.body)

        assert payload["success"] is True
        assert payload["imported_count"] == 0
        assert payload["skipped_count"] == 1
        assert "已存在于账号" in payload["skipped"][0]["reason"]
