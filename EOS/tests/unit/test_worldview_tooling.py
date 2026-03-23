from __future__ import annotations

import json

from runtime.tool_registry import ToolRegistry
from runtime.toolpacks import workspace_tools


def _cfg(tmp_path):
    return {
        "project_root": str(tmp_path),
        "workspace_tools": {
            "enabled": True,
            "workspace_root": "data/workspace",
            "allow_delete": False,
            "allow_exec": False,
        },
        "worldview": {
            "enabled": True,
            "worldview_path": "data/worldview",
        },
    }


def test_worldview_profile_is_readable_via_dedicated_tool_while_workspace_read_stays_confined(tmp_path):
    cfg = _cfg(tmp_path)
    profile_path = tmp_path / "data" / "worldview" / "profile.md"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text("# Partner Worldview Profile\n\n- Calibration signal.\n", encoding="utf-8")

    workspace_file = tmp_path / "data" / "workspace" / "context" / "brief.md"
    workspace_file.parent.mkdir(parents=True, exist_ok=True)
    workspace_file.write_text("workspace brief", encoding="utf-8")

    registry = ToolRegistry()
    workspace_tools.register(registry, cfg)

    worldview_payload = json.loads(registry.get("worldview_read").handler({"path": "data/worldview/profile.md"}))
    assert worldview_payload["path"] == "data/worldview/profile.md"
    assert "Calibration signal" in worldview_payload["content"]

    blocked = json.loads(registry.get("workspace_read").handler({"path": "../worldview/profile.md"}))
    assert "outside workspace" in blocked["error"]

    workspace_payload = json.loads(registry.get("workspace_read").handler({"path": "context/brief.md"}))
    assert workspace_payload["path"] == "context/brief.md"
    assert workspace_payload["content"] == "workspace brief"


def test_worldview_read_only_allows_documented_profile_path(tmp_path):
    cfg = _cfg(tmp_path)
    profile_path = tmp_path / "data" / "worldview" / "profile.md"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text("# Partner Worldview Profile\n", encoding="utf-8")

    registry = ToolRegistry()
    workspace_tools.register(registry, cfg)

    default_payload = json.loads(registry.get("worldview_read").handler({}))
    assert default_payload["path"] == "data/worldview/profile.md"

    short_payload = json.loads(registry.get("worldview_read").handler({"path": "profile.md"}))
    assert short_payload["path"] == "data/worldview/profile.md"

    rejected = json.loads(registry.get("worldview_read").handler({"path": "data/worldview/extraction_log.json"}))
    assert rejected["supported_path"] == "data/worldview/profile.md"
    assert "only supports" in rejected["error"]
