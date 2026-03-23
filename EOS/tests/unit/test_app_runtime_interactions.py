from __future__ import annotations

import asyncio
import json
import time

import webui.app_runtime as app_runtime
from runtime.idle_cognition import IdleCognitionEngine
from runtime.signal_bus import SignalBus
from webui.app_state import app_state


class _TurnSpy:
    def __init__(self):
        self.calls = 0

    def notify_turn(self) -> None:
        self.calls += 1


class _LegacyIdleCognition:
    def __init__(self):
        self.calls = 0

    def notify_interaction(self) -> None:
        self.calls += 1




class _BackupStub:
    def __init__(self):
        self.runs = []
        self.created = []
        self._needs = False
        self._diag = {
            "last_run_at": None,
            "next_run_at": None,
            "recent_failure": None,
            "recent_failures": [],
        }

    def diagnostics(self):
        return dict(self._diag)

    def needs_auto_backup(self):
        return self._needs

    def create_backup(self, **kwargs):
        self.created.append(kwargs)
        return type("Manifest", (), {"backup_id": "bk-1", "total_size_bytes": 42})()

    def mark_auto_backup_run(self, **kwargs):
        self.runs.append(("run", kwargs))
        self._diag["last_run_at"] = "2026-03-23T00:00:00Z"
        self._diag["next_run_at"] = kwargs.get("next_run_at")
        self._diag["recent_failure"] = None

    def mark_auto_backup_failure(self, exc, **kwargs):
        self.runs.append(("failure", str(exc), kwargs))
        self._diag["last_run_at"] = "2026-03-23T00:00:00Z"
        self._diag["next_run_at"] = kwargs.get("next_run_at")
        self._diag["recent_failure"] = {"at": self._diag["last_run_at"], "error": str(exc)}


class _TopologyStub:
    def __init__(self):
        self._boot_time = time.time() - 10

    def status_summary(self) -> dict:
        return {"boot_time": self._boot_time, "servers": {}}


def test_notify_interaction_updates_app_and_idle_cognition_timestamps():
    app_state.idle_cognition = IdleCognitionEngine({})

    interaction_ts = app_runtime._notify_interaction()

    assert app_state.last_interaction_monotonic == interaction_ts
    assert app_state.idle_cognition._last_interaction_monotonic == interaction_ts


def test_notify_interaction_supports_legacy_idle_cognition_signature():
    app_state.idle_cognition = _LegacyIdleCognition()

    interaction_ts = app_runtime._notify_interaction()

    assert interaction_ts == app_state.last_interaction_monotonic
    assert app_state.idle_cognition.calls == 1


def test_discord_turn_notifier_updates_interaction_and_turn_engines():
    app_state.idle_cognition = IdleCognitionEngine({})
    app_state.reflection_pipeline = _TurnSpy()
    app_state.initiative_engine = _TurnSpy()

    notifier = app_runtime._build_discord_turn_notifier()
    notifier()

    assert app_state.last_interaction_monotonic == app_state.idle_cognition._last_interaction_monotonic
    assert app_state.reflection_pipeline.calls == 1
    assert app_state.initiative_engine.calls == 1


def test_admin_get_status_includes_signal_bus_health_summary(monkeypatch):
    app_state.topology = _TopologyStub()
    app_state.cfg = {}
    app_state.tool_states = {}
    app_state.session_id = "session-123"
    app_state.bus = SignalBus()
    app_state.bus._durable_write_failures = 2
    monkeypatch.setattr(app_runtime, "get_status", lambda cfg: {"interaction_count": 4})

    response = asyncio.run(app_runtime.admin_get_status())
    payload = json.loads(response.body)

    assert payload["ok"] is True
    assert payload["data"]["signal_bus"]["available"] is True
    assert payload["data"]["signal_bus"]["durable_write_failures"] == 2
    assert payload["data"]["signal_bus"]["durable_log_healthy"] is False


def test_admin_runtime_diagnostics_includes_signal_bus_health_summary():
    app_state.topology = _TopologyStub()
    app_state.bus = SignalBus()
    app_state.bus._durable_write_failures = 1

    response = asyncio.run(app_runtime.admin_runtime_diagnostics())
    payload = json.loads(response.body)

    assert payload["ok"] is True
    assert payload["data"]["signal_bus"]["available"] is True
    assert payload["data"]["signal_bus"]["durable_write_failures"] == 1
    assert payload["data"]["signal_bus"]["healthy"] is False


def test_admin_get_status_includes_backup_health_summary(monkeypatch):
    app_state.topology = _TopologyStub()
    app_state.cfg = {}
    app_state.tool_states = {}
    app_state.session_id = "session-123"
    app_state.backup_service = _BackupStub()
    app_state.backup_service._diag.update({
        "last_run_at": "2026-03-23T00:00:00Z",
        "next_run_at": "2026-03-24T00:00:00Z",
        "recent_failure": {"at": "2026-03-22T00:00:00Z", "error": "disk full"},
    })
    monkeypatch.setattr(app_runtime, "get_status", lambda cfg: {"interaction_count": 4})

    response = asyncio.run(app_runtime.admin_get_status())
    payload = json.loads(response.body)

    assert payload["ok"] is True
    assert payload["data"]["backup"]["available"] is True
    assert payload["data"]["backup"]["last_run_at"] == "2026-03-23T00:00:00Z"
    assert payload["data"]["backup"]["recent_failure"]["error"] == "disk full"


def test_run_auto_backup_cycle_creates_backup_when_needed():
    app_state.cfg = {"backup": {"auto_backup_interval_hours": 1}}
    app_state.backup_service = _BackupStub()
    app_state.backup_service._needs = True
    app_state.log_ring.clear()

    asyncio.run(app_runtime._run_auto_backup_cycle())

    assert app_state.backup_service.created == [{
        "label": "auto",
        "trigger": "auto_interval",
        "notes": "Background auto-backup scheduler",
    }]
    assert app_state.backup_service.runs[-1][0] == "run"
    assert any(entry["source"] == "backup" and entry["level"] == "info" for entry in app_state.log_ring)


def test_run_auto_backup_cycle_records_failure():
    class _FailingBackup(_BackupStub):
        def needs_auto_backup(self):
            raise RuntimeError("boom")

    app_state.cfg = {"backup": {"auto_backup_interval_hours": 1}}
    app_state.backup_service = _FailingBackup()
    app_state.log_ring.clear()

    asyncio.run(app_runtime._run_auto_backup_cycle())

    assert app_state.backup_service.runs[-1][0] == "failure"
    assert app_state.backup_service._diag["recent_failure"]["error"] == "boom"
    assert any(entry["source"] == "backup" and entry["level"] == "error" for entry in app_state.log_ring)
