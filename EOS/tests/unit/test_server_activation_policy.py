from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.server_activation import (
    ActivationRequest,
    OperatingModeResolver,
    ResourceSnapshot,
    ResourceSnapshotProvider,
    ServerActivationPolicy,
)


class _FixedResourceProvider(ResourceSnapshotProvider):
    def __init__(self, snapshot: ResourceSnapshot):
        self._snapshot = snapshot

    def snapshot(self) -> ResourceSnapshot:  # type: ignore[override]
        return self._snapshot


def _cfg() -> dict:
    return {
        "servers": {
            "primary": {"enabled": True, "required": True, "port": 8080},
            "tool": {"enabled": True, "required": False, "port": 8082},
            "thinking": {"enabled": True, "required": False, "port": 8083},
            "creativity": {"enabled": True, "required": False, "port": 8084},
        }
    }


def test_policy_uses_higher_threshold_in_active_mode_and_lower_threshold_in_idle_mode():
    snapshot = ResourceSnapshot(sampled_at=0.0, cpu_percent=20.0, ram_free_mb=8192.0, ram_used_percent=30.0, source="test")
    provider = _FixedResourceProvider(snapshot)
    resolver = OperatingModeResolver(_cfg())
    policy = ServerActivationPolicy(_cfg(), resource_provider=provider, operating_mode_resolver=resolver)

    request = ActivationRequest(role="thinking", task_type="reflection", requested_by="executive")

    active = policy.evaluate(request, last_interaction_age_s=10.0)
    idle = policy.evaluate(request, last_interaction_age_s=7200.0)

    assert active.allowed is False
    assert active.mode == "active_interaction"
    assert idle.allowed is True
    assert idle.mode == "idle_reflection"
    assert idle.threshold < active.threshold


def test_policy_denies_activation_when_resource_headroom_is_insufficient():
    snapshot = ResourceSnapshot(sampled_at=0.0, cpu_percent=20.0, ram_free_mb=256.0, ram_used_percent=95.0, source="test")
    provider = _FixedResourceProvider(snapshot)
    resolver = OperatingModeResolver(_cfg())
    policy = ServerActivationPolicy(_cfg(), resource_provider=provider, operating_mode_resolver=resolver)

    decision = policy.evaluate(
        ActivationRequest(role="thinking", task_type="deep_reasoning", escalation=True),
        last_interaction_age_s=7200.0,
    )

    assert decision.allowed is False
    assert "RAM" in decision.reason


@pytest.mark.asyncio
async def test_on_demand_manager_starts_auxiliary_server_when_policy_allows(monkeypatch, runtime_topology_factory):
    from runtime.on_demand import OnDemandServerManager

    topology = runtime_topology_factory(primary="ready", thinking="absent")
    cfg = _cfg()

    class _Proc:
        pid = 4321

        def terminate(self):
            return None

    monkeypatch.setattr("runtime.server_runtime.is_port_bound", lambda *args, **kwargs: False)
    monkeypatch.setattr("runtime.server_runtime.launch_server", lambda *args, **kwargs: _Proc())
    monkeypatch.setattr("runtime.server_runtime.wait_for_health_with_retry", lambda *args, **kwargs: True)

    manager = OnDemandServerManager(
        cfg,
        Path("."),
        topology,
        interaction_age_provider=lambda: 5.0,
    )

    endpoint = await manager.ensure(
        "thinking",
        reason="need deeper reasoning",
        task_type="deep_reasoning",
        escalation=True,
        requested_by="executive",
    )

    assert endpoint == "http://127.0.0.1:8083"
    assert topology.server("thinking").is_ready()
    assert manager.status()["roles"]["thinking"]["last_decision"]["allowed"] is True


@pytest.mark.asyncio
async def test_on_demand_manager_tears_down_idle_auxiliary_server(monkeypatch, runtime_topology_factory):
    from runtime.on_demand import OnDemandServerManager

    topology = runtime_topology_factory(primary="ready", thinking="ready")
    cfg = _cfg()
    cfg["servers"]["thinking"]["idle_timeout_seconds"] = 0.1
    cfg["servers"]["thinking"]["min_uptime_seconds"] = 0.0

    manager = OnDemandServerManager(cfg, Path("."), topology, interaction_age_provider=lambda: 5.0)
    manager._last_used["thinking"] = 0.0
    manager._last_started["thinking"] = 0.0

    calls: list[str] = []

    _sleep_calls = {"count": 0}

    async def _fake_sleep(_seconds):
        _sleep_calls["count"] += 1
        if _sleep_calls["count"] > 1:
            raise asyncio.CancelledError()
        return None

    async def _fake_stop(role: str, *, reason: str = "idle", apply_cooldown: bool = True):
        calls.append(f"{role}:{reason}:{apply_cooldown}")

    monkeypatch.setattr("runtime.on_demand.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr(manager, "_stop", _fake_stop)
    monkeypatch.setattr("runtime.on_demand.time.monotonic", lambda: 999.0)

    with pytest.raises(asyncio.CancelledError):
        await manager._idle_loop(check_interval=0.0)

    assert calls == ["thinking:idle timeout:True"]


@pytest.mark.asyncio
async def test_thinking_faculty_falls_back_to_primary_when_auxiliary_cannot_be_started(monkeypatch, runtime_topology_factory):
    import runtime.thinking_faculty as thinking_faculty

    topology = runtime_topology_factory(primary="ready", thinking="absent")

    class _Manager:
        async def ensure(self, *args, **kwargs):
            return None

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {"message": {"content": "ANALYSIS:\nFallback.\n\nOPTIONS:\n1. Use primary\n\nRECOMMENDATION:\nUse primary\n\nCONFIDENCE: 0.7"}}
                ]
            }

    captured = {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            captured["url"] = url
            captured["json"] = json
            return _Response()

    monkeypatch.setattr(thinking_faculty, "httpx", SimpleNamespace(AsyncClient=lambda timeout=120: _Client()))
    monkeypatch.setattr("runtime.on_demand.get_on_demand_manager", lambda: _Manager())

    faculty = thinking_faculty.ThinkingFaculty(topology)
    artifact = await faculty.deliberate("Need help")

    assert captured["url"].startswith("http://127.0.0.1:18080")
    assert artifact.recommendation == "Use primary"
