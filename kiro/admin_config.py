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
Admin configuration manager for Kiro Gateway.

Manages gateway.yml hot-reload configuration file. Values in this file
override corresponding .env settings without requiring a server restart.

Only hot-updatable parameters are stored here; authentication credentials
and server host/port remain in .env where they belong.

gateway.yml structure:
    pool:
      cooldown_seconds: 300
      queue_timeout: 300
      quota_check_interval: 300
    timeout:
      first_token_timeout: 15
      first_token_max_retries: 3
      streaming_read_timeout: 300
    reasoning:
      fake_reasoning: true
      fake_reasoning_max_tokens: 4000
      fake_reasoning_handling: as_reasoning_content
    logging:
      log_level: INFO
      debug_mode: off
      log_history_size: 200
    accounts:
      disabled: []
      multi_creds_dir: null
"""

import asyncio
import copy
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from loguru import logger


# Global instance for easy access from other modules
_instance: Optional["AdminConfig"] = None


def get_admin_config() -> "AdminConfig":
    """
    Get the global AdminConfig instance.
    
    Returns:
        The AdminConfig instance (creates a default one if not yet initialized)
    """
    global _instance
    if _instance is None:
        _instance = AdminConfig()
    return _instance


# Default configuration structure.
# None means "not set in gateway.yml, fall back to .env / hardcoded default".
_DEFAULT_CONFIG: Dict[str, Any] = {
    "pool": {
        "cooldown_seconds": None,
        "queue_timeout": None,
        "quota_check_interval": None,
    },
    "timeout": {
        "first_token_timeout": None,
        "first_token_max_retries": None,
        "streaming_read_timeout": None,
    },
    "reasoning": {
        "fake_reasoning": None,
        "fake_reasoning_max_tokens": None,
        "fake_reasoning_handling": None,
    },
    "logging": {
        "log_level": None,
        "debug_mode": None,
        "log_history_size": None,
    },
    "accounts": {
        "disabled": [],
        "multi_creds_dir": None,  # overrides KIRO_MULTI_CREDS_DIR from .env
        "refresh_token": None,
        "kiro_creds_file": None,
        "kiro_cli_db_file": None,
        "profile_arn": None,
        "region": None,
    },
}

_FILE_HEADER = (
    "# Kiro Gateway — Hot-reload configuration\n"
    "# Values here override .env without requiring a server restart.\n"
    "# Do NOT store credentials or tokens here — use .env for secrets.\n\n"
)


class AdminConfig:
    """
    Manages the gateway.yml hot-reload configuration file.

    Values in gateway.yml take priority over .env settings for supported
    parameters. Unsupported / authentication parameters remain in .env.

    Thread-safe for concurrent async access via asyncio.Lock.

    Attributes:
        path: Resolved path to gateway.yml
    """

    def __init__(self, config_path: str = "gateway.yml") -> None:
        """
        Initialize AdminConfig and load gateway.yml.

        Creates gateway.yml with defaults if it does not already exist.

        Args:
            config_path: Path to gateway.yml file (relative or absolute)
        """
        self._path = Path(config_path).resolve()
        self._config: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._load_sync()
        
        # Set global instance
        global _instance
        _instance = self

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_sync(self) -> None:
        """Load configuration from disk synchronously (called at init)."""
        if not self._path.exists():
            self._config = copy.deepcopy(_DEFAULT_CONFIG)
            self._save_sync()
            logger.info(f"Created default gateway.yml at {self._path}")
        else:
            try:
                content = self._path.read_text(encoding="utf-8")
                loaded = yaml.safe_load(content) or {}
                self._config = _deep_merge(_DEFAULT_CONFIG, loaded)
                logger.info(f"Loaded gateway.yml from {self._path}")
            except Exception as exc:
                logger.warning(
                    f"Failed to load gateway.yml ({exc}), using empty defaults"
                )
                self._config = copy.deepcopy(_DEFAULT_CONFIG)

    def _save_sync(self) -> None:
        """Save configuration to disk synchronously (called at init)."""
        try:
            content = _FILE_HEADER + yaml.dump(
                self._config, default_flow_style=False, allow_unicode=True
            )
            self._path.write_text(content, encoding="utf-8")
        except OSError as exc:
            logger.error(f"Failed to write gateway.yml: {exc}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        """Resolved path to gateway.yml."""
        return self._path

    def get(self, section: str, key: str) -> Any:
        """
        Get a single configuration value.

        Args:
            section: Top-level YAML section (e.g. "pool", "timeout")
            key: Key within that section

        Returns:
            The stored value, or None if not set
        """
        return self._config.get(section, {}).get(key)

    def set_value(self, section: str, key: str, value: Any) -> None:
        """
        Set a single configuration value in memory only.

        Call ``save()`` afterwards to persist to disk.

        Args:
            section: Top-level YAML section
            key: Key within that section
            value: New value (use None to unset / fall back to .env)
        """
        if section not in self._config:
            self._config[section] = {}
        self._config[section][key] = value

    def apply_updates(self, updates: Dict[str, Any]) -> None:
        """
        Apply multiple section/key updates at once (in memory only).

        Args:
            updates: Dict of {section: {key: value, ...}, ...}
        """
        for section, values in updates.items():
            if isinstance(values, dict):
                for key, value in values.items():
                    self.set_value(section, key, value)

    async def save(self) -> None:
        """
        Persist current in-memory configuration to gateway.yml.

        Thread-safe via asyncio.Lock.

        Raises:
            OSError: If file cannot be written
        """
        async with self._lock:
            try:
                content = _FILE_HEADER + yaml.dump(
                    self._config, default_flow_style=False, allow_unicode=True
                )
                self._path.write_text(content, encoding="utf-8")
                logger.info("gateway.yml saved successfully")
            except OSError as exc:
                logger.error(f"Failed to save gateway.yml: {exc}")
                raise

    def reload(self) -> None:
        """
        Reload configuration from disk.

        Useful after external edits to gateway.yml. Does NOT acquire
        the async lock (call from sync context only).
        """
        self._load_sync()

    # ------------------------------------------------------------------
    # Convenience accessors for accounts section
    # ------------------------------------------------------------------

    def get_disabled_accounts(self) -> List[str]:
        """
        Return the list of disabled account directory names.

        Returns:
            List of disabled account names (empty list if none)
        """
        return list(self._config.get("accounts", {}).get("disabled", []))

    def add_disabled_account(self, name: str) -> None:
        """
        Add an account name to the disabled list (in memory only).

        Args:
            name: Account directory name to disable
        """
        disabled = self.get_disabled_accounts()
        if name not in disabled:
            disabled.append(name)
            self.set_value("accounts", "disabled", disabled)

    def remove_disabled_account(self, name: str) -> None:
        """
        Remove an account name from the disabled list (in memory only).

        Args:
            name: Account directory name to re-enable
        """
        disabled = self.get_disabled_accounts()
        if name in disabled:
            disabled.remove(name)
            self.set_value("accounts", "disabled", disabled)

    def get_multi_creds_dir(self) -> Optional[str]:
        """
        Return KIRO_MULTI_CREDS_DIR override from gateway.yml, or None.

        Returns:
            Directory path string, or None if not configured here
        """
        val = self._config.get("accounts", {}).get("multi_creds_dir")
        return str(val) if val else None

    def get_full_config(self) -> Dict[str, Any]:
        """
        Return a deep copy of the full configuration dict.

        Returns:
            Full configuration as a dict
        """
        return copy.deepcopy(self._config)


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _deep_merge(defaults: Dict, overrides: Dict) -> Dict:
    """
    Recursively merge overrides into defaults.

    Lists are replaced entirely (not merged element-by-element).
    Dicts are merged recursively. Scalars replace defaults.

    Args:
        defaults: Base configuration dict
        overrides: Values to overlay on top of defaults

    Returns:
        New merged dict (neither input is mutated)
    """
    result = copy.deepcopy(defaults)
    for key, value in overrides.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
