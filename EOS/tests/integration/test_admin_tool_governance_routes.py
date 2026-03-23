from __future__ import annotations

import pytest

from tests.conftest import make_spec

pytestmark = pytest.mark.release_gate
pytest.importorskip("fastapi")
pytest.importorskip("httpx")


@pytest.fixture
def admin_client(monkeypatch):
    from fastapi.testclient import TestClient
    from webui.server import create_app

    monkeypatch.setenv("EOS_ADMIN_TOKEN", "route-admin-token")

    import core.auth as auth
    auth.load_or_create_token()

    app = create_app()
    app.router.on_startup.clear()
    app.router.on_shutdown.clear()

    with TestClient(
        app,
        raise_server_exceptions=False,
        headers={"X-Admin-Token": "route-admin-token"},
    ) as client:
        yield client


def test_http_admin_routes_preserve_hard_confirm_governance(admin_client, registry, audit_store):
    import runtime.orchestrator as orch
    from webui.app_state import app_state

    executed = []
    registry.register(make_spec(
        name="dangerous_commit",
        handler=lambda params: executed.append(params["payload"]) or {"applied": params["payload"]},
        risk_level="irreversible_commit",
        trust_level="operator_only",
        confirmation_policy="hard_confirm",
        parameters={
            "type": "object",
            "properties": {"payload": {"type": "string"}},
            "required": ["payload"],
        },
    ))

    app_state.tool_registry = registry
    orch.wire_executor(registry, audit_store=audit_store)

    tools_resp = admin_client.get("/admin/tools")
    assert tools_resp.status_code == 200
    assert any(tool["name"] == "dangerous_commit" for tool in tools_resp.json()["data"])

    force_resp = admin_client.post(
        "/admin/diagnostic/force-tool",
        json={"tool_name": "dangerous_commit", "params": {"payload": "deploy"}},
    )
    assert force_resp.status_code == 200
    force_data = force_resp.json()["data"]
    assert force_data["pending_confirmation_id"]
    assert executed == []

    pending_resp = admin_client.get("/admin/tools/pending")
    assert pending_resp.status_code == 200
    pending = pending_resp.json()["data"]
    assert len(pending) == 1
    assert pending[0]["tool_name"] == "dangerous_commit"

    confirm_resp = admin_client.post(f"/admin/tools/confirm/{force_data['pending_confirmation_id']}")
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["data"]["success"] is True
    assert executed == ["deploy"]

    audit_rows = audit_store.query_tool_executions(tool_name="dangerous_commit", limit=5)
    assert audit_rows
    assert audit_rows[0]["success"] == 1


def test_http_admin_routes_respect_registry_enable_disable_and_schema_validation(admin_client, registry, audit_store):
    import runtime.orchestrator as orch
    from webui.app_state import app_state

    registry.register(make_spec(
        name="structured_echo",
        handler=lambda params: {"echo": params["text"]},
        trust_level="operator_only",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    ))

    app_state.tool_registry = registry
    orch.wire_executor(registry, audit_store=audit_store)

    bad_resp = admin_client.post(
        "/admin/diagnostic/force-tool",
        json={"tool_name": "structured_echo", "params": {}},
    )
    assert bad_resp.status_code == 400
    assert "Parameter validation failed" in bad_resp.json()["data"]["error"]

    disable_resp = admin_client.post("/admin/tools/structured_echo/disable")
    assert disable_resp.status_code == 200
    assert registry.get("structured_echo") is None

    disabled_force = admin_client.post(
        "/admin/diagnostic/force-tool",
        json={"tool_name": "structured_echo", "params": {"text": "hi"}},
    )
    assert disabled_force.status_code == 400
    assert "Unknown or disabled tool" in disabled_force.json()["data"]["error"]

    enable_resp = admin_client.post("/admin/tools/structured_echo/enable")
    assert enable_resp.status_code == 200

    good_resp = admin_client.post(
        "/admin/diagnostic/force-tool",
        json={"tool_name": "structured_echo", "params": {"text": "hello"}},
    )
    assert good_resp.status_code == 200
    assert good_resp.json()["data"]["result"] == {"echo": "hello"}
