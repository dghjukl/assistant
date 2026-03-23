from __future__ import annotations

import asyncio
import socket
from pathlib import Path

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


def test_on_demand_port_conflict_skips_launch(monkeypatch):
    from runtime.on_demand import OnDemandServerManager

    class _Topology:
        def server(self, role):
            return None

        def mark_error(self, role, error):
            self.last_error = (role, error)

        def mark_starting(self, role, pid):
            raise AssertionError("launch should be skipped when the port is already bound")

        def mark_ready(self, role, pid):
            raise AssertionError("launch should be skipped when the port is already bound")

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    host, port = sock.getsockname()

    cfg = {
        "servers": {
            "tool": {
                "host": host,
                "port": port,
                "health_timeout": 0.1,
                "binary_cpu": "bin/llama-server",
                "model_path": "models/tool",
            }
        }
    }
    topology = _Topology()
    manager = OnDemandServerManager(cfg, Path('.'), topology)

    called = {"launch": False}

    def _unexpected_launch(*args, **kwargs):
        called["launch"] = True
        raise AssertionError("launch_server should not be called")

    monkeypatch.setattr("runtime.server_runtime.launch_server", _unexpected_launch)

    try:
        assert asyncio.run(manager.ensure("tool")) is None
        assert topology.last_error == ("tool", "port already bound")
        assert called["launch"] is False
    finally:
        sock.close()


def test_admin_token_auth_respects_external_origin(tmp_path):
    from core.access_control import init_access_controller
    from core.auth import load_or_create_token
    from webui.server import create_app

    init_access_controller(tmp_path, {"access_tiers": {"external": {"enabled": True, "admin_enabled": False}}})
    token = load_or_create_token(tmp_path)

    app = create_app()
    app.router.on_startup.clear()
    app.router.on_shutdown.clear()

    @app.get('/admin/probe')
    async def _probe():
        return JSONResponse({"ok": True})

    with TestClient(
        app,
        raise_server_exceptions=False,
        base_url='http://127.0.0.1:7860',
        headers={
            'X-Forwarded-For': '8.8.8.8',
            'X-Admin-Token': token,
        },
    ) as client:
        response = client.get('/admin/probe')

    assert response.status_code == 403
    assert response.json()["error"] == "Admin access from external is disabled"


def test_dispatcher_execute_routes_through_tool_executor(registry):
    import tools.dispatcher as dispatcher
    import runtime.orchestrator as orch
    from tests.conftest import make_spec

    registry.register(make_spec(
        "compat_tool",
        handler=lambda params: {"echo": params["text"]},
        trust_level="verified_user",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    ))
    orch.wire_executor(registry)

    result = asyncio.run(dispatcher.execute("compat_tool", {"text": "hello"}, topology=None, cfg={}))

    assert result == '{"echo": "hello"}'


def test_dispatcher_execute_reports_legacy_path_disabled():
    import tools.dispatcher as dispatcher

    result = asyncio.run(dispatcher.execute("legacy_only_tool", {}, topology=None, cfg={}))

    assert "Legacy TOOL_REGISTRY execution is disabled" in result
