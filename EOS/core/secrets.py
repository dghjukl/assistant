"""
SecretsManager — secure credential storage for EOS.

Backends (tried in priority order)
------------------------------------
1. Environment variable  (EOS_<KEY_UPPER>)
2. Windows Credential Manager via the ``keyring`` library
3. Explicit env var name override (via ``env_var`` kwarg)

The keyring library abstracts Windows Credential Manager on Windows,
macOS Keychain on macOS, and the Secret Service on Linux — install
once, works everywhere.

Key index
---------
The keyring API does not provide a portable "list all credentials"
method.  We maintain a plain key-name index at data/secrets_index.json
(names only, never values) so the admin panel can list what secrets
exist without exposing their contents.

Usage
-----
    from core.secrets import secrets_manager

    # Store a secret (goes to Windows Credential Manager)
    secrets_manager.set("discord_token", "Bot xxxx")

    # Retrieve — checks env var EOS_DISCORD_TOKEN first, then keyring
    token = secrets_manager.get("discord_token")

    # Delete from keyring
    secrets_manager.delete("discord_token")

    # List stored key names
    keys = secrets_manager.list_keys()
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_KEYRING_SERVICE = "EOS"
_INDEX_FILE_NAME = "secrets_index.json"


class SecretsManager:
    """Credential store backed by the system keyring with env-var override."""

    def __init__(self, data_dir: Path | str | None = None):
        if data_dir is not None:
            self._index_file = Path(data_dir) / _INDEX_FILE_NAME
        else:
            self._index_file = Path(__file__).parent.parent / "data" / _INDEX_FILE_NAME

        self._keyring_available = False
        self._keyring_error: str = ""
        try:
            import keyring  # noqa: F401 — just probe availability
            keyring.get_keyring()  # raises if no backend
            self._keyring_available = True
            logger.info("[secrets] keyring backend: %s", type(keyring.get_keyring()).__name__)
        except Exception as exc:
            self._keyring_error = str(exc)
            logger.warning(
                "[secrets] keyring not available (%s) — secrets will use "
                "environment variables only",
                exc,
            )

    # ── Public API ────────────────────────────────────────────────────────

    def get(self, key: str, env_var: Optional[str] = None) -> Optional[str]:
        """Retrieve a secret value.

        Checks (in order):
          1. Explicit ``env_var`` environment variable name
          2. Auto-derived EOS_<KEY_UPPER> environment variable
          3. Keyring (if available)

        Returns None if not found anywhere.
        """
        # 1. Explicit env var override
        if env_var:
            val = os.environ.get(env_var, "").strip()
            if val:
                return val

        # 2. Auto-derived env var: EOS_DISCORD_TOKEN for key "discord_token"
        auto_env = f"EOS_{key.upper()}"
        val = os.environ.get(auto_env, "").strip()
        if val:
            return val

        # 3. Keyring
        if self._keyring_available:
            try:
                import keyring
                return keyring.get_password(_KEYRING_SERVICE, key)
            except Exception as exc:
                logger.warning("[secrets] keyring.get_password(%r) failed: %s", key, exc)

        return None

    def set(self, key: str, value: str) -> bool:
        """Store a secret in the keyring and add its name to the index.

        Returns True on success, False if keyring is not available.
        """
        if not self._keyring_available:
            logger.error(
                "[secrets] Cannot store secret %r — keyring not available: %s",
                key,
                self._keyring_error,
            )
            return False
        try:
            import keyring
            keyring.set_password(_KEYRING_SERVICE, key, value)
            self._add_to_index(key)
            logger.info("[secrets] Secret %r stored in keyring", key)
            return True
        except Exception as exc:
            logger.error("[secrets] keyring.set_password(%r) failed: %s", key, exc)
            return False

    def delete(self, key: str) -> bool:
        """Remove a secret from the keyring and the index.

        Returns True if deleted, False if not found or not available.
        """
        if not self._keyring_available:
            return False
        try:
            import keyring
            keyring.delete_password(_KEYRING_SERVICE, key)
            self._remove_from_index(key)
            logger.info("[secrets] Secret %r deleted from keyring", key)
            return True
        except Exception as exc:
            logger.debug("[secrets] keyring.delete_password(%r): %s", key, exc)
            # Still remove from index even if keyring delete raises
            self._remove_from_index(key)
            return False

    def list_keys(self) -> List[str]:
        """Return names of secrets stored in the keyring (from index file)."""
        return self._load_index()

    @property
    def backend_available(self) -> bool:
        return self._keyring_available

    @property
    def backend_name(self) -> str:
        if not self._keyring_available:
            return f"unavailable ({self._keyring_error})"
        try:
            import keyring
            return type(keyring.get_keyring()).__name__
        except Exception:
            return "unknown"

    def status(self) -> dict:
        return {
            "keyring_available": self._keyring_available,
            "backend": self.backend_name,
            "stored_keys": self.list_keys(),
            "note": (
                "Values are stored in the system keyring — "
                "names listed here, values never exposed via API"
            ),
        }

    # ── Index helpers ─────────────────────────────────────────────────────

    def _load_index(self) -> List[str]:
        try:
            if self._index_file.is_file():
                data = json.loads(self._index_file.read_text(encoding="utf-8"))
                return sorted(set(data.get("keys", [])))
        except Exception as exc:
            logger.debug("[secrets] index load failed: %s", exc)
        return []

    def _save_index(self, keys: List[str]) -> None:
        try:
            self._index_file.parent.mkdir(parents=True, exist_ok=True)
            self._index_file.write_text(
                json.dumps({"keys": sorted(set(keys))}, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("[secrets] index save failed: %s", exc)

    def _add_to_index(self, key: str) -> None:
        keys = self._load_index()
        if key not in keys:
            keys.append(key)
            self._save_index(keys)

    def _remove_from_index(self, key: str) -> None:
        keys = self._load_index()
        if key in keys:
            keys.remove(key)
            self._save_index(keys)


# ── Module-level singleton ──────────────────────────────────────────────────

secrets_manager = SecretsManager()


def init_secrets(data_dir: Path | str) -> SecretsManager:
    """Re-initialise the module singleton with an explicit data directory.

    Call at server startup after config is loaded so the index file lands
    next to the other databases.
    """
    global secrets_manager
    secrets_manager = SecretsManager(data_dir=data_dir)
    return secrets_manager
