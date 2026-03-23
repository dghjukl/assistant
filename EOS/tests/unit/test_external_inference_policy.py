"""
Unit tests for the External Inference policy engine.

Tests cover:
- Feature disabled by default
- Localhost origin allowed (when feature enabled)
- Non-local origins always denied
- Missing API key denied
- Budget-exceeded denied (zero budget, over budget)
- Per-request cap exceeded denied
- Daily cap exceeded denied
- Budget remaining calculations
- Approval mode = never always denies
- Escalation mode = disabled always denies
- clean fallback when policy not initialised
"""
from __future__ import annotations

import math
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# Import tier constants directly to avoid pulling in starlette middleware
TIER_LOCALHOST = "localhost"
TIER_LAN       = "lan"
TIER_EXTERNAL  = "external"

from runtime.external_inference_policy import (
    APPROVAL_ALWAYS,
    APPROVAL_ASK_PAID,
    APPROVAL_NEVER,
    ESCALATION_BALANCED,
    ESCALATION_DISABLED,
    ESCALATION_PERMISSIVE,
    HF_SECRET_KEY,
    ExternalInferencePolicy,
    PolicyDecision,
    init_policy,
    get_policy,
)
from runtime.external_inference_ledger import (
    ExternalInferenceLedger,
    LedgerEntry,
    init_ledger,
    get_ledger,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_secrets(api_key: Optional[str] = "hf_testkey123") -> MagicMock:
    """Return a mock SecretsManager that returns api_key for HF_SECRET_KEY."""
    sm = MagicMock()
    sm.get.side_effect = lambda k: api_key if k == HF_SECRET_KEY else None
    sm.set.return_value = True
    return sm


def _base_cfg(overrides: dict | None = None) -> dict:
    """Minimal config dict with external_inference section."""
    cfg: dict = {
        "external_inference": {
            "enabled":                     True,
            "provider":                    "huggingface",
            "localhost_only":              True,
            "monthly_budget_usd":          5.0,
            "monthly_budget_override_usd": None,
            "per_request_cap_usd":         None,
            "daily_request_cap":           None,
            "approval_mode":               APPROVAL_ALWAYS,
            "escalation_mode":             ESCALATION_BALANCED,
            "current_billing_cycle_start": None,
            "soft_warning_thresholds":     [50, 80, 95],
            "huggingface": {
                "model_id":    "mistralai/Mistral-7B-Instruct-v0.2",
                "timeout_sec": 10.0,
                "max_retries": 0,
            },
        }
    }
    if overrides:
        cfg["external_inference"].update(overrides)
    return cfg


@pytest.fixture()
def tmp_ledger(tmp_path):
    """Temporary ledger for each test — also set as the module singleton.

    Tests that write ledger entries and then check policy decisions must share
    the same ledger instance that ExternalInferencePolicy.check() consults via
    get_ledger().  Calling init_ledger() here overwrites the singleton set by
    reset_ledger_singleton so both point to the same database file.
    """
    db = tmp_path / "test.db"
    return init_ledger(db)


@pytest.fixture(autouse=True)
def reset_ledger_singleton(tmp_path):
    """Initialise the ledger singleton with a temp DB for each test."""
    db = tmp_path / "test_singleton.db"
    init_ledger(db)
    yield


# ── A. Default config ─────────────────────────────────────────────────────────


class TestDefaultDisabled:
    def test_disabled_by_default_in_empty_config(self):
        """If external_inference block is absent, feature is disabled."""
        sm     = _make_secrets()
        policy = ExternalInferencePolicy({}, sm)
        dec    = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1")
        assert not dec.allowed
        assert dec.denial_reason == "feature_disabled"

    def test_disabled_when_enabled_false(self):
        sm     = _make_secrets()
        policy = ExternalInferencePolicy(_base_cfg({"enabled": False}), sm)
        dec    = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1")
        assert not dec.allowed
        assert dec.denial_reason == "feature_disabled"


# ── B. Origin enforcement ─────────────────────────────────────────────────────


class TestOriginEnforcement:
    def test_localhost_allowed(self):
        sm     = _make_secrets()
        policy = ExternalInferencePolicy(_base_cfg(), sm)
        dec    = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1")
        assert dec.allowed, f"Expected allowed but got denial: {dec.denial_reason}"

    def test_lan_denied(self):
        sm     = _make_secrets()
        policy = ExternalInferencePolicy(_base_cfg(), sm)
        dec    = policy.check(origin_tier=TIER_LAN, origin_ip="192.168.1.10")
        assert not dec.allowed
        assert dec.denial_reason == "non_local_origin"

    def test_external_denied(self):
        sm     = _make_secrets()
        policy = ExternalInferencePolicy(_base_cfg(), sm)
        dec    = policy.check(origin_tier=TIER_EXTERNAL, origin_ip="8.8.8.8")
        assert not dec.allowed
        assert dec.denial_reason == "non_local_origin"

    def test_loopback_ipv6_allowed(self):
        """::1 should be classified as localhost by classify_origin."""
        sm     = _make_secrets()
        policy = ExternalInferencePolicy(_base_cfg(), sm)
        # The policy checks origin_tier, not the raw IP.
        # Caller is responsible for classifying; we test with TIER_LOCALHOST.
        dec    = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="::1")
        assert dec.allowed


# ── C. API key ────────────────────────────────────────────────────────────────


class TestApiKeyRequirement:
    def test_missing_key_denied(self):
        sm     = _make_secrets(api_key=None)
        policy = ExternalInferencePolicy(_base_cfg(), sm)
        dec    = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1")
        assert not dec.allowed
        assert dec.denial_reason == "provider_not_configured"

    def test_empty_string_key_denied(self):
        sm     = _make_secrets(api_key="")
        policy = ExternalInferencePolicy(_base_cfg(), sm)
        dec    = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1")
        assert not dec.allowed
        assert dec.denial_reason == "provider_not_configured"

    def test_valid_key_passes(self):
        sm     = _make_secrets(api_key="hf_valid_key_here")
        policy = ExternalInferencePolicy(_base_cfg(), sm)
        dec    = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1")
        assert dec.allowed


# ── D. Escalation & approval modes ───────────────────────────────────────────


class TestModeGating:
    def test_escalation_disabled_blocks(self):
        sm     = _make_secrets()
        policy = ExternalInferencePolicy(
            _base_cfg({"escalation_mode": ESCALATION_DISABLED}), sm
        )
        dec = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1")
        assert not dec.allowed
        assert dec.denial_reason == "escalation_mode_disabled"

    def test_approval_never_blocks(self):
        sm     = _make_secrets()
        policy = ExternalInferencePolicy(
            _base_cfg({"approval_mode": APPROVAL_NEVER}), sm
        )
        dec = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1")
        assert not dec.allowed
        assert dec.denial_reason == "approval_mode_never"

    def test_escalation_permissive_and_approval_always_allows(self):
        sm     = _make_secrets()
        cfg    = _base_cfg({"escalation_mode": ESCALATION_PERMISSIVE,
                            "approval_mode":   APPROVAL_ALWAYS})
        policy = ExternalInferencePolicy(cfg, sm)
        dec    = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1")
        assert dec.allowed


# ── E. Budget enforcement ─────────────────────────────────────────────────────


class TestBudgetEnforcement:
    def test_zero_budget_denied(self):
        sm     = _make_secrets()
        policy = ExternalInferencePolicy(
            _base_cfg({"monthly_budget_usd": 0.0}), sm
        )
        dec = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1")
        assert not dec.allowed
        assert dec.denial_reason == "zero_budget"

    def test_budget_sufficient_passes(self):
        sm     = _make_secrets()
        policy = ExternalInferencePolicy(
            _base_cfg({"monthly_budget_usd": 10.0}), sm
        )
        dec = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1",
                           tokens_input=100, tokens_output=50)
        assert dec.allowed
        assert dec.budget_remaining > 0

    def test_budget_exceeded_after_spend(self, tmp_ledger):
        """Simulate near-exhausted budget by writing a ledger row with high actual cost."""
        # Record $9.999 spent of a $10 budget, leaving only $0.001 remaining.
        # The cost estimate for 512+512 tokens is ~$0.00512, which exceeds $0.001.
        from datetime import date
        cycle = date.today().replace(day=1).isoformat()
        tmp_ledger.record_attempt(LedgerEntry(
            billing_cycle_start=cycle,
            actual_cost_usd=9.999,
            estimated_cost_usd=9.999,
            succeeded=True,
            denied=False,
        ))
        sm = _make_secrets()
        policy = ExternalInferencePolicy(
            _base_cfg({"monthly_budget_usd": 10.0}), sm
        )
        # Estimated cost for 512+512 tokens (~$0.00512) exceeds $0.001 remaining
        dec = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1",
                           tokens_input=512, tokens_output=512)
        assert not dec.allowed
        assert dec.denial_reason == "budget_exceeded"

    def test_budget_remaining_calculation(self):
        sm     = _make_secrets()
        policy = ExternalInferencePolicy(
            _base_cfg({"monthly_budget_usd": 5.0}), sm
        )
        state = policy.get_budget_state()
        assert state.monthly_budget_usd   == 5.0
        assert state.effective_budget_usd == 5.0
        assert state.remaining_usd        <= 5.0
        assert state.remaining_usd        >= 0.0

    def test_override_takes_precedence(self):
        sm     = _make_secrets()
        policy = ExternalInferencePolicy(
            _base_cfg({
                "monthly_budget_usd":          5.0,
                "monthly_budget_override_usd": 20.0,
            }),
            sm,
        )
        state = policy.get_budget_state()
        assert state.effective_budget_usd == 20.0
        assert state.monthly_budget_usd   == 5.0


# ── F. Per-request cap ────────────────────────────────────────────────────────


class TestPerRequestCap:
    def test_per_request_cap_exceeded(self):
        sm     = _make_secrets()
        # $0.001 cap — conservative estimate for 512+256 tokens is ~$0.00384
        policy = ExternalInferencePolicy(
            _base_cfg({"per_request_cap_usd": 0.001}), sm
        )
        dec = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1",
                           tokens_input=512, tokens_output=256)
        assert not dec.allowed
        assert dec.denial_reason == "per_request_cap_exceeded"

    def test_per_request_cap_passes_when_within_limit(self):
        sm     = _make_secrets()
        # $1.0 cap — any normal request passes
        policy = ExternalInferencePolicy(
            _base_cfg({"per_request_cap_usd": 1.0}), sm
        )
        dec = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1",
                           tokens_input=100, tokens_output=50)
        assert dec.allowed

    def test_no_cap_passes(self):
        sm     = _make_secrets()
        policy = ExternalInferencePolicy(
            _base_cfg({"per_request_cap_usd": None}), sm
        )
        dec = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1",
                           tokens_input=1000, tokens_output=1000)
        assert dec.allowed


# ── G. Daily request cap ──────────────────────────────────────────────────────


class TestDailyRequestCap:
    def test_daily_cap_not_exceeded(self):
        sm     = _make_secrets()
        policy = ExternalInferencePolicy(
            _base_cfg({"daily_request_cap": 10}), sm
        )
        dec = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1")
        assert dec.allowed

    def test_daily_cap_exceeded(self, tmp_ledger):
        from datetime import date
        cycle = date.today().replace(day=1).isoformat()
        today = date.today().isoformat()
        # Write 5 succeeded entries today
        for _ in range(5):
            tmp_ledger.record_attempt(LedgerEntry(
                billing_cycle_start=cycle,
                estimated_cost_usd=0.001,
                actual_cost_usd=0.001,
                succeeded=True,
                denied=False,
                ts=f"{today}T12:00:00+00:00",
            ))
        sm = _make_secrets()
        policy = ExternalInferencePolicy(
            _base_cfg({"daily_request_cap": 5}), sm
        )
        dec = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1")
        assert not dec.allowed
        assert dec.denial_reason == "daily_cap_exceeded"

    def test_no_daily_cap_always_passes(self):
        sm     = _make_secrets()
        policy = ExternalInferencePolicy(
            _base_cfg({"daily_request_cap": None}), sm
        )
        dec = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1")
        assert dec.allowed


# ── H. Config reload ──────────────────────────────────────────────────────────


class TestConfigReload:
    def test_reload_enables_feature(self):
        sm     = _make_secrets()
        policy = ExternalInferencePolicy(_base_cfg({"enabled": False}), sm)
        dec    = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1")
        assert not dec.allowed

        policy.reload_config(_base_cfg({"enabled": True}))
        dec2 = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1")
        assert dec2.allowed

    def test_update_ei_config_partial(self):
        sm     = _make_secrets()
        policy = ExternalInferencePolicy(_base_cfg(), sm)
        # Disable via update
        policy.update_ei_config({"enabled": False})
        dec = policy.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1")
        assert not dec.allowed

    def test_localhost_only_always_enforced_even_if_config_says_false(self):
        """localhost_only must always be True regardless of config value."""
        sm = _make_secrets()
        cfg = _base_cfg()
        cfg["external_inference"]["localhost_only"] = False  # attempt to disable guard
        policy = ExternalInferencePolicy(cfg, sm)
        # LAN origin must still be denied
        dec = policy.check(origin_tier=TIER_LAN, origin_ip="192.168.1.1")
        assert not dec.allowed
        assert dec.denial_reason == "non_local_origin"


# ── I. Module-level singleton ─────────────────────────────────────────────────


class TestSingleton:
    def test_init_policy_returns_instance(self):
        sm     = _make_secrets()
        policy = init_policy({}, sm)
        assert policy is get_policy()

    def test_get_policy_none_before_init(self):
        # Reset the singleton
        import runtime.external_inference_policy as _mod
        _mod._policy = None
        assert get_policy() is None
        # Reinitialise for other tests
        init_policy({}, _make_secrets())
