"""
EOS — External Inference Policy Engine
=======================================
Decision gate and budget enforcer for optional Hugging Face external inference.

This module is the single source of truth for whether an external inference
call is permitted.  It is called by the orchestrator BEFORE any external HTTP
request is made.  The backend enforces all rules — no UI-only logic bypasses
this layer.

Policy checks (all must pass in order)
---------------------------------------
  1. Feature enabled           — external_inference.enabled must be True
  2. Provider configured       — HF API key must exist in secrets
  3. Origin is localhost        — HARD requirement; non-local origins always denied
  4. Escalation mode allows    — mode must not be "disabled"
  5. Monthly budget sufficient — estimated cost must not exceed remaining budget
  6. Per-request cap           — estimated cost must not exceed per_request_cap_usd
  7. Daily request cap         — daily count must not exceed daily_request_cap
  8. Approval policy           — if approval_mode == "never", always deny

Denial reasons (machine keys)
------------------------------
  feature_disabled             — enabled flag is False
  provider_not_configured      — no API key in secrets
  non_local_origin             — request did not come from localhost
  escalation_mode_disabled     — escalation_mode == "disabled"
  approval_mode_never          — approval_mode == "never"
  budget_exceeded              — estimated cost would exceed remaining monthly budget
  per_request_cap_exceeded     — estimated cost exceeds per_request_cap_usd
  daily_cap_exceeded           — today's request count >= daily_request_cap
  zero_budget                  — monthly_budget_usd is exactly 0.0

Config keys (under "external_inference" in config.json)
---------------------------------------------------------
  enabled                  bool   — master gate (default False)
  provider                 str    — "huggingface" (only supported value)
  localhost_only           bool   — always True; not user-changeable
  monthly_budget_usd       float  — user-set budget (0 = no spend allowed)
  monthly_budget_override_usd  float|null — current-cycle override
  per_request_cap_usd      float|null — hard cap per single call
  daily_request_cap        int|null   — max non-denied requests per calendar day
  approval_mode            str    — "never" | "ask_for_paid_calls" | "always"
  escalation_mode          str    — "disabled" | "emergency_only" | "constrained"
                                    | "balanced" | "permissive"
  current_billing_cycle_start  str|null  — YYYY-MM-DD; auto-set if null
  huggingface.model_id     str    — HF model repo name
  huggingface.timeout_sec  float  — per-request timeout
  huggingface.max_retries  int    — retry count on transient errors

Persistent state
-----------------
  runtime/external_inference_ledger.py — all spend tracking and billing cycle sums
  core/secrets.py                      — HF API key storage
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Tier constant — matches core.access_control.TIER_LOCALHOST.
# Imported directly to avoid circular imports with starlette middleware.
TIER_LOCALHOST = "localhost"

from runtime.external_inference import (
    HFInferenceResult,
    HuggingFaceProvider,
    estimate_cost,
    get_provider,
    init_provider,
)
from runtime.external_inference_ledger import (
    CycleTotals,
    ExternalInferenceLedger,
    LedgerEntry,
    get_ledger,
)

logger = logging.getLogger("eos.ext_inference.policy")

# ── Sentinel key for the HF API key in SecretsManager ─────────────────────────
HF_SECRET_KEY = "huggingface_api_key"

# ── Approval / escalation mode enums ─────────────────────────────────────────
APPROVAL_NEVER            = "never"
APPROVAL_ASK_PAID         = "ask_for_paid_calls"
APPROVAL_ALWAYS           = "always"

ESCALATION_DISABLED       = "disabled"
ESCALATION_EMERGENCY_ONLY = "emergency_only"
ESCALATION_CONSTRAINED    = "constrained"
ESCALATION_BALANCED       = "balanced"
ESCALATION_PERMISSIVE     = "permissive"

VALID_APPROVAL_MODES    = {APPROVAL_NEVER, APPROVAL_ASK_PAID, APPROVAL_ALWAYS}
VALID_ESCALATION_MODES  = {
    ESCALATION_DISABLED, ESCALATION_EMERGENCY_ONLY,
    ESCALATION_CONSTRAINED, ESCALATION_BALANCED, ESCALATION_PERMISSIVE,
}

# ── Local inference outcome severity constants ─────────────────────────────────
# Used by the orchestrator to describe what happened with the local model call.
# These values are the canonical strings shared between orchestrator and policy.
SEVERITY_HARD_FAIL = "hard_fail"   # connection error / timeout / no primary server
SEVERITY_FAILED    = "failed"      # returned but empty, parse error, structured failure
SEVERITY_DEGRADED  = "degraded"    # returned but suspiciously short / low-quality
SEVERITY_SUCCESS   = "success"     # usable response produced


def escalation_allows(mode: str, local_outcome_severity: str) -> bool:
    """
    Return True if *mode* permits an external inference attempt given the
    observed local inference outcome severity.

    Severity ladder (ascending — each mode permits the levels below it):
      hard_fail  — connection error, timeout, no primary server reachable
      failed     — empty response, structured parse error, or hard-fail variant
      degraded   — response returned but too short / clearly insufficient
      success    — usable response produced (permissive mode still allows EI)

    Mode semantics:
      disabled        → never
      emergency_only  → only on hard_fail
      constrained     → hard_fail OR failed
      balanced        → hard_fail, failed, OR degraded
      permissive      → any outcome within policy/budget/origin restrictions
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
        return True  # any outcome — budget/origin/approval still apply
    return False

# ── Default config values ─────────────────────────────────────────────────────
_DEFAULTS: Dict[str, Any] = {
    "enabled":                      False,
    "provider":                     "huggingface",
    "localhost_only":                True,    # hard-coded; non-negotiable
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
}


# ── Policy decision result ────────────────────────────────────────────────────


@dataclass
class PolicyDecision:
    """Result of a policy gate check."""
    allowed:       bool
    denial_reason: Optional[str]   = None   # machine key if denied
    denial_msg:    str             = ""     # human-readable
    estimated_cost: float          = 0.0
    budget_remaining: float        = 0.0
    cycle_totals:  Optional[CycleTotals] = None


# ── Budget state summary ──────────────────────────────────────────────────────


@dataclass
class BudgetState:
    """Current budget snapshot for display in the admin UI."""
    cycle_start:         str
    cycle_end:           str
    monthly_budget_usd:  float
    effective_budget_usd: float    # override takes precedence if set
    spent_usd:           float
    remaining_usd:       float
    request_count:       int
    denied_count:        int
    daily_count_today:   int
    daily_cap:           Optional[int]
    per_request_cap_usd: Optional[float]
    warning_level:       Optional[int]   # 50 / 80 / 95 if threshold crossed, else None
    thresholds:          List[int]


# ── Main policy engine ────────────────────────────────────────────────────────


class ExternalInferencePolicy:
    """
    Stateless policy engine that reads live config and ledger to make
    allow/deny decisions for external inference requests.

    One instance should be created at startup and held in app_state.
    """

    def __init__(self, cfg: dict, secrets_manager: Any) -> None:
        """
        Parameters
        ----------
        cfg             — the full EOS config dict (as loaded from config.json)
        secrets_manager — core.secrets.SecretsManager instance
        """
        self._cfg            = cfg
        self._secrets        = secrets_manager
        self._ei_cfg: dict   = {}
        self._hf_cfg: dict   = {}
        self.reload_config(cfg)

    # ── Config ────────────────────────────────────────────────────────────────

    def reload_config(self, cfg: dict) -> None:
        """Re-read the external_inference block from a (possibly updated) config."""
        self._cfg   = cfg
        ei          = cfg.get("external_inference", {})
        merged: dict = dict(_DEFAULTS)
        merged.update(ei)
        # Ensure localhost_only is always True regardless of config value
        merged["localhost_only"] = True
        self._ei_cfg = merged
        self._hf_cfg = dict(_DEFAULTS["huggingface"])
        self._hf_cfg.update(ei.get("huggingface", {}))

        # Reinitialise the provider singleton if config changed
        try:
            init_provider(
                model_id    = self._hf_cfg.get("model_id",    "mistralai/Mistral-7B-Instruct-v0.2"),
                timeout_sec = float(self._hf_cfg.get("timeout_sec", 30.0)),
                max_retries = int(self._hf_cfg.get("max_retries",   1)),
            )
        except Exception as exc:
            logger.warning("[policy] Provider init failed: %s", exc)

    def update_ei_config(self, updates: dict, persist_path: Optional[Path] = None) -> None:
        """
        Apply partial updates to the external_inference config block.

        If *persist_path* points to config.json, the updated block is written
        back to disk so it survives a restart.
        """
        ei = dict(self._ei_cfg)
        hf = dict(self._hf_cfg)

        # Split top-level vs huggingface sub-keys
        hf_updates = updates.pop("huggingface", {})
        ei.update(updates)
        hf.update(hf_updates)
        ei["huggingface"] = hf
        ei["localhost_only"] = True   # hard-enforced

        self._ei_cfg = ei
        self._hf_cfg = hf

        if persist_path:
            self._persist_config(persist_path, ei)

        # Reinitialise provider in case model / timeout changed
        try:
            init_provider(
                model_id    = hf.get("model_id",    "mistralai/Mistral-7B-Instruct-v0.2"),
                timeout_sec = float(hf.get("timeout_sec", 30.0)),
                max_retries = int(hf.get("max_retries",   1)),
            )
        except Exception as exc:
            logger.warning("[policy] Provider reinit failed: %s", exc)

    def get_ei_config_safe(self) -> dict:
        """Return the external_inference config without the API key."""
        safe = dict(self._ei_cfg)
        safe.pop("huggingface_api_key", None)   # belt-and-suspenders
        safe["huggingface"] = {
            k: v for k, v in self._hf_cfg.items()
            if k != "api_key"
        }
        # Indicate whether a key is stored (without exposing it)
        safe["api_key_configured"] = bool(self._secrets.get(HF_SECRET_KEY))
        return safe

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

        Parameters
        ----------
        origin_tier   — TIER_LOCALHOST / TIER_LAN / TIER_EXTERNAL
        origin_ip     — raw client IP string (for logging only)
        reason        — human-readable reason why escalation is being considered
        tokens_input  — estimated prompt tokens (used for cost pre-check)
        tokens_output — estimated completion tokens
        """
        ledger = get_ledger()
        ei     = self._ei_cfg

        # Pre-compute estimated cost
        est_cost = estimate_cost(tokens_input=tokens_input, tokens_output=tokens_output)

        # 1. Feature enabled
        if not ei.get("enabled", False):
            return self._deny("feature_disabled", "External inference is disabled.", est_cost)

        # 2. Provider configured (API key exists)
        api_key = self._secrets.get(HF_SECRET_KEY)
        if not api_key:
            return self._deny("provider_not_configured",
                              "No Hugging Face API key is configured.", est_cost)

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
            totals = ledger.cycle_totals(cycle_start)
            spent  = totals.total_spent_usd
            remaining = max(0.0, effective_budget - spent)
        else:
            remaining = effective_budget

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
                    f"Daily request cap of {daily_cap} reached ({today_count} requests today).",
                    est_cost,
                    budget_remaining=remaining,
                    cycle_totals=totals,
                )

        # All checks passed
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
        reason:      str = "",
        max_tokens:  int = 512,
        temperature: float = 0.7,
        tokens_input:  Optional[int] = None,
        tokens_output: Optional[int] = None,
        local_outcome_severity: str = SEVERITY_HARD_FAIL,
    ) -> tuple[PolicyDecision, Optional[HFInferenceResult]]:
        """
        High-level entry point: runs policy check, writes ledger, calls provider.

        Returns (PolicyDecision, HFInferenceResult | None).
        If the policy check fails, returns (decision, None) without making any
        external call.
        If the call fails, the returned HFInferenceResult has ok=False.

        The caller must inspect decision.allowed and result.ok independently.
        """
        import time as _time

        ledger   = get_ledger()
        ei       = self._ei_cfg
        provider = get_provider()
        api_key  = self._secrets.get(HF_SECRET_KEY)
        cycle    = self._current_cycle_start()
        appr     = ei.get("approval_mode", APPROVAL_ASK_PAID)

        decision = self.check(
            origin_tier=origin_tier,
            origin_ip=origin_ip,
            reason=reason,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            local_outcome_severity=local_outcome_severity,
        )

        if not decision.allowed:
            # Write denial record
            if ledger:
                entry = LedgerEntry(
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
                )
                ledger.record_attempt(entry)
            return decision, None

        if provider is None:
            err = "Provider not initialised"
            logger.error("[policy] %s", err)
            if ledger:
                ledger.record_attempt(LedgerEntry(
                    request_origin_tier = origin_tier,
                    request_origin_ip   = origin_ip,
                    request_reason      = reason,
                    model_id            = self._hf_cfg.get("model_id", ""),
                    estimated_cost_usd  = decision.estimated_cost,
                    approval_mode       = appr,
                    auto_approved       = True,
                    succeeded           = False,
                    denied              = False,
                    billing_cycle_start = cycle,
                    error_detail        = err,
                ))
            return decision, HFInferenceResult(ok=False, error=err, error_code="provider_uninit")

        # Make the call
        t0 = _time.monotonic()
        result = provider.complete(
            messages=messages,
            api_key=api_key or "",
            max_tokens=max_tokens,
            temperature=temperature,
        )
        elapsed_ms = int((_time.monotonic() - t0) * 1000)

        # Write ledger entry with actuals
        if ledger:
            entry = LedgerEntry(
                request_origin_tier  = origin_tier,
                request_origin_ip    = origin_ip,
                request_reason       = reason,
                model_id             = self._hf_cfg.get("model_id", ""),
                estimated_cost_usd   = decision.estimated_cost,
                actual_cost_usd      = estimate_cost(
                    tokens_input  = result.tokens_input,
                    tokens_output = result.tokens_output,
                ) if result.ok else None,
                tokens_input         = result.tokens_input,
                tokens_output        = result.tokens_output,
                approval_mode        = appr,
                auto_approved        = True,
                succeeded            = result.ok,
                denied               = False,
                billing_cycle_start  = cycle,
                response_latency_ms  = result.latency_ms or elapsed_ms,
                error_detail         = result.error if not result.ok else None,
            )
            ledger.record_attempt(entry)

        if not result.ok:
            logger.warning("[policy] External call failed: %s (%s)", result.error, result.error_code)

        return decision, result

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

        ledger  = get_ledger()
        totals  = ledger.cycle_totals(cycle_start) if ledger else CycleTotals(
            cycle_start=cycle_start, total_spent_usd=0.0, request_count=0,
            denied_count=0, succeeded_count=0, failed_count=0,
            estimated_spent_usd=0.0,
        )
        daily_count = ledger.daily_request_count(cycle_start) if ledger else 0

        spent     = totals.total_spent_usd
        remaining = max(0.0, effective - spent)

        # Compute warning level
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

    def test_connection(self) -> dict:
        """Test the HF API key and model reachability. Returns a result dict."""
        api_key  = self._secrets.get(HF_SECRET_KEY)
        provider = get_provider()

        if not api_key:
            return {"ok": False, "error": "No API key configured", "error_code": "no_api_key"}
        if provider is None:
            return {"ok": False, "error": "Provider not initialised", "error_code": "provider_uninit"}

        result = provider.test_connection(api_key)
        return {
            "ok":          result.ok,
            "model_id":    result.model_id or self._hf_cfg.get("model_id", ""),
            "latency_ms":  result.latency_ms,
            "error":       result.error,
            "error_code":  result.error_code,
        }

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _effective_budget(self) -> float:
        override = self._ei_cfg.get("monthly_budget_override_usd")
        if override is not None:
            return float(override)
        return float(self._ei_cfg.get("monthly_budget_usd", 0.0))

    def _current_cycle_start(self) -> str:
        """
        Return the current billing cycle start date as YYYY-MM-DD.

        If current_billing_cycle_start is configured and still in the same month,
        use it.  Otherwise default to the 1st of the current month.
        """
        cfg_start = self._ei_cfg.get("current_billing_cycle_start")
        today     = date.today()
        if cfg_start:
            try:
                d = date.fromisoformat(str(cfg_start))
                # Use configured start if it's in the same month as today
                if d.year == today.year and d.month == today.month:
                    return d.isoformat()
            except ValueError:
                pass
        return today.replace(day=1).isoformat()

    @staticmethod
    def _current_cycle_end(cycle_start: str) -> str:
        """Return the last day of the month that cycle_start falls in."""
        import calendar
        d = date.fromisoformat(cycle_start)
        last_day = calendar.monthrange(d.year, d.month)[1]
        return d.replace(day=last_day).isoformat()

    @staticmethod
    def _deny(
        reason:          str,
        msg:             str,
        estimated_cost:  float,
        budget_remaining: float = 0.0,
        cycle_totals:    Optional[CycleTotals] = None,
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
            safe.pop("huggingface_api_key", None)   # never persist key in config
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
    logger.info("[policy] ExternalInferencePolicy ready (enabled=%s)",
                cfg.get("external_inference", {}).get("enabled", False))
    return _policy


def get_policy() -> Optional[ExternalInferencePolicy]:
    """Return the active policy engine, or None if not yet initialised."""
    return _policy
