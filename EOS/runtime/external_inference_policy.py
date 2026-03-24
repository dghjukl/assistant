"""
EOS — External Inference Policy Engine
=======================================
Decision gate and budget enforcer for optional external inference.

This module is the single source of truth for whether an external inference
call is permitted.  It is called by the orchestrator BEFORE any external HTTP
request is made.  The backend enforces all rules — no UI-only logic bypasses
this layer.

Multi-provider routing
-----------------------
The policy engine delegates actual provider selection to InferenceRouter
(runtime.providers.router).  The router selects and retries providers based
on routing_mode, capability requirements, budget, and API key availability.

Supported providers
--------------------
  huggingface  — HuggingFace Inference API (default, budget)
  openai       — OpenAI Chat Completions API (premium)
  anthropic    — Anthropic Messages API (premium)
  gemini       — Google Gemini API (cheap/premium)
  openrouter   — OpenRouter gateway for open-weight models (cheap)
  local        — local llama-server (free, no key required)

Routing modes (external_inference.routing_mode)
-------------------------------------------------
  default      — use the configured default_provider
  explicit     — use the provider/model specified per-call
  cheapest     — prefer lowest-cost viable provider
  best_quality — prefer highest-quality viable provider
  local_only   — only local backends (llama-server)
  remote_only  — only remote/cloud backends
  fallback     — follow fallback_order list in order

Policy checks (all must pass in order)
---------------------------------------
  1. Feature enabled           — external_inference.enabled must be True
  2. Provider viable           — at least one enabled provider has a key (or is local)
  3. Origin is localhost        — HARD requirement; non-local origins always denied
  4. Escalation mode allows    — mode must not be "disabled"
  5. Monthly budget sufficient — estimated cost must not exceed remaining budget
  6. Per-request cap           — estimated cost must not exceed per_request_cap_usd
  7. Daily request cap         — daily count must not exceed daily_request_cap
  8. Approval policy           — if approval_mode == "never", always deny

Denial reasons (machine keys)
------------------------------
  feature_disabled             — enabled flag is False
  provider_not_configured      — no API key in secrets for any enabled provider
  non_local_origin             — request did not come from localhost
  escalation_mode_disabled     — escalation_mode == "disabled" / mode mismatch
  approval_mode_never          — approval_mode == "never"
  budget_exceeded              — estimated cost exceeds remaining monthly budget
  per_request_cap_exceeded     — estimated cost exceeds per_request_cap_usd
  daily_cap_exceeded           — today's request count >= daily_request_cap
  zero_budget                  — monthly_budget_usd is exactly 0.0

Config keys (under "external_inference" in config.json)
---------------------------------------------------------
  enabled                      bool   — master gate (default False)
  provider                     str    — active/default provider (default "huggingface")
  routing_mode                 str    — routing mode (default "default")
  default_provider             str    — alias for provider; overrides provider if set
  fallback_order               list   — ordered provider list for fallback mode
  enabled_providers            list   — providers the router may consider
  localhost_only               bool   — always True; not user-changeable
  monthly_budget_usd           float
  monthly_budget_override_usd  float|null
  per_request_cap_usd          float|null
  daily_request_cap            int|null
  approval_mode                str    — "never" | "ask_for_paid_calls" | "always"
  escalation_mode              str    — "disabled" | ... | "permissive"
  current_billing_cycle_start  str|null
  soft_warning_thresholds      list
  huggingface.model_id         str
  huggingface.timeout_sec      float
  huggingface.max_retries      int
  openai.model_id / .timeout_sec / .max_retries
  anthropic.model_id / .timeout_sec / .max_retries
  gemini.model_id / .timeout_sec / .max_retries
  openrouter.model_id / .timeout_sec / .max_retries

Persistent state
-----------------
  runtime/external_inference_ledger.py — spend tracking and billing cycle sums
  core/secrets.py                      — API keys (one per provider)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Tier constant — matches core.access_control.TIER_LOCALHOST.
TIER_LOCALHOST = "localhost"

from runtime.external_inference import (
    HFInferenceResult,
    estimate_cost,
    # Legacy singletons kept for backward compat; policy no longer uses them
    get_provider,
    init_provider,
)
from runtime.external_inference_ledger import (
    CycleTotals,
    ExternalInferenceLedger,
    LedgerEntry,
    get_ledger,
)
from runtime.providers.router import RoutingMode, RoutingRequest, VALID_ROUTING_MODES
from runtime.providers._bootstrap import build_registry, build_router

logger = logging.getLogger("eos.ext_inference.policy")

# ── Secret key names per provider ─────────────────────────────────────────────
# Maps provider_id → SecretsManager key name.
# None means the provider needs no key (local).
PROVIDER_SECRET_KEYS: Dict[str, Optional[str]] = {
    "huggingface": "huggingface_api_key",
    "openai":      "openai_api_key",
    "anthropic":   "anthropic_api_key",
    "gemini":      "gemini_api_key",
    "openrouter":  "openrouter_api_key",
    "local":       None,
}

# Backward-compat alias — existing code imports HF_SECRET_KEY directly
HF_SECRET_KEY = "huggingface_api_key"

# ── Approval / escalation mode constants ─────────────────────────────────────
APPROVAL_NEVER            = "never"
APPROVAL_ASK_PAID         = "ask_for_paid_calls"
APPROVAL_ALWAYS           = "always"

ESCALATION_DISABLED       = "disabled"
ESCALATION_EMERGENCY_ONLY = "emergency_only"
ESCALATION_CONSTRAINED    = "constrained"
ESCALATION_BALANCED       = "balanced"
ESCALATION_PERMISSIVE     = "permissive"

VALID_APPROVAL_MODES   = {APPROVAL_NEVER, APPROVAL_ASK_PAID, APPROVAL_ALWAYS}
VALID_ESCALATION_MODES = {
    ESCALATION_DISABLED, ESCALATION_EMERGENCY_ONLY,
    ESCALATION_CONSTRAINED, ESCALATION_BALANCED, ESCALATION_PERMISSIVE,
}

# ── Local inference outcome severity constants ────────────────────────────────
SEVERITY_HARD_FAIL = "hard_fail"
SEVERITY_FAILED    = "failed"
SEVERITY_DEGRADED  = "degraded"
SEVERITY_SUCCESS   = "success"


def escalation_allows(mode: str, local_outcome_severity: str) -> bool:
    """
    Return True if *mode* permits an external inference attempt given the
    observed local inference outcome severity.
    """
    if mode == ESCALATION_DISABLED:
        return False
    if mode == ESCALATION_EMERGENCY_ONLY:
        return local_outcome_severity == SEVERITY_HARD_FAIL
    if mode == ESCALATION_CONSTRAINED:
        return local_outcome_severity in (SEVERITY_HARD_FAIL, SEVERITY_FAILED)
    if mode == ESCALATION_BALANCED:
        return local_outcome_severity in (SEVERITY_HARD_FAIL, SEVERITY_FAILED, SEVERITY_DEGRADED)
    if mode == ESCALATION_PERMISSIVE:
        return True
    return False


# ── Default config values ─────────────────────────────────────────────────────
_DEFAULTS: Dict[str, Any] = {
    "enabled":                      False,
    "provider":                     "huggingface",
    "routing_mode":                 "default",
    "default_provider":             None,       # overrides "provider" if set
    "fallback_order":               ["huggingface", "openrouter", "openai", "anthropic", "gemini"],
    "enabled_providers":            ["huggingface"],
    "localhost_only":               True,
    "monthly_budget_usd":           0.0,
    "monthly_budget_override_usd":  None,
    "per_request_cap_usd":          None,
    "daily_request_cap":            None,
    "approval_mode":                APPROVAL_ASK_PAID,
    "escalation_mode":              ESCALATION_DISABLED,
    "current_billing_cycle_start":  None,
    "soft_warning_thresholds":      [50, 80, 95],
    "huggingface": {
        "model_id":    "mistralai/Mistral-7B-Instruct-v0.2",
        "timeout_sec": 30.0,
        "max_retries": 1,
    },
    "openai": {
        "model_id":    "gpt-4o-mini",
        "timeout_sec": 30.0,
        "max_retries": 1,
    },
    "anthropic": {
        "model_id":    "claude-haiku-4-5-20251001",
        "timeout_sec": 60.0,
        "max_retries": 1,
    },
    "gemini": {
        "model_id":    "gemini-2.0-flash",
        "timeout_sec": 30.0,
        "max_retries": 1,
    },
    "openrouter": {
        "model_id":    "meta-llama/llama-3.1-8b-instruct:free",
        "timeout_sec": 30.0,
        "max_retries": 1,
    },
}


# ── Policy decision result ─────────────────────────────────────────────────────


@dataclass
class PolicyDecision:
    """Result of a policy gate check."""
    allowed:        bool
    denial_reason:  Optional[str]   = None
    denial_msg:     str             = ""
    estimated_cost: float           = 0.0
    budget_remaining: float         = 0.0
    cycle_totals:   Optional[CycleTotals] = None


# ── Budget state summary ──────────────────────────────────────────────────────


@dataclass
class BudgetState:
    """Current budget snapshot for display in the admin UI."""
    cycle_start:           str
    cycle_end:             str
    monthly_budget_usd:    float
    effective_budget_usd:  float
    spent_usd:             float
    remaining_usd:         float
    request_count:         int
    denied_count:          int
    daily_count_today:     int
    daily_cap:             Optional[int]
    per_request_cap_usd:   Optional[float]
    warning_level:         Optional[int]
    thresholds:            List[int]


# ── Main policy engine ────────────────────────────────────────────────────────


class ExternalInferencePolicy:
    """
    Stateless policy engine that reads live config and ledger to make
    allow/deny decisions for external inference requests.

    Internally delegates provider selection to InferenceRouter.
    One instance should be created at startup and held in app_state.
    """

    def __init__(self, cfg: dict, secrets_manager: Any) -> None:
        self._cfg      = cfg
        self._secrets  = secrets_manager
        self._ei_cfg: dict = {}
        self._hf_cfg:  dict = {}   # kept for backward compat
        self._registry = None
        self._router   = None
        self.reload_config(cfg)

    # ── Config ────────────────────────────────────────────────────────────────

    def reload_config(self, cfg: dict) -> None:
        """Re-read the external_inference block and rebuild the router."""
        self._cfg = cfg
        ei        = cfg.get("external_inference", {})

        merged: dict = {}
        for k, v in _DEFAULTS.items():
            if isinstance(v, dict):
                sub = dict(v)
                sub.update(ei.get(k, {}))
                merged[k] = sub
            else:
                merged[k] = ei.get(k, v)
        merged["localhost_only"] = True   # hard-enforced, non-negotiable

        self._ei_cfg = merged
        self._hf_cfg = dict(merged.get("huggingface", _DEFAULTS["huggingface"]))

        # Re-init legacy HF singleton for backward compat
        try:
            init_provider(
                model_id    = self._hf_cfg.get("model_id",    "mistralai/Mistral-7B-Instruct-v0.2"),
                timeout_sec = float(self._hf_cfg.get("timeout_sec", 30.0)),
                max_retries = int(self._hf_cfg.get("max_retries",   1)),
            )
        except Exception as exc:
            logger.warning("[policy] Legacy HF provider init failed: %s", exc)

        # Build provider registry + router
        try:
            primary_cfg = cfg.get("servers", {}).get("primary", {})
            host        = primary_cfg.get("host", "127.0.0.1")
            port        = primary_cfg.get("port", 8080)
            local_ep    = f"http://{host}:{port}"

            self._registry = build_registry(merged, primary_endpoint=local_ep)
            self._router   = build_router(merged, self._registry)
        except Exception as exc:
            logger.error("[policy] Router init failed: %s — external inference will not route", exc)
            self._registry = None
            self._router   = None

    def update_ei_config(self, updates: dict, persist_path: Optional[Path] = None) -> None:
        """
        Apply partial updates to the external_inference config block.

        Handles top-level keys and per-provider sub-dicts.
        Persists to config.json if persist_path is given.
        """
        ei = dict(self._ei_cfg)

        # Provider sub-configs are merged separately
        for pid in list(PROVIDER_SECRET_KEYS.keys()) + []:
            if pid in updates:
                sub = dict(ei.get(pid, {}))
                sub.update(updates.pop(pid))
                ei[pid] = sub

        ei.update(updates)
        ei["localhost_only"] = True

        if persist_path:
            self._persist_config(persist_path, ei)

        # Rebuild from merged config
        full_cfg = dict(self._cfg)
        full_cfg["external_inference"] = ei
        self.reload_config(full_cfg)

    def get_ei_config_safe(self) -> dict:
        """Return the external_inference config without any API keys."""
        safe = dict(self._ei_cfg)
        # Remove any stray key material
        for bad in ("huggingface_api_key", "api_key"):
            safe.pop(bad, None)

        # Scrub per-provider sub-dicts of keys
        for pid in PROVIDER_SECRET_KEYS:
            if pid in safe and isinstance(safe[pid], dict):
                safe[pid] = {k: v for k, v in safe[pid].items() if "key" not in k.lower()}

        # Indicate which providers have keys configured
        key_status: dict = {}
        for pid, key_name in PROVIDER_SECRET_KEYS.items():
            if key_name is None:
                key_status[pid] = True   # local never needs a key
            else:
                key_status[pid] = bool(self._secrets.get(key_name))
        safe["provider_key_status"] = key_status

        # Legacy field for existing code that checks api_key_configured
        safe["api_key_configured"] = key_status.get("huggingface", False)

        return safe

    def get_providers_status(self) -> list:
        """Return per-provider status for the admin UI."""
        if self._registry is None:
            return []
        result = self._registry.summary()
        for item in result:
            pid      = item["provider_id"]
            key_name = PROVIDER_SECRET_KEYS.get(pid)
            item["key_configured"] = (
                True if key_name is None
                else bool(self._secrets.get(key_name))
            )
            item["enabled"] = pid in self._ei_cfg.get("enabled_providers", ["huggingface"])
        return result

    # ── Policy gate ───────────────────────────────────────────────────────────

    def check(
        self,
        *,
        origin_tier: str,
        origin_ip:   str,
        reason:      str    = "",
        tokens_input:  Optional[int] = None,
        tokens_output: Optional[int] = None,
        local_outcome_severity: str = SEVERITY_HARD_FAIL,
    ) -> PolicyDecision:
        """
        Run all policy checks.  Returns PolicyDecision(allowed=False) with a
        specific denial_reason if any check fails.
        """
        ledger = get_ledger()
        ei     = self._ei_cfg

        # Pre-compute estimated cost (conservative flat rate)
        est_cost = estimate_cost(tokens_input=tokens_input, tokens_output=tokens_output)

        # 1. Feature enabled
        if not ei.get("enabled", False):
            return self._deny("feature_disabled", "External inference is disabled.", est_cost)

        # 2. At least one viable provider configured
        if not self._has_any_viable_provider():
            active = self._active_provider_id()
            return self._deny(
                "provider_not_configured",
                f"No API key configured for '{active}'. "
                "Set an API key via Admin → Ext. Inference to enable external inference.",
                est_cost,
            )

        # 3. Origin MUST be localhost — hard requirement, non-negotiable
        if origin_tier != TIER_LOCALHOST:
            logger.warning(
                "[policy] External inference denied: non-local origin %r (ip=%s)",
                origin_tier, origin_ip,
            )
            return self._deny(
                "non_local_origin",
                f"External inference is only available to localhost requests. "
                f"Denied for origin tier '{origin_tier}'.",
                est_cost,
            )

        # 4. Escalation mode — check against observed local outcome severity
        esc_mode = ei.get("escalation_mode", ESCALATION_DISABLED)
        if not escalation_allows(esc_mode, local_outcome_severity):
            return self._deny(
                "escalation_mode_disabled",
                f"Escalation mode '{esc_mode}' does not permit external inference "
                f"for local outcome '{local_outcome_severity}'.",
                est_cost,
            )

        # 5. Approval mode — "never" always blocks
        appr_mode = ei.get("approval_mode", APPROVAL_NEVER)
        if appr_mode == APPROVAL_NEVER:
            return self._deny("approval_mode_never",
                              "Approval mode is set to 'never'.", est_cost)

        # 6. Budget check
        effective_budget = self._effective_budget()
        if effective_budget == 0.0:
            return self._deny("zero_budget",
                              "Monthly budget is $0.00 — no external spend is allowed.", est_cost)

        cycle_start = self._current_cycle_start()
        totals: Optional[CycleTotals] = None
        if ledger:
            totals     = ledger.cycle_totals(cycle_start)
            spent      = totals.total_spent_usd
            remaining  = max(0.0, effective_budget - spent)
        else:
            remaining  = effective_budget

        if est_cost > remaining:
            return self._deny(
                "budget_exceeded",
                f"Estimated cost ${est_cost:.4f} exceeds remaining budget ${remaining:.4f}.",
                est_cost,
                budget_remaining=remaining,
                cycle_totals=totals,
            )

        # 7. Per-request cap
        per_req_cap = ei.get("per_request_cap_usd")
        if per_req_cap is not None and est_cost > float(per_req_cap):
            return self._deny(
                "per_request_cap_exceeded",
                f"Estimated cost ${est_cost:.4f} exceeds per-request cap ${per_req_cap:.4f}.",
                est_cost,
                budget_remaining=remaining,
                cycle_totals=totals,
            )

        # 8. Daily request cap
        daily_cap = ei.get("daily_request_cap")
        if daily_cap is not None and ledger:
            today_count = ledger.daily_request_count(cycle_start)
            if today_count >= int(daily_cap):
                return self._deny(
                    "daily_cap_exceeded",
                    f"Daily request cap of {daily_cap} reached ({today_count} today).",
                    est_cost,
                    budget_remaining=remaining,
                    cycle_totals=totals,
                )

        return PolicyDecision(
            allowed=True,
            estimated_cost=est_cost,
            budget_remaining=remaining,
            cycle_totals=totals,
        )

    # ── Inference entry point ─────────────────────────────────────────────────

    def call_external(
        self,
        messages:    "list[dict]",
        *,
        origin_tier: str,
        origin_ip:   str,
        reason:      str   = "",
        max_tokens:  int   = 512,
        temperature: float = 0.7,
        tokens_input:  Optional[int] = None,
        tokens_output: Optional[int] = None,
        local_outcome_severity: str  = SEVERITY_HARD_FAIL,
        routing_mode: Optional[str]  = None,
    ) -> tuple:
        """
        High-level entry point: runs policy check, routes via InferenceRouter,
        writes ledger, returns result.

        Returns (PolicyDecision, HFInferenceResult | None).
        - If the policy check fails: returns (decision, None).
        - If the call fails: returns (decision, HFInferenceResult(ok=False)).

        The caller must inspect decision.allowed and result.ok independently.

        Parameters
        ----------
        routing_mode — override the configured routing mode for this call.
                       If None, uses the configured routing_mode.
        """
        ledger = get_ledger()
        ei     = self._ei_cfg
        cycle  = self._current_cycle_start()
        appr   = ei.get("approval_mode", APPROVAL_ASK_PAID)

        decision = self.check(
            origin_tier=origin_tier,
            origin_ip=origin_ip,
            reason=reason,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            local_outcome_severity=local_outcome_severity,
        )

        if not decision.allowed:
            if ledger:
                ledger.record_attempt(LedgerEntry(
                    provider             = self._active_provider_id(),
                    request_origin_tier  = origin_tier,
                    request_origin_ip    = origin_ip,
                    request_reason       = reason,
                    model_id             = self._hf_cfg.get("model_id", ""),
                    estimated_cost_usd   = decision.estimated_cost,
                    approval_mode        = appr,
                    auto_approved        = False,
                    succeeded            = False,
                    denied               = True,
                    denial_reason        = decision.denial_reason,
                    billing_cycle_start  = cycle,
                ))
            return decision, None

        # Router required
        if self._router is None:
            err = "Inference router not initialised"
            logger.error("[policy] %s", err)
            if ledger:
                ledger.record_attempt(LedgerEntry(
                    provider             = self._active_provider_id(),
                    request_origin_tier  = origin_tier,
                    request_origin_ip    = origin_ip,
                    request_reason       = reason,
                    model_id             = self._hf_cfg.get("model_id", ""),
                    estimated_cost_usd   = decision.estimated_cost,
                    approval_mode        = appr,
                    auto_approved        = True,
                    succeeded            = False,
                    denied               = False,
                    billing_cycle_start  = cycle,
                    error_detail         = err,
                ))
            return decision, HFInferenceResult(ok=False, error=err, error_code="router_uninit")

        # Determine routing mode
        raw_mode = routing_mode or ei.get("routing_mode", "default")
        if raw_mode not in VALID_ROUTING_MODES:
            raw_mode = "default"
        rmode = RoutingMode(raw_mode)

        request = RoutingRequest(
            messages             = messages,
            max_tokens           = max_tokens,
            temperature          = temperature,
            routing_mode         = rmode,
            budget_remaining_usd = decision.budget_remaining if decision.budget_remaining > 0 else None,
        )

        prov_result, route_record = self._router.route(
            request,
            get_api_key=self._get_api_key,
        )

        # Compute actual cost from token counts if available
        actual_cost: Optional[float] = None
        if prov_result.ok and (prov_result.tokens_input or prov_result.tokens_output):
            actual_cost = estimate_cost(
                tokens_input  = prov_result.tokens_input,
                tokens_output = prov_result.tokens_output,
            )

        # Write ledger entry
        if ledger:
            ledger.record_attempt(LedgerEntry(
                provider             = prov_result.provider or self._active_provider_id(),
                request_origin_tier  = origin_tier,
                request_origin_ip    = origin_ip,
                request_reason       = reason,
                model_id             = prov_result.model_id or self._hf_cfg.get("model_id", ""),
                estimated_cost_usd   = decision.estimated_cost,
                actual_cost_usd      = actual_cost,
                tokens_input         = prov_result.tokens_input,
                tokens_output        = prov_result.tokens_output,
                approval_mode        = appr,
                auto_approved        = True,
                succeeded            = prov_result.ok,
                denied               = False,
                billing_cycle_start  = cycle,
                response_latency_ms  = prov_result.latency_ms,
                error_detail         = prov_result.error if not prov_result.ok else None,
            ))

        if not prov_result.ok:
            logger.warning("[policy] External call failed: %s (%s) — tried: %s",
                           prov_result.error, prov_result.error_code,
                           route_record.attempted)

        # Convert ProviderResult → HFInferenceResult for backward compat
        hf_result = HFInferenceResult(
            ok            = prov_result.ok,
            content       = prov_result.content,
            model_id      = prov_result.model_id,
            provider      = prov_result.provider or "huggingface",
            tokens_input  = prov_result.tokens_input,
            tokens_output = prov_result.tokens_output,
            latency_ms    = prov_result.latency_ms,
            error         = prov_result.error,
            error_code    = prov_result.error_code,
            raw_response  = prov_result.raw_response,
            is_external   = prov_result.is_external,
        )
        return decision, hf_result

    # ── Connection test ───────────────────────────────────────────────────────

    def test_connection(self) -> dict:
        """
        Test the active/default provider.  Returns a result dict.

        Uses the provider configured as the active default.
        """
        active_pid = self._active_provider_id()
        return self.test_connection_provider(active_pid)

    def test_connection_provider(self, provider_id: str) -> dict:
        """
        Test a specific provider by ID.  Returns a result dict.

        Safe to call for any registered provider_id.
        """
        if self._registry is None:
            return {"ok": False, "error": "Registry not initialised",
                    "error_code": "registry_uninit", "provider_id": provider_id}

        provider = self._registry.get(provider_id)
        if provider is None:
            return {"ok": False, "error": f"Provider '{provider_id}' not registered",
                    "error_code": "not_registered", "provider_id": provider_id}

        key_name = PROVIDER_SECRET_KEYS.get(provider_id)
        api_key  = self._secrets.get(key_name) if key_name else ""

        if not api_key and not provider.capabilities.is_local:
            return {"ok": False, "error": f"No API key configured for '{provider_id}'",
                    "error_code": "no_api_key", "provider_id": provider_id}

        result = provider.test_connection(api_key or "")
        return {
            "ok":          result.ok,
            "provider_id": provider_id,
            "model_id":    result.model_id or provider.capabilities.default_model,
            "latency_ms":  result.latency_ms,
            "error":       result.error,
            "error_code":  result.error_code,
        }

    # ── Budget & cycle helpers ─────────────────────────────────────────────────

    def get_budget_state(self) -> BudgetState:
        """Return a complete budget snapshot for the current billing cycle."""
        ei          = self._ei_cfg
        budget      = float(ei.get("monthly_budget_usd", 0.0))
        override    = ei.get("monthly_budget_override_usd")
        effective   = float(override) if override is not None else budget
        thresholds  = ei.get("soft_warning_thresholds", [50, 80, 95])
        daily_cap   = ei.get("daily_request_cap")
        per_req_cap = ei.get("per_request_cap_usd")

        cycle_start = self._current_cycle_start()
        cycle_end   = self._current_cycle_end(cycle_start)

        ledger      = get_ledger()
        totals      = ledger.cycle_totals(cycle_start) if ledger else CycleTotals(
            cycle_start=cycle_start, total_spent_usd=0.0, request_count=0,
            denied_count=0, succeeded_count=0, failed_count=0, estimated_spent_usd=0.0,
        )
        daily_count = ledger.daily_request_count(cycle_start) if ledger else 0

        spent     = totals.total_spent_usd
        remaining = max(0.0, effective - spent)

        warning_level: Optional[int] = None
        if effective > 0:
            pct = (spent / effective) * 100.0
            for t in sorted(thresholds, reverse=True):
                if pct >= t:
                    warning_level = t
                    break

        return BudgetState(
            cycle_start          = cycle_start,
            cycle_end            = cycle_end,
            monthly_budget_usd   = budget,
            effective_budget_usd = effective,
            spent_usd            = spent,
            remaining_usd        = remaining,
            request_count        = totals.request_count,
            denied_count         = totals.denied_count,
            daily_count_today    = daily_count,
            daily_cap            = int(daily_cap) if daily_cap is not None else None,
            per_request_cap_usd  = float(per_req_cap) if per_req_cap is not None else None,
            warning_level        = warning_level,
            thresholds           = thresholds,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _active_provider_id(self) -> str:
        """Return the configured default/active provider ID."""
        ei = self._ei_cfg
        return ei.get("default_provider") or ei.get("provider", "huggingface")

    def _get_api_key(self, provider_id: str) -> Optional[str]:
        """Retrieve the API key for *provider_id* from the secrets manager."""
        key_name = PROVIDER_SECRET_KEYS.get(provider_id)
        if key_name is None:
            return ""   # local: no key needed
        return self._secrets.get(key_name) or None

    def _has_any_viable_provider(self) -> bool:
        """
        Return True if at least one enabled provider has an API key
        configured or is a local provider (no key required).
        """
        ei               = self._ei_cfg
        enabled_providers = ei.get("enabled_providers", [self._active_provider_id()])
        fallback_order    = ei.get("fallback_order", [])
        candidates        = list(dict.fromkeys(
            [self._active_provider_id()] + enabled_providers + fallback_order
        ))
        for pid in candidates:
            key_name = PROVIDER_SECRET_KEYS.get(pid)
            if key_name is None:
                return True   # local needs no key
            if self._secrets.get(key_name):
                return True
        return False

    def _effective_budget(self) -> float:
        override = self._ei_cfg.get("monthly_budget_override_usd")
        if override is not None:
            return float(override)
        return float(self._ei_cfg.get("monthly_budget_usd", 0.0))

    def _current_cycle_start(self) -> str:
        cfg_start = self._ei_cfg.get("current_billing_cycle_start")
        today     = date.today()
        if cfg_start:
            try:
                d = date.fromisoformat(str(cfg_start))
                if d.year == today.year and d.month == today.month:
                    return d.isoformat()
            except ValueError:
                pass
        return today.replace(day=1).isoformat()

    @staticmethod
    def _current_cycle_end(cycle_start: str) -> str:
        import calendar
        d        = date.fromisoformat(cycle_start)
        last_day = calendar.monthrange(d.year, d.month)[1]
        return d.replace(day=last_day).isoformat()

    @staticmethod
    def _deny(
        reason:           str,
        msg:              str,
        estimated_cost:   float,
        budget_remaining: float            = 0.0,
        cycle_totals:     Optional[CycleTotals] = None,
    ) -> PolicyDecision:
        logger.info("[policy] Denied: %s — %s", reason, msg)
        return PolicyDecision(
            allowed=False,
            denial_reason=reason,
            denial_msg=msg,
            estimated_cost=estimated_cost,
            budget_remaining=budget_remaining,
            cycle_totals=cycle_totals,
        )

    def _persist_config(self, config_path: Path, ei_cfg: dict) -> None:
        """Write the external_inference block back to config.json on disk."""
        try:
            raw  = json.loads(config_path.read_text(encoding="utf-8"))
            safe = dict(ei_cfg)
            # Never persist any key material
            for bad in ("huggingface_api_key", "api_key"):
                safe.pop(bad, None)
            raw["external_inference"] = safe
            config_path.write_text(
                json.dumps(raw, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info("[policy] Config persisted to %s", config_path)
        except Exception as exc:
            logger.error("[policy] Failed to persist config: %s", exc)


# ── Module-level singleton ────────────────────────────────────────────────────

_policy: Optional[ExternalInferencePolicy] = None


def init_policy(cfg: dict, secrets_manager: Any) -> ExternalInferencePolicy:
    """Initialise the module-level policy singleton.  Call once at startup."""
    global _policy
    _policy = ExternalInferencePolicy(cfg, secrets_manager)
    logger.info("[policy] ExternalInferencePolicy ready (enabled=%s provider=%s routing=%s)",
                cfg.get("external_inference", {}).get("enabled", False),
                cfg.get("external_inference", {}).get("provider", "huggingface"),
                cfg.get("external_inference", {}).get("routing_mode", "default"))
    return _policy


def get_policy() -> Optional[ExternalInferencePolicy]:
    """Return the active policy engine, or None if not yet initialised."""
    return _policy
