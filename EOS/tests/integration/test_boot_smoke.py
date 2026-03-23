"""
EOS — Boot Smoke Tests
======================
Proves the system initializes its core subsystems correctly without requiring
real llama-server backends.  This is the counterpart to verify.py: where
verify.py checks static install-time conditions, these tests prove runtime
initialization paths actually execute without crashing.

Scope
-----
- Auth token generation and loading
- Audit store initialization and write/query cycle
- Secrets manager initialization
- Config loading and validation (boot.load_config)
- DB migrations apply cleanly to a fresh database
- Survival mode: activation, turn handling, status reporting
- Orchestrator startup path (memory/db init only — no backend connections)

What is NOT covered here
------------------------
- Live llama-server communication (requires real models — integration environment only)
- Full FastAPI boot (covered by test_api_contracts.py)
- Tool execution against real external services
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.release_gate


# ── Config helpers ────────────────────────────────────────────────────────────

def make_minimal_config(tmp_path: Path) -> dict:
    """Return a minimal EOS config dict with all paths under tmp_path."""
    return {
        "deployment_mode": "standard",
        "db_path": str(tmp_path / "entity_state.db"),
        "retrieval": {
            "chroma_path": str(tmp_path / "chroma"),
            "embed_model":  "models/embedding/all-MiniLM-L6-v2",
            "collection":   "entity_memory",
            "top_k":        5,
        },
        "google":  {"enabled": False},
        "discord": {"enabled": False},
        "servers": {
            "primary": {
                "enabled": False, "required": False,
                "host": "127.0.0.1", "port": 8080,
                "binary_cpu": "llama-CPU/llama-server.exe",
                "model_path": "models/primary/",
                "n_gpu_layers": 0, "context_size": 4096,
            }
        },
    }


def write_config(tmp_path: Path) -> Path:
    cfg = make_minimal_config(tmp_path)
    cfg_file = tmp_path / "config_test.json"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
    return cfg_file


# ── Boot config loader ────────────────────────────────────────────────────────

class TestBootConfigLoader:
    """boot.load_config — correct validation and rejection behaviour."""

    def test_valid_config_loads(self, tmp_path):
        p = write_config(tmp_path)
        from runtime.boot import load_config
        cfg = load_config(p)
        assert cfg["deployment_mode"] == "standard"

    def test_missing_deployment_mode_raises(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text('{"servers": {}}', encoding="utf-8")
        from runtime.boot import load_config, BootError
        with pytest.raises(BootError, match="deployment_mode"):
            load_config(p)

    def test_invalid_deployment_mode_raises(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text('{"deployment_mode": "turbo"}', encoding="utf-8")
        from runtime.boot import load_config, BootError
        with pytest.raises(BootError, match="Invalid deployment_mode"):
            load_config(p)

    def test_missing_file_raises(self, tmp_path):
        from runtime.boot import load_config, BootError
        with pytest.raises(BootError, match="not found"):
            load_config(tmp_path / "nonexistent.json")

    def test_vision_mode_accepted(self, tmp_path):
        p = tmp_path / "v.json"
        p.write_text('{"deployment_mode": "vision"}', encoding="utf-8")
        from runtime.boot import load_config
        cfg = load_config(p)
        assert cfg["deployment_mode"] == "vision"


# ── Auth token lifecycle ──────────────────────────────────────────────────────

class TestAuthTokenLifecycle:
    """Auth token is generated, persisted, and reloaded correctly."""

    def test_generates_token_on_first_run(self, tmp_path):
        from core.auth import load_or_create_token
        token = load_or_create_token(data_dir=tmp_path)
        assert token and len(token) >= 20
        # File must exist
        assert (tmp_path / "admin_token.txt").is_file()

    def test_reloads_same_token_on_second_run(self, tmp_path):
        from core.auth import load_or_create_token
        t1 = load_or_create_token(data_dir=tmp_path)
        t2 = load_or_create_token(data_dir=tmp_path)
        assert t1 == t2

    def test_env_override_takes_precedence(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EOS_ADMIN_TOKEN", "test-override-token-12345")
        from core.auth import load_or_create_token
        token = load_or_create_token(data_dir=tmp_path)
        assert token == "test-override-token-12345"


# ── Audit store lifecycle ─────────────────────────────────────────────────────

class TestAuditStore:
    """AuditStore initializes and can record/query entries."""

    def test_init_creates_database(self, tmp_path):
        from core.audit import AuditStore
        store = AuditStore(tmp_path / "audit.db")
        assert (tmp_path / "audit.db").is_file()

    def test_log_and_query_tool_call(self, tmp_path):
        from core.audit import AuditStore
        store = AuditStore(tmp_path / "audit.db")
        store.log_tool_call(
            tool_name="test_tool",
            params={"key": "value"},
            result="ok",
            caller="smoke_test",
            duration_ms=42,
        )
        entries = store.query(limit=10)
        assert len(entries) >= 1
        assert any(e.get("tool_name") == "test_tool" for e in entries)

    def test_query_returns_most_recent_first(self, tmp_path):
        from core.audit import AuditStore
        store = AuditStore(tmp_path / "audit.db")
        for i in range(5):
            store.log_tool_call(
                tool_name=f"tool_{i}", params={}, result="ok",
                caller="smoke_test", duration_ms=i,
            )
        entries = store.query(limit=5)
        assert len(entries) == 5
        # Most recent entry is tool_4
        assert entries[0]["tool_name"] == "tool_4"


# ── Secrets manager ───────────────────────────────────────────────────────────

class TestSecretsManager:
    """SecretsManager initialises cleanly; env-var path is always available."""

    def test_init_without_keyring(self, tmp_path):
        """Should not raise even if system keyring is unavailable."""
        from core.secrets import SecretsManager
        sm = SecretsManager(data_dir=tmp_path)
        # Should have determined availability status without crashing
        assert isinstance(sm.backend_available, bool)

    def test_env_var_get(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EOS_SMOKE_SECRET", "hello-from-env")
        from core.secrets import SecretsManager
        sm = SecretsManager(data_dir=tmp_path)
        val = sm.get("smoke_secret")
        assert val == "hello-from-env"

    def test_get_missing_returns_none(self, tmp_path):
        from core.secrets import SecretsManager
        sm = SecretsManager(data_dir=tmp_path)
        assert sm.get("this_key_definitely_does_not_exist_xyz") is None


# ── DB migrations ─────────────────────────────────────────────────────────────

class TestDbMigrations:
    """Migrations apply cleanly to a fresh database and are idempotent."""

    def test_apply_to_fresh_db(self, tmp_path):
        from core.db_migrations import apply_migrations
        db = tmp_path / "fresh.db"
        apply_migrations(db)   # must not raise
        assert db.is_file()

    def test_idempotent_second_apply(self, tmp_path):
        from core.db_migrations import apply_migrations
        db = tmp_path / "idem.db"
        apply_migrations(db)
        apply_migrations(db)   # must not raise on second run


# ── Survival mode ─────────────────────────────────────────────────────────────

class TestSurvivalMode:
    """SurvivalModeService activates and handles turns with correct responses."""

    def setup_method(self):
        from runtime.survival_mode import SurvivalModeService, SurvivalReason
        self.SurvivalModeService = SurvivalModeService
        self.SurvivalReason = SurvivalReason

    def test_inactive_by_default(self):
        svc = self.SurvivalModeService()
        assert not svc.is_active

    def test_activate_sets_state(self):
        svc = self.SurvivalModeService()
        svc.activate(self.SurvivalReason.PRIMARY_BOOT_FAILURE, detail="test detail")
        assert svc.is_active
        st = svc.status()
        assert st["survival_mode"] is True
        assert st["reason"] == "primary_boot_failure"

    def test_activate_is_idempotent(self):
        svc = self.SurvivalModeService()
        svc.activate(self.SurvivalReason.PRIMARY_BOOT_FAILURE)
        svc.activate(self.SurvivalReason.UNKNOWN)  # second call ignored
        assert svc.status()["reason"] == "primary_boot_failure"

    def test_deactivate_clears_state(self):
        svc = self.SurvivalModeService()
        svc.activate(self.SurvivalReason.PRIMARY_MID_SESSION)
        svc.deactivate()
        assert not svc.is_active

    def test_handle_turn_help_command(self):
        svc = self.SurvivalModeService()
        svc.activate(self.SurvivalReason.UNKNOWN)
        resp = svc.handle_turn("/help")
        assert "survival mode" in resp.lower()
        assert "/status" in resp

    def test_handle_turn_status_command(self):
        svc = self.SurvivalModeService()
        svc.activate(self.SurvivalReason.PRIMARY_BOOT_FAILURE)
        resp = svc.handle_turn("/status")
        assert "primary_boot_failure" in resp
        assert "SURVIVAL MODE" in resp

    def test_handle_turn_diagnose_with_detail(self):
        svc = self.SurvivalModeService()
        svc.activate(self.SurvivalReason.CONFIG_FATAL, detail="config.json not found")
        resp = svc.handle_turn("/diagnose")
        assert "config.json not found" in resp

    def test_handle_turn_unknown_message_returns_degraded(self):
        svc = self.SurvivalModeService()
        svc.activate(self.SurvivalReason.UNKNOWN)
        resp = svc.handle_turn("tell me a joke")
        assert "survival mode" in resp.lower()

    def test_turn_count_increments(self):
        svc = self.SurvivalModeService()
        svc.activate(self.SurvivalReason.UNKNOWN)
        svc.handle_turn("a")
        svc.handle_turn("b")
        assert svc.status()["turns_handled"] == 2

    def test_status_dict_inactive(self):
        svc = self.SurvivalModeService()
        st = svc.status()
        assert st["survival_mode"] is False
        assert st["reason"] is None
