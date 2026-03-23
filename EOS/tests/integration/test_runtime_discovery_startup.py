from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.release_gate
pytest.importorskip("fastapi")
pytest.importorskip("httpx")


def _write_runtime_config(tmp_path, *, primary_port: int, tool_port: int, thinking_port: int, vision_port: int | None = None, primary_multimodal: bool = False, google_enabled: bool = False, discord_enabled: bool = False):
    cfg = {
        "deployment_mode": "standard",
        "db_path": str(tmp_path / "entity_state.db"),
        "retrieval": {"chroma_path": str(tmp_path / "chroma")},
        "primary": {"is_multimodal": primary_multimodal},
        "google": {"enabled": google_enabled},
        "discord": {"enabled": discord_enabled},
        "stt": {"model_path": "models/stt.bin"},
        "tts": {"binary": "Piper/piper.exe", "model_path": "Piper/voice.onnx"},
        "servers": {
            "primary": {"enabled": True, "required": True, "host": "127.0.0.1", "port": primary_port},
            "tool": {"enabled": True, "required": False, "host": "127.0.0.1", "port": tool_port},
            "thinking": {"enabled": True, "required": False, "host": "127.0.0.1", "port": thinking_port},
            "creativity": {"enabled": False, "required": False, "host": "127.0.0.1", "port": 18084},
        },
    }
    if vision_port is not None:
        cfg["servers"]["vision"] = {"enabled": True, "required": False, "host": "127.0.0.1", "port": vision_port}
    path = tmp_path / "runtime_config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def _patch_lightweight_startup(monkeypatch):
    import webui.app_runtime as app_runtime

    async def _noop_forever(*args, **kwargs):
        return None

    class _NoopService:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def wire(self, **kwargs):
            self.wired = kwargs

        def start(self):
            return None

        def state(self):
            return SimpleNamespace(context_documents=[], total_files=0)

        def root_path(self):
            return "/tmp/workspace"

        def profile_summary(self):
            return {"profile_exists": False, "sources_processed": 0}

        def integrity_check(self):
            return SimpleNamespace(ok=True, findings=[])

        def needs_auto_backup(self):
            return False

        def list_backups(self):
            return []

        def diagnostics(self):
            return {"last_run_at": None, "next_run_at": None, "recent_failure": None, "recent_failures": []}

        def mark_auto_backup_run(self, **kwargs):
            self.marked_auto_backup = ("run", kwargs)

        def mark_auto_backup_failure(self, exc, **kwargs):
            self.marked_auto_backup = ("failure", str(exc), kwargs)

        def lifecycle_summary(self):
            return SimpleNamespace(boot_count=1, boot_reason="startup", entity_id="entity-1", compact=lambda: "boot=1")

        def has_prior_session(self):
            return False

        def active_count(self):
            return 0

        def snapshot_count(self):
            return 0

        def stability_score(self):
            return 1.0

        def get_diagnostics(self):
            return {"ok": True}

    class _NoopTaskStarter(_NoopService):
        def start_idle_loop(self):
            return asyncio.create_task(_noop_forever())

        async def run_loop(self, *args, **kwargs):
            return None

    class _ManifestLoader:
        def __init__(self, registry, config):
            self.registry = registry
            self.config = config

        def load_all(self):
            return {"packs": [], "summary": {"loaded": 0, "total": 0, "failed": 0}}

    class _CapabilityRegistry:
        def __init__(self):
            self._caps = {}

        def all(self):
            return list(self._caps.values())

        def set_status(self, name, status, reason=None):
            self._caps[name] = {"name": name, "status": str(status), "reason": reason}

        def get(self, name):
            return self._caps.get(name)

        def register(self, entry):
            self._caps[entry.name] = entry

    monkeypatch.setattr(app_runtime, "memory_configure", lambda cfg: None)
    monkeypatch.setattr(app_runtime, "orchestrator_startup", lambda cfg: None)
    monkeypatch.setattr(app_runtime, "_ensure_runtime_dirs", lambda root: None)
    monkeypatch.setattr(app_runtime, "load_or_create_token", lambda data_dir=None: "test-admin-token")
    monkeypatch.setattr(app_runtime, "get_token_file_path", lambda: Path("/tmp/admin_token.txt"))
    monkeypatch.setattr(app_runtime, "init_audit_store", lambda path: None)
    monkeypatch.setattr(app_runtime, "init_access_controller", lambda data_dir=None, cfg=None: None)
    monkeypatch.setattr(app_runtime, "init_secrets", lambda data_dir=None: None)
    monkeypatch.setattr(app_runtime, "_server_health_loop", _noop_forever)
    monkeypatch.setattr(app_runtime, "_bus_poll_loop", _noop_forever)
    monkeypatch.setattr(app_runtime, "_initiative_loop", _noop_forever)
    monkeypatch.setattr(app_runtime, "_memory_maintenance_loop", _noop_forever)
    monkeypatch.setattr(app_runtime, "_backup_loop", _noop_forever)
    monkeypatch.setattr(app_runtime, "_wire_signal_subscribers", lambda: None)
    monkeypatch.setattr(app_runtime, "shutdown_event", _noop_forever)

    import runtime.on_demand as on_demand
    monkeypatch.setattr(on_demand, "init_on_demand_manager", lambda *args, **kwargs: _NoopTaskStarter())

    import runtime.crash_recovery as crash_recovery
    monkeypatch.setattr(crash_recovery, "CrashRecoveryService", lambda cfg: SimpleNamespace(record_boot=lambda config_mode=None: SimpleNamespace(is_crash_recovery=lambda: False, previous_shutdown_kind="clean", to_dict=lambda: {}, admin_summary=lambda: "clean")))

    import runtime.entity_lifecycle as entity_lifecycle
    monkeypatch.setattr(entity_lifecycle, "EntityLifecycleService", lambda cfg, crash_report=None: _NoopService())

    import runtime.entity_state_service as entity_state_service
    monkeypatch.setattr(entity_state_service, "EntityStateService", lambda cfg: _NoopService())

    import runtime.current_focus as current_focus
    monkeypatch.setattr(current_focus, "CurrentFocusService", lambda: _NoopService())

    import runtime.session_continuity as session_continuity
    monkeypatch.setattr(session_continuity, "SessionContinuityService", lambda cfg: _NoopService())

    import core.intent as intent
    monkeypatch.setattr(intent, "GoalStore", lambda db_path: _NoopService())

    import runtime.workspace_service as workspace_service
    monkeypatch.setattr(workspace_service, "WorkspaceService", lambda cfg: _NoopService())

    import core.worldview as worldview
    monkeypatch.setattr(worldview, "WorldviewService", lambda cfg: _NoopService())

    import runtime.backup_service as backup_service
    monkeypatch.setattr(backup_service, "BackupService", lambda cfg: _NoopService())

    monkeypatch.setattr(app_runtime, "CognitionTracer", None)
    monkeypatch.setattr(app_runtime, "SignalBus", None)

    import runtime.capability_registry as capability_registry
    monkeypatch.setattr(capability_registry, "build_default_registry", lambda cfg, topology: _CapabilityRegistry())

    import runtime.system_sensors as system_sensors
    monkeypatch.setattr(system_sensors, "SensorPoller", lambda cfg, topology: _NoopService())

    import runtime.backend_health_probe as backend_health_probe
    monkeypatch.setattr(backend_health_probe, "BackendHealthProbe", lambda **kwargs: _NoopService())

    import runtime.idle_cognition as idle_cognition
    monkeypatch.setattr(idle_cognition, "IdleCognitionEngine", lambda cfg: _NoopService())

    import runtime.identity_continuity as identity_continuity
    monkeypatch.setattr(identity_continuity, "IdentityContinuityMonitor", lambda db_path: _NoopService())

    import runtime.initiative_engine as initiative_engine
    monkeypatch.setattr(initiative_engine, "InitiativeEngine", lambda cfg: _NoopService())

    import runtime.investigation_engine as investigation_engine
    monkeypatch.setattr(investigation_engine, "InvestigationEngine", lambda cfg: _NoopService())

    import runtime.toolpack_loader as toolpack_loader
    monkeypatch.setattr(toolpack_loader, "ToolpackLoader", _ManifestLoader)

    import runtime.reflection_pipeline as reflection_pipeline
    monkeypatch.setattr(reflection_pipeline, "ReflectionPipeline", lambda cfg: _NoopTaskStarter())


@pytest.mark.asyncio
async def test_runtime_discovery_reports_fake_backends_and_local_service_degradation(tmp_path, backend_server_factory):
    from runtime.service_discovery import discover_runtime

    primary = backend_server_factory(chat_reply="primary response")
    tool = backend_server_factory(chat_reply="tool response")
    thinking = backend_server_factory(chat_reply="thinking response")

    config_path = _write_runtime_config(
        tmp_path,
        primary_port=primary.port,
        tool_port=tool.port,
        thinking_port=thinking.port,
    )

    discovery = discover_runtime(config_path, root=tmp_path)

    assert discovery.services["primary"].status == "active"
    assert discovery.services["tool"].status == "active"
    assert discovery.services["thinking"].status == "active"
    assert discovery.services["stt"].status == "unavailable"
    assert discovery.services["tts"].status == "unavailable"
    assert discovery.capabilities["chat"] == "available"
    assert discovery.capabilities["tools"] == "available"
    assert discovery.capabilities["reasoning"] == "available"
    assert discovery.capabilities["voice"] == "unavailable"


@pytest.mark.asyncio
async def test_startup_event_sets_user_guidance_when_no_backends_are_running(monkeypatch, tmp_path):
    import webui.app_runtime as app_runtime
    from webui.app_state import app_state

    config_path = _write_runtime_config(
        tmp_path,
        primary_port=28080,
        tool_port=28082,
        thinking_port=28083,
    )
    _patch_lightweight_startup(monkeypatch)

    await app_runtime.startup_event(config_path=config_path)

    assert app_state.startup_guidance == "Start baseline backend services before using UI"




def test_runtime_discovery_degrades_vision_and_optional_tool_helpers(tmp_path, backend_server_factory):
    from runtime.service_discovery import discover_runtime

    primary = backend_server_factory(chat_reply="primary response")
    unhealthy_tool = backend_server_factory(health_status=503, health_body="offline")
    failed_vision = backend_server_factory(health_status=500, health_body="vision down")

    config_path = _write_runtime_config(
        tmp_path,
        primary_port=primary.port,
        tool_port=unhealthy_tool.port,
        thinking_port=unhealthy_tool.port,
        vision_port=failed_vision.port,
        primary_multimodal=False,
    )

    discovery = discover_runtime(config_path, root=tmp_path)

    assert discovery.services["tool"].status == "unavailable"
    assert discovery.services["tool"].fallback == "fallback to main"
    assert discovery.services["thinking"].status == "degraded"
    assert discovery.services["vision"].status == "unavailable"
    assert discovery.capabilities["tools"] == "degraded"
    assert discovery.capabilities["reasoning"] == "available"
    assert discovery.capabilities["vision"] == "unavailable"

@pytest.mark.asyncio
async def test_fastapi_startup_path_uses_runtime_discovery_and_degrades_optional_services(monkeypatch, tmp_path, backend_server_factory):
    from fastapi.testclient import TestClient
    from webui.app_state import app_state
    from webui.server import create_app

    primary = backend_server_factory(chat_reply="primary ok")
    tool = backend_server_factory(chat_reply="tool ok")
    thinking = backend_server_factory(chat_reply="thinking ok")

    config_path = _write_runtime_config(
        tmp_path,
        primary_port=primary.port,
        tool_port=tool.port,
        thinking_port=thinking.port,
        google_enabled=True,
        discord_enabled=True,
    )

    _patch_lightweight_startup(monkeypatch)
    monkeypatch.setenv("EOS_ADMIN_TOKEN", "test-admin-token")

    import core.google_oauth as google_oauth
    monkeypatch.setattr(google_oauth, "configure", lambda cfg: (_ for _ in ()).throw(RuntimeError("google unavailable")))

    def _discord_fail(*args, **kwargs):
        raise RuntimeError("discord offline")

    monkeypatch.setitem(sys.modules, "interfaces.discord_bot", SimpleNamespace(start=_discord_fail))

    app = create_app(config_path=config_path)
    with TestClient(app, raise_server_exceptions=False, headers={"X-Admin-Token": "test-admin-token"}) as client:
        status_resp = client.get("/api/auth/verify")
        assert status_resp.status_code == 200

    assert app_state.topology is not None
    assert app_state.runtime_discovery.services["primary"].status == "active"
    assert app_state.runtime_discovery.services["tool"].status == "active"
    assert app_state.runtime_discovery.services["thinking"].status == "active"
    assert app_state.runtime_discovery.services["stt"].status == "unavailable"
    assert app_state.runtime_discovery.services["tts"].status == "unavailable"
    assert any("Discord bot failed to start" in entry["message"] for entry in app_state.log_ring)


def test_eos_main_status_prints_runtime_summary_without_booting_uvicorn(monkeypatch, capsys, tmp_path, backend_server_factory):
    import eos

    primary = backend_server_factory(chat_reply="primary response")
    tool = backend_server_factory(chat_reply="tool response")
    thinking = backend_server_factory(chat_reply="thinking response")

    config_path = _write_runtime_config(
        tmp_path,
        primary_port=primary.port,
        tool_port=tool.port,
        thinking_port=thinking.port,
    )

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", lambda self: argparse.Namespace(config=str(config_path), host=None, port=None, status=True, profile=None, no_boot=False))

    eos.main()
    output = capsys.readouterr().out
    assert "Runtime Discovery" in output
    assert "Main model: active" in output
    assert "tools: available" in output
    assert "reasoning: available" in output
    assert "voice: unavailable" in output


def test_eos_main_passes_config_path_directly_to_webui(monkeypatch, tmp_path, backend_server_factory):
    import eos

    primary = backend_server_factory(chat_reply="primary response")
    tool = backend_server_factory(chat_reply="tool response")
    thinking = backend_server_factory(chat_reply="thinking response")

    config_path = _write_runtime_config(
        tmp_path,
        primary_port=primary.port,
        tool_port=tool.port,
        thinking_port=thinking.port,
    )

    monkeypatch.setattr(
        argparse.ArgumentParser,
        "parse_args",
        lambda self: argparse.Namespace(
            config=str(config_path),
            host=None,
            port=None,
            status=False,
            profile=None,
            no_boot=False,
        ),
    )

    captured = {}

    def _fake_run(app, **kwargs):
        captured["config_path"] = Path(app.state.config_path)
        captured["kwargs"] = kwargs

    monkeypatch.setattr("uvicorn.run", _fake_run)

    eos.main()

    assert captured["config_path"] == config_path
    assert captured["kwargs"]["host"] == "127.0.0.1"
    assert captured["kwargs"]["port"] == 7860
