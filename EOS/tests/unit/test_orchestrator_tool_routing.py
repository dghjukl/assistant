"""Unit tests for orchestrator live tool routing."""
from __future__ import annotations

import asyncio
import json

from tests.conftest import make_spec


class _FakeTopology:
    pass


def test_registry_tool_intent_uses_live_executor(monkeypatch, registry):
    import runtime.orchestrator as orch
    import tools.dispatcher as dispatcher

    registry.register(make_spec(
        "echo_tool",
        handler=lambda params: json.dumps({"echo": params["text"]}),
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    ))
    orch.wire_executor(registry)

    async def _fake_extract(tool_intent, topology, available_tools=None):
        assert "echo_tool" in (available_tools or {})
        return {"tool": "echo_tool", "args": {"text": "hello"}}

    monkeypatch.setattr(dispatcher, "extract_tool_call", _fake_extract)

    result = asyncio.run(orch._run_registry_tool_intent("say hello", _FakeTopology()))
    assert json.loads(result) == {"echo": "hello"}


def test_legacy_dispatcher_schema_exposes_list_events():
    from tools.dispatcher import TOOL_SCHEMA

    assert "list_events" in TOOL_SCHEMA
