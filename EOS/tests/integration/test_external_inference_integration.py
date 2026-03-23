"""
Integration tests for the External Inference feature.

These tests verify end-to-end behaviour without making real HTTP calls to
HuggingFace.  The HuggingFaceProvider is mocked at the HTTP layer using
pytest-httpx or via unittest.mock.patch.

Tests cover:
- App still works fully without Hugging Face configured
- External inference is truly optional (no import errors)
- Localhost origin allowed, non-local denied
- Budget cap enforced end-to-end through call_external()
- Failed external call falls back cleanly (no hard exception)
- Ledger records are written for both allowed and denied calls
- API key never appears in status or config payloads
- Policy singleton initialises correctly from a minimal config
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# Import tier constants directly to avoid pulling in starlette middleware
TIER_LOCALHOST = "localhost"
TIER_LAN       = "lan"
TIER_EXTERNAL  = "external"

from runtime.external_inference import HFInferenceResult
from runtime.external_inference_ledger import (
    ExternalInferenceLedger,
    LedgerEntry,
    init_ledger,
    get_ledger,
)
from runtime.external_inference_policy import (
    APPROVAL_ALWAYS,
    ESCALATION_BALANCED,
    HF_SECRET_KEY,
    ExternalInferencePolicy,
    init_policy,
    get_policy,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _secrets(key: str | None = "hf_testkey"):
    sm = MagicMock()
    sm.get.side_effect = lambda k: key if k == HF_SECRET_KEY else None
    sm.set.return_value = True
    return sm


def _cfg(overrides: dict | None = None) -> dict:
    base = {
        "external_inference": {
            "enabled":              True,
            "monthly_budget_usd":  10.0,
            "approval_mode":       APPROVAL_ALWAYS,
            "escalation_mode":     ESCALATION_BALANCED,
            "huggingface": {
                "model_id":    "mistralai/Mistral-7B-Instruct-v0.2",
                "timeout_sec": 5.0,
                "max_retries": 0,
            },
        }
    }
    if overrides:
        base["external_inference"].update(overrides)
    return base


@pytest.fixture(autouse=True)
def fresh_ledger(tmp_path):
    db = tmp_path / "ei_integration.db"
    init_ledger(db)
    yield get_ledger()


@pytest.fixture()
def policy(fresh_ledger) -> ExternalInferencePolicy:
    p = ExternalInferencePolicy(_cfg(), _secrets())
    return p


# ── A. Optional / no hidden dependency ───────────────────────────────────────


class TestFeatureIsOptional:
    def test_imports_succeed_without_hf_token(self):
        """Importing the modules must never raise even if no key is set."""
        import runtime.external_inference as ei
        import runtime.external_inference_ledger as el
        import runtime.external_inference_policy as ep
        assert True  # no exception

    def test_policy_without_hf_config_disables_cleanly(self):
        sm = _secrets(key=None)
        p  = ExternalInferencePolicy({}, sm)  # empty config
        dec = p.check(origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1")
        assert not dec.allowed
        assert dec.denial_reason == "feature_disabled"

    def test_get_ledger_returns_none_when_uninitialised(self):
        import runtime.external_inference_ledger as _mod
        prev = _mod._ledger
        _mod._ledger = None
        assert get_ledger() is None
        _mod._ledger = prev  # restore

    def test_get_policy_returns_none_when_uninitialised(self):
        import runtime.external_inference_policy as _mod
        prev = _mod._policy
        _mod._policy = None
        assert get_policy() is None
        _mod._policy = prev


# ── B. Origin enforcement end-to-end ─────────────────────────────────────────


class TestOriginEnforcementE2E:
    def test_localhost_call_external_succeeds_policy_check(self, policy, fresh_ledger):
        """call_external() with localhost origin should pass the policy gate."""
        ok_result = HFInferenceResult(ok=True, content="hello", model_id="test/model",
                                      tokens_input=50, tokens_output=20, latency_ms=100)
        with patch("runtime.external_inference_policy.get_provider") as mock_prov_fn:
            mock_prov = MagicMock()
            mock_prov.complete.return_value = ok_result
            mock_prov_fn.return_value = mock_prov
            dec, result = policy.call_external(
                [{"role": "user", "content": "hello"}],
                origin_tier=TIER_LOCALHOST,
                origin_ip="127.0.0.1",
                reason="test",
            )
        assert dec.allowed
        assert result is not None
        assert result.ok
        assert result.is_external is True   # trust boundary flag

    def test_lan_call_external_denied_no_http_call(self, policy, fresh_ledger):
        """LAN origin must never trigger an HTTP call to HuggingFace."""
        with patch("runtime.external_inference_policy.get_provider") as mock_prov_fn:
            mock_prov = MagicMock()
            mock_prov_fn.return_value = mock_prov

            dec, result = policy.call_external(
                [{"role": "user", "content": "hello"}],
                origin_tier=TIER_LAN,
                origin_ip="192.168.1.5",
                reason="test",
            )

        assert not dec.allowed
        assert dec.denial_reason == "non_local_origin"
        assert result is None
        # The provider .complete() must NOT have been called
        mock_prov.complete.assert_not_called()

    def test_external_origin_denied_no_http_call(self, policy, fresh_ledger):
        with patch("runtime.external_inference_policy.get_provider") as mock_prov_fn:
            mock_prov = MagicMock()
            mock_prov_fn.return_value = mock_prov

            dec, result = policy.call_external(
                [{"role": "user", "content": "hello"}],
                origin_tier=TIER_EXTERNAL,
                origin_ip="1.2.3.4",
                reason="test",
            )

        assert not dec.allowed
        assert dec.denial_reason == "non_local_origin"
        mock_prov.complete.assert_not_called()


# ── C. Budget enforcement end-to-end ─────────────────────────────────────────


class TestBudgetE2E:
    def test_zero_budget_blocks_call(self, fresh_ledger):
        p  = ExternalInferencePolicy(_cfg({"monthly_budget_usd": 0.0}), _secrets())
        with patch("runtime.external_inference_policy.get_provider") as mock_prov_fn:
            mock_prov = MagicMock()
            mock_prov_fn.return_value = mock_prov
            dec, result = p.call_external(
                [{"role": "user", "content": "hello"}],
                origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1",
            )
        assert not dec.allowed
        assert dec.denial_reason == "zero_budget"
        mock_prov.complete.assert_not_called()

    def test_budget_not_exceeded(self, policy, fresh_ledger):
        ok_result = HFInferenceResult(ok=True, content="ok", tokens_input=10, tokens_output=10, latency_ms=50)
        with patch("runtime.external_inference_policy.get_provider") as mock_fn:
            mock_fn.return_value = MagicMock(complete=MagicMock(return_value=ok_result))
            dec, result = policy.call_external(
                [{"role": "user", "content": "hi"}],
                origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1",
            )
        assert dec.allowed
        assert result.ok


# ── D. Provider failure falls back cleanly ────────────────────────────────────


class TestProviderFailureFallback:
    def test_timeout_returns_ok_false_no_exception(self, policy, fresh_ledger):
        fail_result = HFInferenceResult(ok=False, error="timeout", error_code="timeout")
        with patch("runtime.external_inference_policy.get_provider") as mock_fn:
            mock_fn.return_value = MagicMock(complete=MagicMock(return_value=fail_result))
            dec, result = policy.call_external(
                [{"role": "user", "content": "hi"}],
                origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1",
            )
        # Policy allowed the attempt — it's the provider that failed
        assert dec.allowed
        assert result is not None
        assert not result.ok
        assert result.error_code == "timeout"
        assert result.is_external is True

    def test_invalid_key_returns_ok_false(self, policy, fresh_ledger):
        fail_result = HFInferenceResult(ok=False, error="Invalid key", error_code="invalid_key")
        with patch("runtime.external_inference_policy.get_provider") as mock_fn:
            mock_fn.return_value = MagicMock(complete=MagicMock(return_value=fail_result))
            dec, result = policy.call_external(
                [{"role": "user", "content": "hi"}],
                origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1",
            )
        assert not result.ok
        assert result.error_code == "invalid_key"

    def test_provider_uninit_returns_ok_false(self, policy, fresh_ledger):
        with patch("runtime.external_inference_policy.get_provider", return_value=None):
            dec, result = policy.call_external(
                [{"role": "user", "content": "hi"}],
                origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1",
            )
        assert dec.allowed          # policy itself passed
        assert result is not None
        assert not result.ok        # provider missing
        assert result.error_code == "provider_uninit"


# ── E. Ledger write verification ──────────────────────────────────────────────


class TestLedgerWrites:
    def test_allowed_call_writes_ledger_entry(self, policy, fresh_ledger):
        ok_result = HFInferenceResult(ok=True, content="ok", tokens_input=50, tokens_output=30, latency_ms=200)
        with patch("runtime.external_inference_policy.get_provider") as mock_fn:
            mock_fn.return_value = MagicMock(complete=MagicMock(return_value=ok_result))
            policy.call_external(
                [{"role": "user", "content": "hi"}],
                origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1",
            )
        rows = fresh_ledger.recent_history(limit=10)
        assert len(rows) == 1
        assert rows[0]["succeeded"] == 1
        assert rows[0]["denied"]    == 0

    def test_denied_call_writes_denial_record(self, policy, fresh_ledger):
        dec, result = policy.call_external(
            [{"role": "user", "content": "hi"}],
            origin_tier=TIER_LAN, origin_ip="192.168.1.99",
            reason="test denial",
        )
        rows = fresh_ledger.recent_history(limit=10)
        assert len(rows) == 1
        assert rows[0]["denied"]        == 1
        assert rows[0]["denial_reason"] == "non_local_origin"

    def test_failed_call_writes_failure_record(self, policy, fresh_ledger):
        fail = HFInferenceResult(ok=False, error="bad", error_code="http_error")
        with patch("runtime.external_inference_policy.get_provider") as mock_fn:
            mock_fn.return_value = MagicMock(complete=MagicMock(return_value=fail))
            policy.call_external(
                [{"role": "user", "content": "hi"}],
                origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1",
            )
        rows = fresh_ledger.recent_history(limit=10)
        assert len(rows) == 1
        assert rows[0]["succeeded"]  == 0
        assert rows[0]["denied"]     == 0
        assert rows[0]["error_detail"] == "bad"


# ── F. API key security ───────────────────────────────────────────────────────


class TestApiKeySecurity:
    def test_get_ei_config_safe_does_not_contain_api_key_value(self, policy):
        """The safe config dict must never expose the actual key."""
        safe = policy.get_ei_config_safe()
        # Flatten all values recursively and check no value equals the test key
        def _flatten(d):
            for v in d.values():
                if isinstance(v, dict):
                    yield from _flatten(v)
                else:
                    yield v
        all_vals = list(_flatten(safe))
        assert "hf_testkey" not in all_vals

    def test_api_key_configured_flag_is_bool(self, policy):
        safe = policy.get_ei_config_safe()
        assert isinstance(safe.get("api_key_configured"), bool)

    def test_api_key_configured_false_when_no_key(self):
        sm = _secrets(key=None)
        p  = ExternalInferencePolicy(_cfg(), sm)
        safe = p.get_ei_config_safe()
        assert safe["api_key_configured"] is False


# ── G. Trust boundary flag ────────────────────────────────────────────────────


class TestTrustBoundary:
    def test_successful_result_has_is_external_true(self, policy, fresh_ledger):
        ok = HFInferenceResult(ok=True, content="hello", tokens_input=10, tokens_output=10, latency_ms=50)
        with patch("runtime.external_inference_policy.get_provider") as mock_fn:
            mock_fn.return_value = MagicMock(complete=MagicMock(return_value=ok))
            _, result = policy.call_external(
                [{"role": "user", "content": "hi"}],
                origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1",
            )
        assert result is not None
        assert result.is_external is True
        assert result.provider == "huggingface"

    def test_failed_result_still_marked_external(self, policy, fresh_ledger):
        fail = HFInferenceResult(ok=False, error="oops", error_code="timeout")
        with patch("runtime.external_inference_policy.get_provider") as mock_fn:
            mock_fn.return_value = MagicMock(complete=MagicMock(return_value=fail))
            _, result = policy.call_external(
                [{"role": "user", "content": "hi"}],
                origin_tier=TIER_LOCALHOST, origin_ip="127.0.0.1",
            )
        assert result.is_external is True
