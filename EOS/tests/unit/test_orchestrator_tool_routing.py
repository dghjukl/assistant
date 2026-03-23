"""Unit tests for orchestrator live tool routing and branch selection."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from runtime.thinking_faculty import ThinkingArtifact
from tests.conftest import make_spec

pytestmark = pytest.mark.release_gate


class _FakeTopology:
    pass


def _patch_turn_side_effects(monkeypatch):
    import runtime.orchestrator as orch

    monkeypatch.setattr(orch, "search_memory", lambda query, top_k=3: [])
    monkeypatch.setattr(orch, "log_interaction", lambda role, text: None)
    monkeypatch.setattr(orch, "remember", lambda text, source="interaction": None)
    monkeypatch.setattr(orch, "record_turn_attention", lambda **kwargs: None)
    monkeypatch.setattr(orch, "should_run_identity_eval", lambda cfg: False)
    return orch


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

    async def _fake_extract(tool_intent, topology, available_tools=None, environment_context=""):
        assert "echo_tool" in (available_tools or {})
        assert environment_context == "known environment"
        return {"tool": "echo_tool", "args": {"text": "hello"}}

    monkeypatch.setattr(dispatcher, "extract_tool_call", _fake_extract)

    snapshot = type("Snap", (), {"environment_tool_context": "known environment"})()
    result = asyncio.run(orch._run_registry_tool_intent("say hello", _FakeTopology(), entity_snapshot=snapshot))
    assert json.loads(result) == {"echo": "hello"}


@pytest.mark.asyncio
async def test_process_turn_direct_answer_branch_uses_primary_route(monkeypatch, runtime_topology_factory):
    orch = _patch_turn_side_effects(monkeypatch)
    topology = runtime_topology_factory(primary="ready", tool="absent", thinking="absent")

    call_args = {}

    async def _fake_call_qwen3(topology_arg, prompt, cfg, *, use_think=False, override_system=None, entity_snapshot=None):
        call_args.update({
            "topology": topology_arg,
            "prompt": prompt,
            "use_think": use_think,
            "override_system": override_system,
        })
        return "direct answer"

    monkeypatch.setattr(orch, "call_qwen3", _fake_call_qwen3)

    result = await orch.process_turn(topology, "What is a closure in Python?", {"qwen3": {}})

    assert result == "direct answer"
    assert call_args["topology"] is topology
    assert call_args["prompt"] == "What is a closure in Python?"
    assert call_args["use_think"] is False


@pytest.mark.asyncio
async def test_process_turn_thinking_escalation_branch_uses_faculty_artifact(monkeypatch, runtime_topology_factory):
    orch = _patch_turn_side_effects(monkeypatch)
    topology = runtime_topology_factory(primary="ready", thinking="ready")

    faculty_calls = []

    class _Faculty:
        async def deliberate(self, task, context=""):
            faculty_calls.append({"task": task, "context": context})
            return ThinkingArtifact(
                analysis="Need to compare trade-offs.",
                options=["1. Keep the old stack", "2. Migrate now"],
                recommendation="Migrate incrementally.",
                confidence=0.82,
            )

    async def _fake_call_qwen3(topology_arg, prompt, cfg, *, use_think=False, override_system=None, entity_snapshot=None):
        assert topology_arg is topology
        assert "Internal reasoning completed" in prompt
        assert "Migrate incrementally." in prompt
        return "escalated answer"

    monkeypatch.setattr(orch, "_get_faculty", lambda topology_arg: _Faculty())
    monkeypatch.setattr(orch, "call_qwen3", _fake_call_qwen3)

    result = await orch.process_turn(topology, "Help me decide whether to migrate our API this quarter.", {"qwen3": {}})

    assert result == "escalated answer"
    assert faculty_calls == [{"task": "Help me decide whether to migrate our API this quarter.", "context": ""}]


@pytest.mark.asyncio
async def test_process_turn_tool_branch_executes_registry_tool_then_summarizes(monkeypatch, registry, runtime_topology_factory):
    orch = _patch_turn_side_effects(monkeypatch)
    topology = runtime_topology_factory(primary="ready", tool="ready")

    registry.register(make_spec(
        "lookup_status",
        handler=lambda params: {"service": params["service"], "status": "green"},
        trust_level="verified_user",
        parameters={
            "type": "object",
            "properties": {"service": {"type": "string"}},
            "required": ["service"],
        },
    ))
    orch.wire_executor(registry)

    import tools.dispatcher as dispatcher

    async def _fake_extract(user_input, topology_arg, available_tools=None, environment_context=""):
        assert topology_arg is topology
        assert "lookup_status" in (available_tools or {})
        return {"tool": "lookup_status", "args": {"service": "tooling"}}

    async def _fake_call_qwen3(topology_arg, prompt, cfg, *, use_think=False, override_system=None, entity_snapshot=None):
        assert topology_arg is topology
        assert "Tool result:" in prompt
        assert "green" in prompt
        return "tool informed answer"

    monkeypatch.setattr(dispatcher, "extract_tool_call", _fake_extract)
    monkeypatch.setattr(orch, "call_qwen3", _fake_call_qwen3)
    monkeypatch.setattr(orch, "should_use_tool", lambda user_input: True)

    result = await orch.process_turn(topology, "Check the tooling service status for me.", {"qwen3": {}})

    assert result == "tool informed answer"


def test_legacy_dispatcher_schema_exposes_list_events():
    from tools.dispatcher import TOOL_SCHEMA

    assert "list_events" in TOOL_SCHEMA
