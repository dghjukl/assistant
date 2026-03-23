from __future__ import annotations

import asyncio
import builtins
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import webui.app_runtime as app_runtime
from webui.app_state import app_state

pytestmark = pytest.mark.release_gate


class _HealthResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _HealthClient:
    def __init__(self, *, calls: list[str], status_code: int = 200):
        self._calls = calls
        self._status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str):
        self._calls.append(url)
        return _HealthResponse(self._status_code)


def _load_module_with_blocked_imports(module_name: str, relative_path: str, blocked: set[str]):
    path = Path(__file__).resolve().parents[2] / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    real_import = builtins.__import__

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.split(".", 1)[0] in blocked:
            raise ImportError(f"blocked import: {name}")
        return real_import(name, globals, locals, fromlist, level)

    original_name = module.__name__
    try:
        builtins.__import__ = _guarded_import
        spec.loader.exec_module(module)
    finally:
        builtins.__import__ = real_import

    assert module.__name__ == original_name
    return module


def test_server_health_loop_uses_runtime_discovery_endpoint(runtime_topology_factory, monkeypatch):
    app_state.topology = runtime_topology_factory(primary="error", tool="absent")
    app_state.primary_degraded = True
    app_state.runtime_discovery = SimpleNamespace(
        services={
            "primary": SimpleNamespace(endpoint="http://127.0.0.1:19191", status="active"),
        }
    )

    sleep_calls = {"count": 0}
    requests: list[str] = []

    async def _sleep_once(_seconds: float):
        sleep_calls["count"] += 1
        if sleep_calls["count"] > 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr(app_runtime.asyncio, "sleep", _sleep_once)
    monkeypatch.setattr(
        app_runtime.httpx,
        "AsyncClient",
        lambda timeout=5: _HealthClient(calls=requests, status_code=200),
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app_runtime._server_health_loop())

    assert requests == ["http://127.0.0.1:19191/health"]
    assert app_state.topology.server("primary").is_ready()
    assert app_state.primary_degraded is False


def test_background_task_tracker_records_health_loop_failure():
    async def _boom():
        raise RuntimeError("health loop failed")

    async def _run():
        task = app_runtime._track_background_task("server_health_loop", asyncio.create_task(_boom()))
        await asyncio.sleep(0)
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_run())

    assert any(
        issue["category"] == "background_task_failure"
        and issue["component"] == "server_health_loop"
        for issue in app_state.startup_issues
    )


def test_admin_degradation_status_returns_state_backed_service_data(runtime_topology_factory):
    app_state.topology = runtime_topology_factory(primary="ready", tool="error")
    app_state.topology.server("primary").pid = 4242
    app_state.runtime_discovery = SimpleNamespace(
        services={
            "primary": SimpleNamespace(endpoint="http://127.0.0.1:28080", status="active"),
            "tool": SimpleNamespace(endpoint="http://127.0.0.1:28081", status="unavailable"),
        }
    )
    app_state.primary_degraded = False

    response = asyncio.run(app_runtime.admin_degradation_status())
    payload = json.loads(response.body)

    assert payload["ok"] is True
    assert payload["data"]["chat_available"] is True
    assert payload["data"]["servers_up"] == [{
        "role": "primary",
        "status": "ready",
        "error": None,
        "pid": 4242,
        "port": 18080,
        "endpoint": "http://127.0.0.1:28080",
        "discovery_status": "active",
    }]
    assert payload["data"]["servers_down"] == [{
        "role": "tool",
        "status": "error",
        "error": "forced error",
        "pid": None,
        "port": 18081,
        "endpoint": "http://127.0.0.1:28081",
        "discovery_status": "unavailable",
    }]


def test_stt_module_imports_without_optional_dependencies():
    module = _load_module_with_blocked_imports(
        "test_services_stt_missing",
        "services/stt.py",
        {"numpy", "sounddevice", "faster_whisper"},
    )

    assert module.STT_AVAILABLE is False
    with pytest.raises(RuntimeError, match="Missing optional STT dependencies"):
        module.transcribe_array(object(), {"stt": {}})


def test_vision_module_imports_without_optional_dependencies(runtime_topology_factory):
    module = _load_module_with_blocked_imports(
        "test_services_vision_missing",
        "services/vision.py",
        {"cv2", "mss", "numpy", "PIL"},
    )

    assert module.VISION_AVAILABLE is False
    topology = runtime_topology_factory(primary="ready")

    with pytest.raises(RuntimeError, match="Missing optional vision dependencies"):
        module.capture_screen()

    result = asyncio.run(module.describe_screen(topology))
    assert "Vision unavailable" in result


def test_startup_guidance_detects_missing_backends():
    from runtime.startup_health import START_BACKENDS_MESSAGE, detect_startup_guidance

    runtime_discovery = SimpleNamespace(
        config={"server_activation": {"baseline_roles": ["primary", "tool"]}},
        services={
            "primary": SimpleNamespace(status="unavailable"),
            "tool": SimpleNamespace(status="unavailable"),
            "thinking": SimpleNamespace(status="unavailable"),
        }
    )

    assert detect_startup_guidance(runtime_discovery) == START_BACKENDS_MESSAGE
