"""
EOS — Inference Router
=======================
Selects and dispatches to the appropriate provider adapter based on
routing mode, capability requirements, budget, and configured policy.

Routing modes
-------------
  explicit      — use exactly the provider/model named in the request
  default       — use the configured default_provider
  cheapest      — sort by cost_tier descending (4=free wins)
  best_quality  — sort by quality_tier ascending (1=premium wins)
  local_only    — restrict to is_local providers
  remote_only   — restrict to non-local providers
  fallback      — follow fallback_order from config, in order

Fallback behaviour
------------------
For every candidate in the ordered list, the router:
  1. Skips the provider if not registered, has no API key (unless local),
     or cannot meet budget headroom.
  2. Calls provider.complete().
  3. On success: returns immediately with routing metadata.
  4. On failure: logs the error, marks the provider as failed, tries next.
  5. If all candidates fail: returns ProviderResult(ok=False,
     error_code="all_providers_failed").

No infinite loops: each candidate is tried at most once per call.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

from runtime.providers.base import BaseProvider, ProviderResult
from runtime.providers.registry import ProviderRegistry
from runtime.providers.cost import estimate_cost, CostPolicy

logger = logging.getLogger("eos.providers.router")


# ── Routing mode ──────────────────────────────────────────────────────────────


class RoutingMode(str, Enum):
    EXPLICIT     = "explicit"       # request names specific provider/model
    DEFAULT      = "default"        # use configured default_provider
    CHEAPEST     = "cheapest"       # prefer lowest cost (highest cost_tier)
    BEST_QUALITY = "best_quality"   # prefer best quality (lowest quality_tier)
    LOCAL_ONLY   = "local_only"     # only is_local providers
    REMOTE_ONLY  = "remote_only"    # only non-local providers
    FALLBACK     = "fallback"       # follow fallback_order list in config


VALID_ROUTING_MODES = {m.value for m in RoutingMode}


# ── Routing request ───────────────────────────────────────────────────────────


@dataclass
class RoutingRequest:
    """
    Describes what the caller wants from the router.

    The router selects an ordered candidate list, then tries each provider
    until one succeeds or all fail.
    """
    messages:             list
    max_tokens:           int            = 512
    temperature:          float          = 0.7
    routing_mode:         RoutingMode    = RoutingMode.DEFAULT
    # Used only when routing_mode == EXPLICIT
    explicit_provider:    Optional[str]  = None
    explicit_model:       Optional[str]  = None
    # Capability filters — if True, only providers supporting the feature
    # are included in the candidate list
    require_tool_calling: bool           = False
    require_streaming:    bool           = False
    require_vision:       bool           = False
    # Budget headroom remaining for this request (None = no budget filter)
    budget_remaining_usd: Optional[float] = None


# ── Route record ──────────────────────────────────────────────────────────────


@dataclass
class RouteRecord:
    """
    Audit record of how a request was routed.

    Attached to the ProviderResult by the router for observability.
    """
    mode:             RoutingMode
    selected:         str                = ""
    attempted:        List[str]          = field(default_factory=list)
    failure_reasons:  Dict[str, str]     = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "mode":            self.mode.value,
            "selected":        self.selected,
            "attempted":       self.attempted,
            "failure_reasons": self.failure_reasons,
        }


# ── Inference router ──────────────────────────────────────────────────────────


class InferenceRouter:
    """
    Deterministic multi-provider router.

    Instantiate once at startup and hold in the policy engine.
    All state that affects routing comes from the constructor parameters —
    no hidden global state.

    Parameters
    ----------
    registry           — ProviderRegistry containing all registered adapters
    enabled_providers  — only providers in this set are candidates
    default_provider   — used when routing_mode == DEFAULT
    fallback_order     — ordered list used when routing_mode == FALLBACK
    cost_overrides     — optional per-provider CostPolicy overrides
    """

    def __init__(
        self,
        registry:          ProviderRegistry,
        *,
        enabled_providers: List[str],
        default_provider:  str,
        fallback_order:    List[str],
        cost_overrides:    Optional[Dict[str, CostPolicy]] = None,
    ) -> None:
        self._registry        = registry
        self._enabled         = set(enabled_providers)
        self._default         = default_provider
        self._fallback_order  = fallback_order
        self._cost_overrides  = cost_overrides or {}

    # ── Public API ────────────────────────────────────────────────────────────

    def route(
        self,
        request:     RoutingRequest,
        get_api_key: Callable[[str], Optional[str]],
    ) -> Tuple[ProviderResult, RouteRecord]:
        """
        Attempt inference via providers selected by routing mode.

        Parameters
        ----------
        request     — what to infer and how to route it
        get_api_key — callable mapping provider_id → API key (or None)

        Returns
        -------
        (ProviderResult, RouteRecord)
        ProviderResult.ok=False + error_code="all_providers_failed" if
        every candidate fails.
        """
        candidates = self._build_candidate_list(request)
        record = RouteRecord(mode=request.routing_mode)

        if not candidates:
            logger.error("[router] No viable candidates for mode=%s explicit=%s",
                         request.routing_mode.value, request.explicit_provider)
            return ProviderResult(
                ok=False,
                error=f"No providers available for routing mode '{request.routing_mode.value}'",
                error_code="no_viable_providers",
                routed_by=request.routing_mode.value,
            ), record

        prev_provider: Optional[str] = None

        for provider_id, model_id in candidates:
            provider = self._registry.get(provider_id)
            if provider is None:
                logger.debug("[router] %s not registered; skipping", provider_id)
                record.attempted.append(provider_id)
                record.failure_reasons[provider_id] = "not_registered"
                continue

            # Budget pre-check (skip if no budget constraint)
            if request.budget_remaining_usd is not None:
                est = estimate_cost(
                    provider_id=provider_id,
                    cost_overrides=self._cost_overrides,
                )
                if est > request.budget_remaining_usd:
                    logger.info(
                        "[router] Skipping %s: est $%.4f > remaining $%.4f",
                        provider_id, est, request.budget_remaining_usd,
                    )
                    record.attempted.append(provider_id)
                    record.failure_reasons[provider_id] = "budget_insufficient"
                    continue

            # Key check (local providers don't need one)
            api_key = get_api_key(provider_id) or ""
            if not api_key and not provider.capabilities.is_local:
                logger.debug("[router] No API key for %s; skipping", provider_id)
                record.attempted.append(provider_id)
                record.failure_reasons[provider_id] = "no_api_key"
                continue

            # Model override: use model_id from candidate if non-empty
            effective_provider = provider
            if model_id and hasattr(provider, "with_model"):
                effective_provider = provider.with_model(model_id)

            logger.info("[router] Attempting %s model=%s (mode=%s prev_fail=%s)",
                        provider_id,
                        model_id or provider.capabilities.default_model,
                        request.routing_mode.value,
                        prev_provider or "none")
            record.attempted.append(provider_id)

            result = effective_provider.complete(
                request.messages,
                api_key=api_key,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
            )

            if result.ok:
                result.routed_by    = request.routing_mode.value
                result.fallback_from = prev_provider
                record.selected     = provider_id
                logger.info("[router] Success: provider=%s latency=%sms",
                            provider_id, result.latency_ms)
                return result, record

            # Failure — log and try next
            err_key = result.error_code or "failed"
            logger.warning("[router] %s failed: %s (%s)",
                           provider_id, result.error, err_key)
            record.failure_reasons[provider_id] = err_key
            prev_provider = provider_id

        # All candidates exhausted
        tried = ", ".join(record.attempted) or "none"
        logger.error("[router] All providers failed. Tried: %s  Reasons: %s",
                     tried, record.failure_reasons)
        return ProviderResult(
            ok=False,
            error=f"All providers failed. Tried: {tried}",
            error_code="all_providers_failed",
            routed_by=request.routing_mode.value,
        ), record

    # ── Candidate list builder ────────────────────────────────────────────────

    def _build_candidate_list(
        self, request: RoutingRequest,
    ) -> List[Tuple[str, str]]:
        """
        Return ordered [(provider_id, model_id)] to try for this request.
        model_id="" means use the provider's configured default model.
        """
        mode = request.routing_mode

        # EXPLICIT: exactly what the caller asked for, regardless of enabled list
        if mode == RoutingMode.EXPLICIT:
            if not request.explicit_provider:
                return []
            return [(request.explicit_provider, request.explicit_model or "")]

        # DEFAULT: single provider, no fallback
        if mode == RoutingMode.DEFAULT:
            return [(self._default, "")]

        # FALLBACK: follow the ordered fallback_order list
        if mode == RoutingMode.FALLBACK:
            return [
                (pid, "")
                for pid in self._fallback_order
                if pid in self._enabled
            ]

        # All other modes: filter enabled+registered providers by capabilities
        caps_map = self._registry.capabilities_map()
        viable = [
            pid for pid in self._enabled
            if pid in caps_map
            and self._satisfies_caps(caps_map[pid], request)
        ]

        if mode == RoutingMode.LOCAL_ONLY:
            viable = [p for p in viable if caps_map[p].is_local]
        elif mode == RoutingMode.REMOTE_ONLY:
            viable = [p for p in viable if not caps_map[p].is_local]
        elif mode == RoutingMode.CHEAPEST:
            # Highest cost_tier first (4=free is most preferred)
            viable.sort(key=lambda p: caps_map[p].cost_tier, reverse=True)
        elif mode == RoutingMode.BEST_QUALITY:
            # Lowest quality_tier first (1=premium is most preferred)
            viable.sort(key=lambda p: caps_map[p].quality_tier)

        return [(pid, "") for pid in viable]

    @staticmethod
    def _satisfies_caps(caps, request: RoutingRequest) -> bool:
        """Return False if the provider cannot satisfy a hard capability requirement."""
        if request.require_tool_calling and not caps.supports_tool_calling:
            return False
        if request.require_streaming and not caps.supports_streaming:
            return False
        if request.require_vision and not caps.supports_vision:
            return False
        return True
