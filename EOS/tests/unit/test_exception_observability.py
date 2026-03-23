from __future__ import annotations

import sqlite3
import shutil
from pathlib import Path

import pytest

from interfaces import discord_bot
from runtime.backup_service import BackupService
from runtime.investigation_engine import InvestigationEngine
from runtime.reflection_pipeline import ReflectionPipeline
from webui.app_state import app_state


class _CapabilityRegistry:
    def __init__(self) -> None:
        self.statuses: dict[str, tuple[str, str]] = {}

    def set_status(self, name: str, status: str, reason: str = "") -> None:
        self.statuses[name] = (status, reason)


class _Topology:
    def primary_endpoint(self) -> str:
        return "http://127.0.0.1:18080"


class _FocusService:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def set_background_focus(self, **kwargs):
        self.records.append(kwargs)
        return kwargs


@pytest.mark.asyncio
async def test_reflection_pipeline_surfaces_identity_failures_in_diagnostics_and_log_ring(monkeypatch):
    app_state.capability_registry = _CapabilityRegistry()
    app_state.current_focus_service = _FocusService()

    import core.identity as identity

    async def _boom(**kwargs):
        raise RuntimeError("identity boom")

    monkeypatch.setattr(identity, "run_evaluation_cycle", _boom)

    pipeline = ReflectionPipeline({"cognition": {}})
    result = await pipeline.run_once(_Topology())

    assert result["error"] == "identity boom"
    assert pipeline.diagnostics()["failure_count"] >= 1
    assert any(
        evt["operation"] == "identity eval cycle"
        for evt in pipeline.diagnostics()["recent_failures"]
    )
    assert any(
        entry["source"] == "reflection_pipeline"
        and "identity eval cycle failed" in entry["message"]
        for entry in app_state.log_ring
    )
    assert app_state.capability_registry.statuses["reflection_pipeline"][0] == "degraded"
    assert app_state.current_focus_service.records[-1]["status"] == "blocked"


@pytest.mark.asyncio
async def test_investigation_publish_failures_are_observable(minimal_cfg):
    app_state.capability_registry = _CapabilityRegistry()
    engine = InvestigationEngine(minimal_cfg)
    inv = engine.create(title="Observe bus failures")

    async def _fake_gather(*args, **kwargs):
        return []

    async def _fake_review(*args, **kwargs):
        result = args[3]
        result["summary"] = "ok"
        result["confidence_score"] = 0.7

    engine._gather_evidence = _fake_gather  # type: ignore[method-assign]
    engine._do_evidence_review = _fake_review  # type: ignore[method-assign]

    class _FailBus:
        def publish(self, envelope):
            raise RuntimeError("bus down")

    result = await engine.run_pass(
        _Topology(),
        inv["investigation_id"],
        task_type="evidence_review",
        bus=_FailBus(),
    )

    assert result["ok"] is True
    diagnostics = engine.get_diagnostics()
    assert any(
        evt["operation"] == "publish pass-complete signal"
        for evt in diagnostics["recent_failures"]
    )
    assert any(
        entry["source"] == "investigation_engine"
        and "publish pass-complete signal failed" in entry["message"]
        for entry in app_state.log_ring
    )


def test_backup_restore_failures_are_observable(tmp_path):
    cfg = {
        "project_root": str(tmp_path),
        "db_path": "data/entity_state.db",
        "retrieval": {"chroma_path": "data/memory_store"},
        "workspace_tools": {"workspace_root": "data/workspace"},
        "backup": {"backup_path": "data/backups"},
    }
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = sqlite3.connect(data_dir / "entity_state.db")
    conn.execute("create table demo (id integer primary key, name text)")
    conn.commit()
    conn.close()
    (data_dir / "memory_store").mkdir()
    (data_dir / "memory_store" / "a.txt").write_text("vec", encoding="utf-8")
    (data_dir / "workspace").mkdir()
    (data_dir / "workspace" / "note.txt").write_text("hi", encoding="utf-8")
    for name in ("entity_lifecycle.json", "session_continuity.json", "shutdown_ledger.json"):
        (data_dir / name).write_text("{}", encoding="utf-8")

    service = BackupService(cfg)
    manifest = service.create_backup(label="unit")

    original_copytree = shutil.copytree

    def _failing_copytree(src, dst, *args, **kwargs):
        if Path(src).name == "workspace" and Path(dst).name == "workspace":
            raise OSError("workspace blocked")
        return original_copytree(src, dst, *args, **kwargs)

    mp = pytest.MonkeyPatch()
    mp.setattr(shutil, "copytree", _failing_copytree)
    try:
        result = service.restore_backup(manifest.backup_id)
    finally:
        mp.undo()

    assert result["ok"] is False
    assert any("Workspace restore failed" in err for err in result["errors"])
    assert any(
        evt["operation"] == "restore workspace"
        for evt in service.diagnostics()["recent_failures"]
    )
    assert any(
        entry["source"] == "backup_service"
        and "restore workspace failed" in entry["message"]
        for entry in app_state.log_ring
    )


def test_discord_turn_notifier_failures_are_observable():
    app_state.capability_registry = _CapabilityRegistry()

    def _boom():
        raise RuntimeError("notify boom")

    old = list(discord_bot._turn_notifiers)
    discord_bot._turn_notifiers = [_boom]
    try:
        discord_bot._notify_turns()
    finally:
        discord_bot._turn_notifiers = old

    assert any(
        entry["source"] == "discord"
        and "run turn notifier failed" in entry["message"]
        for entry in app_state.log_ring
    )
    assert app_state.capability_registry.statuses["discord"][0] == "degraded"
