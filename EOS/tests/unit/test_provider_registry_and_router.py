"""
Unit tests for ProviderRegistry and InferenceRouter.

Tests cover:
- ProviderRegistry: register, get, all, ids, capabilities_map, summary
- InferenceRouter.DEFAULT: uses default_provider, single candidate
- InferenceRouter.EXPLICIT: uses named provider/model regardless of enabled list
- InferenceRouter.FALLBACK: follows fallback_order, only enabled providers
- InferenceRouter.CHEAPEST: orders by cost_tier descending (4=free wins)
- InferenceRouter.BEST_QUALITY: orders by quality_tier ascending (1=premium wins)
- InferenceRouter.LOCAL_ONLY: filters to is_local providers
- InferenceRouter.REMOTE_ONLY: filters to non-local providers
- Router: first successful provider is returned; failed providers are skipped
- Router: all_providers_failed when every candidate fails or has no key
- Router: no_viable_providers when candidate list is empty
- Router: budget_insufficient skips expensive providers
- Router: no_api_key skips non-local providers without a key
- Router: local providers proceed without a key
- Router: RouteRecord.failure_reasons populated on skip/failure
- RoutingMode enum has all expected values
- VALID_ROUTING_MODES set matches RoutingMode enum
"""
from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock

import pytest

from runtime.providers.base import ProviderCapabilities, ProviderResult
from runtime.providers.registry import ProviderRegistry
from runtime.providers.router import (
    InferenceRouter,
    RoutingMode,
    RoutingRequest,
    VALID_ROUTING_MODES,
)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_adapter(
    provider_id: str,
    *,
    quality_tier: int = 2,
    cost_tier: int = 2,
    is_local: bool = False,
    supports_tool_calling: bool = False,
    supports_vision: bool = False,
    ok: bool = True,
    content: str = "reply",
    error_code: str = "",
) -> MagicMock:
    """Create a fake BaseProvider adapter."""
    caps = ProviderCapabilities(
        provider_id=provider_id,
        quality_tier=quality_tier,
        cost_tier=cost_tier,
        is_local=is_local,
        supports_tool_calling=supports_tool_calling,
        supports_streaming=True,
        supports_vision=supports_vision,
        default_model=f"{provider_id}-default",
    )
    result = ProviderResult(
        ok=ok, content=content, provider=provider_id,
        model_id=caps.default_model, error="" if ok else "mock error",
        error_code=error_code if not ok else "",
    )
    adapter = MagicMock()
    adapter.provider_id = provider_id
    adapter.capabilities = caps
    adapter.complete.return_value = result
    adapter.with_model.side_effect = lambda m: adapter  # return same mock
    return adapter


def _registry(*adapters) -> ProviderRegistry:
    reg = ProviderRegistry()
    for a in adapters:
        reg.register(a)
    return reg


def _router(
    registry: ProviderRegistry,
    enabled: list,
    default: str,
    fallback_order: Optional[list] = None,
) -> InferenceRouter:
    return InferenceRouter(
        registry=registry,
        enabled_providers=enabled,
        default_provider=default,
        fallback_order=fallback_order or list(enabled),
    )


def _no_key(_provider_id: str) -> Optional[str]:
    return None


def _key_for(*providers):
    """Return a get_api_key callable that provides keys only for listed providers."""
    provider_set = set(providers)
    return lambda pid: "testkey" if pid in provider_set else None


# ── ProviderRegistry ──────────────────────────────────────────────────────────


class TestProviderRegistry:
    def test_register_and_get(self):
        reg = ProviderRegistry()
        a = _make_adapter("openai")
        reg.register(a)
        assert reg.get("openai") is a

    def test_get_unknown_returns_none(self):
        reg = ProviderRegistry()
        assert reg.get("nonexistent") is None

    def test_ids(self):
        reg = _registry(_make_adapter("a"), _make_adapter("b"), _make_adapter("c"))
        assert reg.ids() == ["a", "b", "c"]

    def test_all_returns_all_adapters(self):
        a1 = _make_adapter("p1")
        a2 = _make_adapter("p2")
        reg = _registry(a1, a2)
        assert set(reg.all()) == {a1, a2}

    def test_register_replaces_existing(self):
        reg = ProviderRegistry()
        a1 = _make_adapter("openai")
        a2 = _make_adapter("openai")
        reg.register(a1)
        reg.register(a2)
        assert reg.get("openai") is a2
        assert len(reg.ids()) == 1

    def test_capabilities_map(self):
        a = _make_adapter("anthropic", quality_tier=1)
        reg = _registry(a)
        caps = reg.capabilities_map()
        assert "anthropic" in caps
        assert caps["anthropic"].quality_tier == 1

    def test_summary_serialisable(self):
        reg = _registry(_make_adapter("hf", cost_tier=3))
        rows = reg.summary()
        assert isinstance(rows, list)
        row = rows[0]
        assert row["provider_id"] == "hf"
        assert row["cost_tier"] == 3
        # Every key should be a primitive (JSON-serialisable)
        for v in row.values():
            assert isinstance(v, (str, int, float, bool))


# ── RoutingMode enum ──────────────────────────────────────────────────────────


class TestRoutingModeEnum:
    def test_all_expected_values_exist(self):
        expected = {"explicit", "default", "cheapest", "best_quality",
                    "local_only", "remote_only", "fallback"}
        assert VALID_ROUTING_MODES == expected

    def test_enum_values_match_set(self):
        for mode in RoutingMode:
            assert mode.value in VALID_ROUTING_MODES


# ── InferenceRouter — candidate selection ─────────────────────────────────────


class TestRouterCandidateSelection:
    """Tests that verify the candidate list before any HTTP call."""

    def test_default_mode_uses_default_provider(self):
        hf = _make_adapter("huggingface")
        reg = _registry(hf, _make_adapter("openai"))
        router = _router(reg, ["huggingface", "openai"], "huggingface")
        result, record = router.route(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                           routing_mode=RoutingMode.DEFAULT),
            get_api_key=_key_for("huggingface"),
        )
        assert result.ok
        assert record.selected == "huggingface"

    def test_explicit_mode_uses_named_provider(self):
        hf = _make_adapter("huggingface")
        reg = _registry(hf)
        router = _router(reg, ["huggingface"], "huggingface")
        result, record = router.route(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                           routing_mode=RoutingMode.EXPLICIT,
                           explicit_provider="huggingface",
                           explicit_model="some-model"),
            get_api_key=_key_for("huggingface"),
        )
        assert result.ok
        assert record.selected == "huggingface"
        # with_model was called with the explicit model
        hf.with_model.assert_called_once_with("some-model")

    def test_explicit_mode_no_provider_returns_no_viable(self):
        reg = _registry(_make_adapter("hf"))
        router = _router(reg, ["hf"], "hf")
        result, _ = router.route(
            RoutingRequest(messages=[], routing_mode=RoutingMode.EXPLICIT,
                           explicit_provider=None),
            get_api_key=_key_for("hf"),
        )
        assert not result.ok
        assert result.error_code == "no_viable_providers"

    def test_fallback_mode_follows_order_skipping_disabled(self):
        hf   = _make_adapter("huggingface", ok=False, error_code="timeout")
        oai  = _make_adapter("openai")
        gem  = _make_adapter("gemini")
        reg  = _registry(hf, oai, gem)
        # openai NOT in enabled list — should be skipped in fallback
        router = _router(reg, ["huggingface", "gemini"], "huggingface",
                         fallback_order=["huggingface", "openai", "gemini"])
        result, record = router.route(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                           routing_mode=RoutingMode.FALLBACK),
            get_api_key=_key_for("huggingface", "gemini"),
        )
        assert result.ok
        assert record.selected == "gemini"
        assert "openai" not in record.attempted

    def test_cheapest_mode_prefers_highest_cost_tier(self):
        """cost_tier 4 (free) should be tried first."""
        openrouter = _make_adapter("openrouter", cost_tier=4)  # free
        openai     = _make_adapter("openai",     cost_tier=2)  # moderate
        reg        = _registry(openai, openrouter)             # openai registered first
        router     = _router(reg, ["openai", "openrouter"], "openai")
        result, record = router.route(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                           routing_mode=RoutingMode.CHEAPEST),
            get_api_key=_key_for("openai", "openrouter"),
        )
        assert result.ok
        assert record.selected == "openrouter"   # free tier wins

    def test_best_quality_mode_prefers_lowest_quality_tier(self):
        """quality_tier 1 (premium) should be tried first."""
        hf        = _make_adapter("huggingface", quality_tier=3)
        anthropic = _make_adapter("anthropic",   quality_tier=1)  # premium
        reg       = _registry(hf, anthropic)
        router    = _router(reg, ["huggingface", "anthropic"], "huggingface")
        result, record = router.route(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                           routing_mode=RoutingMode.BEST_QUALITY),
            get_api_key=_key_for("huggingface", "anthropic"),
        )
        assert result.ok
        assert record.selected == "anthropic"

    def test_local_only_skips_remote_providers(self):
        local  = _make_adapter("local",  is_local=True, cost_tier=4)
        remote = _make_adapter("openai", is_local=False)
        reg    = _registry(local, remote)
        router = _router(reg, ["local", "openai"], "local")
        result, record = router.route(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                           routing_mode=RoutingMode.LOCAL_ONLY),
            get_api_key=lambda pid: "" if pid == "local" else "remotekey",
        )
        assert result.ok
        assert record.selected == "local"
        assert "openai" not in record.attempted

    def test_remote_only_skips_local_providers(self):
        local  = _make_adapter("local",  is_local=True, cost_tier=4)
        remote = _make_adapter("openai", is_local=False)
        reg    = _registry(local, remote)
        router = _router(reg, ["local", "openai"], "local")
        result, record = router.route(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                           routing_mode=RoutingMode.REMOTE_ONLY),
            get_api_key=_key_for("openai"),
        )
        assert result.ok
        assert record.selected == "openai"
        assert "local" not in record.attempted


# ── InferenceRouter — skip conditions ─────────────────────────────────────────


class TestRouterSkipConditions:
    def test_no_api_key_skips_remote_provider(self):
        hf = _make_adapter("huggingface")
        reg = _registry(hf)
        router = _router(reg, ["huggingface"], "huggingface")
        result, record = router.route(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                           routing_mode=RoutingMode.DEFAULT),
            get_api_key=_no_key,
        )
        assert not result.ok
        assert record.failure_reasons.get("huggingface") == "no_api_key"

    def test_local_provider_proceeds_without_key(self):
        local = _make_adapter("local", is_local=True)
        reg   = _registry(local)
        router = _router(reg, ["local"], "local")
        result, record = router.route(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                           routing_mode=RoutingMode.DEFAULT),
            get_api_key=_no_key,
        )
        assert result.ok
        assert record.selected == "local"

    def test_budget_insufficient_skips_provider(self):
        hf = _make_adapter("huggingface")
        reg = _registry(hf)
        router = _router(reg, ["huggingface"], "huggingface")
        # budget 0.0 means any positive estimated cost exceeds it
        result, record = router.route(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                           routing_mode=RoutingMode.DEFAULT,
                           budget_remaining_usd=0.0),
            get_api_key=_key_for("huggingface"),
        )
        # huggingface has a non-zero default rate; should be skipped
        assert not result.ok

    def test_fallback_on_provider_failure(self):
        failing = _make_adapter("huggingface", ok=False, error_code="timeout")
        success = _make_adapter("openai")
        reg     = _registry(failing, success)
        router  = _router(reg, ["huggingface", "openai"], "huggingface",
                          fallback_order=["huggingface", "openai"])
        result, record = router.route(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                           routing_mode=RoutingMode.FALLBACK),
            get_api_key=_key_for("huggingface", "openai"),
        )
        assert result.ok
        assert record.selected == "openai"
        assert record.failure_reasons.get("huggingface") == "timeout"
        assert result.fallback_from == "huggingface"

    def test_all_providers_failed_returns_error(self):
        hf  = _make_adapter("huggingface", ok=False, error_code="timeout")
        oai = _make_adapter("openai",      ok=False, error_code="rate_limit")
        reg = _registry(hf, oai)
        router = _router(reg, ["huggingface", "openai"], "huggingface",
                         fallback_order=["huggingface", "openai"])
        result, record = router.route(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                           routing_mode=RoutingMode.FALLBACK),
            get_api_key=_key_for("huggingface", "openai"),
        )
        assert not result.ok
        assert result.error_code == "all_providers_failed"
        assert "huggingface" in record.attempted
        assert "openai" in record.attempted

    def test_unregistered_provider_skipped_with_record(self):
        reg = ProviderRegistry()   # empty — no adapters
        router = _router(reg, ["huggingface"], "huggingface")
        result, record = router.route(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                           routing_mode=RoutingMode.DEFAULT),
            get_api_key=_key_for("huggingface"),
        )
        assert not result.ok
        assert record.failure_reasons.get("huggingface") == "not_registered"


# ── InferenceRouter — capability filters ──────────────────────────────────────


class TestRouterCapabilityFilters:
    def test_require_tool_calling_excludes_non_supporting(self):
        no_tc = _make_adapter("huggingface", supports_tool_calling=False)
        has_tc = _make_adapter("openai",     supports_tool_calling=True)
        reg    = _registry(no_tc, has_tc)
        router = _router(reg, ["huggingface", "openai"], "huggingface")
        result, record = router.route(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                           routing_mode=RoutingMode.CHEAPEST,
                           require_tool_calling=True),
            get_api_key=_key_for("huggingface", "openai"),
        )
        assert result.ok
        assert record.selected == "openai"
        assert "huggingface" not in record.attempted

    def test_require_vision_excludes_non_supporting(self):
        no_vis = _make_adapter("openrouter", supports_vision=False)
        has_vis = _make_adapter("gemini",    supports_vision=True)
        reg     = _registry(no_vis, has_vis)
        router  = _router(reg, ["openrouter", "gemini"], "openrouter")
        result, record = router.route(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                           routing_mode=RoutingMode.BEST_QUALITY,
                           require_vision=True),
            get_api_key=_key_for("openrouter", "gemini"),
        )
        assert result.ok
        assert record.selected == "gemini"


# ── InferenceRouter — RouteRecord ─────────────────────────────────────────────


class TestRouteRecord:
    def test_to_dict(self):
        hf = _make_adapter("huggingface")
        reg = _registry(hf)
        router = _router(reg, ["huggingface"], "huggingface")
        _, record = router.route(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                           routing_mode=RoutingMode.DEFAULT),
            get_api_key=_key_for("huggingface"),
        )
        d = record.to_dict()
        assert d["mode"] == "default"
        assert d["selected"] == "huggingface"
        assert isinstance(d["attempted"], list)
        assert isinstance(d["failure_reasons"], dict)

    def test_routing_metadata_on_result(self):
        hf = _make_adapter("huggingface")
        reg = _registry(hf)
        router = _router(reg, ["huggingface"], "huggingface")
        result, _ = router.route(
            RoutingRequest(messages=[{"role": "user", "content": "hi"}],
                           routing_mode=RoutingMode.DEFAULT),
            get_api_key=_key_for("huggingface"),
        )
        assert result.routed_by == "default"
        assert result.fallback_from is None  # no prior failure
