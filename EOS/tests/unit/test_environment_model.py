from __future__ import annotations

from types import SimpleNamespace

from runtime.environment_model import EnvironmentModelService
from tests.conftest import make_spec


class _FakeTopology:
    def __init__(self):
        self.servers = {
            "primary": SimpleNamespace(status=SimpleNamespace(value="ready"), is_ready=lambda: True),
            "tool": SimpleNamespace(status=SimpleNamespace(value="ready"), is_ready=lambda: True),
            "thinking": SimpleNamespace(status=SimpleNamespace(value="error"), is_ready=lambda: False),
        }

    def status_summary(self):
        return {"servers": {k: {"status": v.status.value} for k, v in self.servers.items()}}


class _FakeWorkspaceService:
    def state(self):
        doc = SimpleNamespace(filename="brief.md", path="context/brief.md", size_bytes=128, mtime="2026-03-23T00:00:00Z")
        return SimpleNamespace(total_files=4, context_documents=[doc])

    def root_path(self):
        return "/tmp/workspace"


class _FakeComputerUseService:
    def get_state(self):
        return SimpleNamespace(
            mode="command_only",
            mode_description="Approved apps only",
            active_app_id="browser",
            active_shortcut_id="chrome",
            active_window_title="Chrome - EOS",
            pending_confirmation=None,
            approved_shortcuts=[{"shortcut_id": "chrome", "app_id": "browser", "display_name": "Chrome"}],
        )


def test_environment_model_builds_structured_locations_and_surfaces(registry, minimal_cfg):
    registry.register(make_spec(name="workspace_read", handler=lambda params: "ok"))
    registry.get("workspace_read").pack = "workspace_tools"
    registry.get("workspace_read").tags = ["files"]

    registry.register(make_spec(name="web_search", handler=lambda params: "ok"))
    registry.get("web_search").pack = "web_tools"
    registry.get("web_search").tags = ["web"]

    svc = EnvironmentModelService({
        **minimal_cfg,
        "google": {"enabled": False},
        "discord": {"enabled": False},
    })
    svc.wire(
        topology=_FakeTopology(),
        workspace_service=_FakeWorkspaceService(),
        computer_use_service=_FakeComputerUseService(),
        tool_registry=registry,
    )

    model = svc.build_model()
    data = model.to_dict()

    location_ids = {item["id"] for item in data["locations"]}
    surface_ids = {item["id"] for item in data["surfaces"]}

    assert {"workspace", "desktop", "browser", "runtime"}.issubset(location_ids)
    assert "surface:computer-use" in surface_ids
    assert "surface:workspace-tools" in surface_ids
    assert data["summary"]["resource_count"] >= 3
    assert "Known locations:" in model.prompt_block()
    assert "Environment surfaces relevant to tool choice:" in model.tool_context_block()
