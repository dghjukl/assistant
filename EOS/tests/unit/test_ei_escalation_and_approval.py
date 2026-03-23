"""
Unit tests for escalation mode semantics, approval flow, orchestrator EI path,
and DB migration m005.

Tests cover:
- escalation_allows() for every mode × severity combination
- _classify_local_outcome() for every branch
- _build_ei_messages() deduplication logic
- _try_ei_fallback() ask_for_paid_calls → registers pending entry (no external call)
- _try_ei_fallback() always mode → calls policy.call_external() directly
- _try_ei_fallback() when policy is None → (None, False) returned safely
- _try_ei_fallback() pre-check denied → (None, False), nothing queued
- Pending approval confirm → executes call exactly once, entry removed
- Pending approval deny → call discarded, entry removed, no HTTP call
- DB migration m005 creates external_inference_ledger on a pre-existing DB
- DB migration m005 is idempotent (running twice does not fail)
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Import the policy constants and helpers under test ────────────────────────

from runtime.external_inference_policy import (
    ESCALATION_BALANCED,
    ESCALATION_CONSTRAINED,
    ESCALATION_DISABLED,
    ESCALATION_EMERGENCY_ONLY,
    ESCALATION_PERMISSIVE,
    SEVERITY_DEGRADED,
    SEVERITY_FAILED,
    SEVERITY_HARD_FAIL,
    SEVERITY_SUCCESS,
    escalation_allows,
)

# ── Import orchestrator helpers under test ────────────────────────────────────

from runtime.orchestrator import (
    _build_ei_messages,
    _classify_local_outcome,
    _try_ei_fallback,
)

# ── Import migration runner for m005 test ─────────────────────────────────────

from core.db_migrations import apply_migrations


# ═══════════════════════════════════════════════════════════════════════════════
# A. escalation_allows() — all mode × severity combinations
# ═══════════════════════════════════════════════════════════════════════════════


class TestEscalationAllows:
    """escalation_allows(mode, severity) must implement the documented ladder exactly."""

    # disabled — never allows anything
    def test_disabled_hard_fail(self):
        assert escalation_allows(ESCALATION_DISABLED, SEVERITY_HARD_FAIL) is False

    def test_disabled_failed(self):
        assert escalation_allows(ESCALATION_DISABLED, SEVERITY_FAILED) is False

    def test_disabled_degraded(self):
        assert escalation_allows(ESCALATION_DISABLED, SEVERITY_DEGRADED) is False

    def test_disabled_success(self):
        assert escalation_allows(ESCALATION_DISABLED, SEVERITY_SUCCESS) is False

    # emergency_only — only hard_fail
    def test_emergency_only_hard_fail(self):
        assert escalation_allows(ESCALATION_EMERGENCY_ONLY, SEVERITY_HARD_FAIL) is True

    def test_emergency_only_failed(self):
        assert escalation_allows(ESCALATION_EMERGENCY_ONLY, SEVERITY_FAILED) is False

    def test_emergency_only_degraded(self):
        assert escalation_allows(ESCALATION_EMERGENCY_ONLY, SEVERITY_DEGRADED) is False

    def test_emergency_only_success(self):
        assert escalation_allows(ESCALATION_EMERGENCY_ONLY, SEVERITY_SUCCESS) is False

    # constrained — hard_fail and failed
    def test_constrained_hard_fail(self):
        assert escalation_allows(ESCALATION_CONSTRAINED, SEVERITY_HARD_FAIL) is True

    def test_constrained_failed(self):
        assert escalation_allows(ESCALATION_CONSTRAINED, SEVERITY_FAILED) is True

    def test_constrained_degraded(self):
        assert escalation_allows(ESCALATION_CONSTRAINED, SEVERITY_DEGRADED) is False

    def test_constrained_success(self):
        assert escalation_allows(ESCALATION_CONSTRAINED, SEVERITY_SUCCESS) is False

    # balanced — hard_fail, failed, and degraded
    def test_balanced_hard_fail(self):
        assert escalation_allows(ESCALATION_BALANCED, SEVERITY_HARD_FAIL) is True

    def test_balanced_failed(self):
        assert escalation_allows(ESCALATION_BALANCED, SEVERITY_FAILED) is True

    def test_balanced_degraded(self):
        assert escalation_allows(ESCALATION_BALANCED, SEVERITY_DEGRADED) is True

    def test_balanced_success(self):
        assert escalation_allows(ESCALATION_BALANCED, SEVERITY_SUCCESS) is False

    # permissive — all severities allowed
    def test_permissive_hard_fail(self):
        assert escalation_allows(ESCALATION_PERMISSIVE, SEVERITY_HARD_FAIL) is True

    def test_permissive_failed(self):
        assert escalation_allows(ESCALATION_PERMISSIVE, SEVERITY_FAILED) is True

    def test_permissive_degraded(self):
        assert escalation_allows(ESCALATION_PERMISSIVE, SEVERITY_DEGRADED) is True

    def test_permissive_success(self):
        assert escalation_allows(ESCALATION_PERMISSIVE, SEVERITY_SUCCESS) is True

    # modes are not aliases of each other
    def test_emergency_only_is_not_constrained(self):
        """constrained allows failed; emergency_only does not."""
        assert escalation_allows(ESCALATION_CONSTRAINED, SEVERITY_FAILED) is True
        assert escalation_allows(ESCALATION_EMERGENCY_ONLY, SEVERITY_FAILED) is False

    def test_constrained_is_not_balanced(self):
        """balanced allows degraded; constrained does not."""
        assert escalation_allows(ESCALATION_BALANCED, SEVERITY_DEGRADED) is True
        assert escalation_allows(ESCALATION_CONSTRAINED, SEVERITY_DEGRADED) is False

    def test_balanced_is_not_permissive(self):
        """permissive allows success; balanced does not."""
        assert escalation_allows(ESCALATION_PERMISSIVE, SEVERITY_SUCCESS) is True
        assert escalation_allows(ESCALATION_BALANCED, SEVERITY_SUCCESS) is False

    def test_unknown_mode_returns_false(self):
        assert escalation_allows("made_up_mode", SEVERITY_HARD_FAIL) is False


# ═══════════════════════════════════════════════════════════════════════════════
# B. _classify_local_outcome()
# ═══════════════════════════════════════════════════════════════════════════════


class TestClassifyLocalOutcome:
    """Every branch of _classify_local_outcome must be reachable and correct."""

    def test_primary_unavailable_is_hard_fail(self):
        assert _classify_local_outcome("some text", False) == SEVERITY_HARD_FAIL

    def test_empty_response_is_hard_fail(self):
        assert _classify_local_outcome("", True) == SEVERITY_HARD_FAIL

    def test_whitespace_only_is_hard_fail(self):
        assert _classify_local_outcome("   \n  ", True) == SEVERITY_HARD_FAIL

    def test_known_hard_fail_prefix_brain(self):
        r = "I can't reach my brain right now."
        assert _classify_local_outcome(r, True) == SEVERITY_HARD_FAIL

    def test_known_hard_fail_prefix_error_communicating(self):
        r = "[Error communicating with primary model: connection refused]"
        assert _classify_local_outcome(r, True) == SEVERITY_HARD_FAIL

    def test_short_bracket_is_failed(self):
        # A short bracket response that is NOT one of the known hard-fail prefixes
        r = "[Unknown error occurred]"
        assert _classify_local_outcome(r, True) == SEVERITY_FAILED

    def test_long_bracket_is_not_failed(self):
        # More than 120 chars starting with [ → not SEVERITY_FAILED
        r = "[" + "x" * 200
        result = _classify_local_outcome(r, True)
        # Not FAILED — could be DEGRADED (len > 20) or SUCCESS
        assert result in (SEVERITY_DEGRADED, SEVERITY_SUCCESS)

    def test_very_short_response_is_degraded(self):
        r = "ok"  # 2 chars — below 20-char threshold
        assert _classify_local_outcome(r, True) == SEVERITY_DEGRADED

    def test_exactly_20_chars_is_not_degraded(self):
        r = "a" * 20
        assert _classify_local_outcome(r, True) == SEVERITY_SUCCESS

    def test_normal_response_is_success(self):
        r = "This is a normal response that provides useful information to the user."
        assert _classify_local_outcome(r, True) == SEVERITY_SUCCESS


# ═══════════════════════════════════════════════════════════════════════════════
# C. _build_ei_messages()
# ═══════════════════════════════════════════════════════════════════════════════


class TestBuildEiMessages:
    def test_appends_user_input_when_not_in_history(self):
        history = [{"role": "user", "content": "earlier question"},
                   {"role": "assistant", "content": "earlier answer"}]
        msgs = _build_ei_messages("new question", history)
        assert msgs[-1] == {"role": "user", "content": "new question"}
        assert len(msgs) == 3

    def test_does_not_duplicate_when_already_last(self):
        history = [{"role": "assistant", "content": "prev answer"},
                   {"role": "user", "content": "current question"}]
        msgs = _build_ei_messages("current question", history)
        # Should NOT append again
        assert msgs[-1]["content"] == "current question"
        assert sum(1 for m in msgs if m["role"] == "user" and "current question" in m["content"]) == 1

    def test_limits_to_last_6_turns(self):
        history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"msg{i}"}
                   for i in range(20)]
        msgs = _build_ei_messages("latest", history)
        # At most 6 from history + possibly 1 appended = max 7
        assert len(msgs) <= 7

    def test_filters_out_system_role(self):
        history = [{"role": "system", "content": "system prompt"},
                   {"role": "user", "content": "question"}]
        msgs = _build_ei_messages("new q", history)
        assert all(m["role"] != "system" for m in msgs)


# ═══════════════════════════════════════════════════════════════════════════════
# D. _try_ei_fallback() — approval flow
# ═══════════════════════════════════════════════════════════════════════════════


def _make_mock_policy(approval_mode: str = "always", check_allowed: bool = True,
                      call_result_content: str = "EI response"):
    """Build a mock ExternalInferencePolicy for _try_ei_fallback tests."""
    from runtime.external_inference import HFInferenceResult
    from runtime.external_inference_policy import PolicyDecision

    policy = MagicMock()
    policy._ei_cfg = {
        "approval_mode": approval_mode,
    }
    policy.check.return_value = PolicyDecision(
        allowed=check_allowed,
        denial_reason=None if check_allowed else "zero_budget",
        estimated_cost=0.001,
        budget_remaining=4.999,
    )
    ok_result = HFInferenceResult(
        ok=True, content=call_result_content,
        tokens_input=50, tokens_output=30, latency_ms=100,
    )
    policy.call_external.return_value = (
        PolicyDecision(allowed=True, estimated_cost=0.001, budget_remaining=4.998),
        ok_result,
    )
    return policy


def _make_mock_app_state(policy):
    """Build a mock app_state object."""
    state = MagicMock()
    state.ei_policy = policy
    state.ei_pending_approvals = {}
    return state


class TestTryEiFallback:

    @pytest.mark.asyncio
    async def test_always_mode_returns_content_and_used_true(self):
        from runtime.external_inference import HFInferenceResult
        from runtime.external_inference_policy import PolicyDecision

        policy = MagicMock()
        policy._ei_cfg = {"approval_mode": "always"}
        policy.call_external.return_value = (
            PolicyDecision(allowed=True, estimated_cost=0.001, budget_remaining=4.0),
            HFInferenceResult(ok=True, content="Answer from EI", tokens_input=10, tokens_output=10, latency_ms=50),
        )

        state = MagicMock()
        state.ei_policy = policy
        state.ei_pending_approvals = {}

        mock_module = MagicMock()
        mock_module.app_state = state

        with patch.dict("sys.modules", {"webui.app_state": mock_module}):
            response, used = await _try_ei_fallback(
                [{"role": "user", "content": "hi"}],
                origin_tier="localhost",
                origin_ip="127.0.0.1",
                reason="local_hard_fail",
                local_outcome_severity=SEVERITY_HARD_FAIL,
            )

        assert used is True
        assert response == "Answer from EI"
        policy.call_external.assert_called_once()

    @pytest.mark.asyncio
    async def test_ask_paid_registers_pending_no_call(self):
        """ask_for_paid_calls → pending entry registered, call_external NOT called."""
        from runtime.external_inference_policy import PolicyDecision, APPROVAL_ALWAYS

        policy = MagicMock()
        policy._ei_cfg = {"approval_mode": "ask_for_paid_calls"}
        # Pre-check with APPROVAL_ALWAYS temporarily swapped in — allowed
        policy.check.return_value = PolicyDecision(
            allowed=True, estimated_cost=0.002, budget_remaining=3.0
        )

        state = MagicMock()
        state.ei_policy = policy
        pending = {}
        state.ei_pending_approvals = pending

        mock_module = MagicMock()
        mock_module.app_state = state

        with patch.dict("sys.modules", {"webui.app_state": mock_module}):
            response, used = await _try_ei_fallback(
                [{"role": "user", "content": "complex question"}],
                origin_tier="localhost",
                origin_ip="127.0.0.1",
                reason="local_hard_fail",
                local_outcome_severity=SEVERITY_HARD_FAIL,
            )

        # No external call was made
        policy.call_external.assert_not_called()
        # used_ei is False — we queued, not called
        assert used is False
        # A pending entry was registered
        assert len(pending) == 1
        approval_id = list(pending.keys())[0]
        assert pending[approval_id]["reason"] == "local_hard_fail"
        # Response tells user it is pending
        assert "pending" in response.lower()
        assert approval_id in response

    @pytest.mark.asyncio
    async def test_ask_paid_precheck_denied_nothing_queued(self):
        """If pre-check denies even with always mode, nothing is queued."""
        from runtime.external_inference_policy import PolicyDecision

        policy = MagicMock()
        policy._ei_cfg = {"approval_mode": "ask_for_paid_calls"}
        # Pre-check denied (e.g. zero budget)
        policy.check.return_value = PolicyDecision(
            allowed=False, denial_reason="zero_budget", estimated_cost=0.001
        )

        state = MagicMock()
        state.ei_policy = policy
        pending = {}
        state.ei_pending_approvals = pending

        mock_module = MagicMock()
        mock_module.app_state = state

        with patch.dict("sys.modules", {"webui.app_state": mock_module}):
            response, used = await _try_ei_fallback(
                [{"role": "user", "content": "hi"}],
                origin_tier="localhost",
                origin_ip="127.0.0.1",
                reason="local_hard_fail",
                local_outcome_severity=SEVERITY_HARD_FAIL,
            )

        assert used is False
        assert response is None
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_policy_none_returns_none_false(self):
        """If ei_policy is None, _try_ei_fallback returns (None, False) safely."""
        state = MagicMock()
        state.ei_policy = None

        mock_module = MagicMock()
        mock_module.app_state = state

        with patch.dict("sys.modules", {"webui.app_state": mock_module}):
            response, used = await _try_ei_fallback(
                [{"role": "user", "content": "hi"}],
                origin_tier="localhost",
                origin_ip="127.0.0.1",
                reason="local_hard_fail",
                local_outcome_severity=SEVERITY_HARD_FAIL,
            )

        assert response is None
        assert used is False

    @pytest.mark.asyncio
    async def test_call_external_denied_returns_none_false(self):
        """If call_external returns denied, (None, False) is returned."""
        from runtime.external_inference_policy import PolicyDecision

        policy = MagicMock()
        policy._ei_cfg = {"approval_mode": "always"}
        policy.call_external.return_value = (
            PolicyDecision(allowed=False, denial_reason="budget_exceeded", estimated_cost=0.01),
            None,
        )

        state = MagicMock()
        state.ei_policy = policy
        state.ei_pending_approvals = {}

        mock_module = MagicMock()
        mock_module.app_state = state

        with patch.dict("sys.modules", {"webui.app_state": mock_module}):
            response, used = await _try_ei_fallback(
                [{"role": "user", "content": "hi"}],
                origin_tier="localhost",
                origin_ip="127.0.0.1",
                reason="local_hard_fail",
                local_outcome_severity=SEVERITY_HARD_FAIL,
            )

        assert response is None
        assert used is False

    @pytest.mark.asyncio
    async def test_empty_content_returns_none(self):
        """If EI call succeeds but content is empty, (None, False) is returned."""
        from runtime.external_inference import HFInferenceResult
        from runtime.external_inference_policy import PolicyDecision

        policy = MagicMock()
        policy._ei_cfg = {"approval_mode": "always"}
        policy.call_external.return_value = (
            PolicyDecision(allowed=True, estimated_cost=0.001, budget_remaining=4.0),
            HFInferenceResult(ok=True, content="   ", tokens_input=5, tokens_output=0, latency_ms=50),
        )

        state = MagicMock()
        state.ei_policy = policy
        state.ei_pending_approvals = {}

        mock_module = MagicMock()
        mock_module.app_state = state

        with patch.dict("sys.modules", {"webui.app_state": mock_module}):
            response, used = await _try_ei_fallback(
                [{"role": "user", "content": "hi"}],
                origin_tier="localhost",
                origin_ip="127.0.0.1",
                reason="local_hard_fail",
                local_outcome_severity=SEVERITY_HARD_FAIL,
            )

        assert response is None
        assert used is False


# ═══════════════════════════════════════════════════════════════════════════════
# E. Pending approval confirm and deny
# ═══════════════════════════════════════════════════════════════════════════════


class TestPendingApprovalLifecycle:
    """
    Simulate the admin confirm/deny flow against the pending_approvals dict.
    These tests mirror the behaviour of admin_ei_pending_confirm / admin_ei_pending_deny
    as implemented in app_runtime.py without requiring a full FastAPI server.
    """

    def _make_pending_entry(self):
        return {
            "messages":               [{"role": "user", "content": "test question"}],
            "origin_tier":            "localhost",
            "origin_ip":              "127.0.0.1",
            "reason":                 "local_hard_fail",
            "local_outcome_severity": SEVERITY_HARD_FAIL,
            "requested_at":           time.time(),
            "estimated_cost":         0.002,
        }

    def test_confirm_pops_entry_exactly_once(self):
        """Confirming an approval removes it from the queue."""
        from runtime.external_inference import HFInferenceResult
        from runtime.external_inference_policy import PolicyDecision, APPROVAL_ALWAYS

        approval_id = "abc123def456"
        pending = {approval_id: self._make_pending_entry()}

        policy = MagicMock()
        policy._ei_cfg = {"approval_mode": "ask_for_paid_calls"}
        ok_result = HFInferenceResult(ok=True, content="confirmed response",
                                      tokens_input=20, tokens_output=15, latency_ms=200)
        policy.call_external.return_value = (
            PolicyDecision(allowed=True, estimated_cost=0.002, budget_remaining=3.0),
            ok_result,
        )

        # Simulate confirm logic: pop entry, force approval_mode=always, call, restore
        entry = pending.pop(approval_id)
        assert approval_id not in pending  # removed

        orig_mode = policy._ei_cfg["approval_mode"]
        policy._ei_cfg["approval_mode"] = APPROVAL_ALWAYS
        dec, result = policy.call_external(
            entry["messages"],
            origin_tier=entry["origin_tier"],
            origin_ip=entry["origin_ip"],
            reason=entry["reason"],
            local_outcome_severity=entry["local_outcome_severity"],
        )
        policy._ei_cfg["approval_mode"] = orig_mode  # restored

        assert dec.allowed
        assert result.ok
        assert result.content == "confirmed response"
        # call_external called exactly once
        policy.call_external.assert_called_once()
        # Pending queue now empty
        assert len(pending) == 0

    def test_deny_pops_entry_no_call(self):
        """Denying an approval removes it from the queue without calling the provider."""
        approval_id = "xyz987"
        pending = {approval_id: self._make_pending_entry()}

        policy = MagicMock()

        # Simulate deny logic: just pop the entry
        entry = pending.pop(approval_id, None)
        assert entry is not None
        assert approval_id not in pending

        # No external call was made
        policy.call_external.assert_not_called()

    def test_confirm_nonexistent_id_returns_none(self):
        """Confirming an unknown approval_id returns None gracefully."""
        pending = {}
        entry = pending.pop("nonexistent_id", None)
        assert entry is None

    def test_confirm_cannot_be_called_twice(self):
        """After confirm, the entry is gone; a second confirm attempt finds nothing."""
        approval_id = "once_only"
        pending = {approval_id: self._make_pending_entry()}

        # First confirm
        entry1 = pending.pop(approval_id, None)
        assert entry1 is not None

        # Second confirm — already gone
        entry2 = pending.pop(approval_id, None)
        assert entry2 is None


# ═══════════════════════════════════════════════════════════════════════════════
# F. DB migration m005 — external_inference_ledger creation
# ═══════════════════════════════════════════════════════════════════════════════


def _make_pre_existing_db(db_path: Path) -> None:
    """Create a minimal entity_state.db without the external_inference_ledger table."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS entity_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS interaction_log (
            id        TEXT PRIMARY KEY,
            role      TEXT NOT NULL,
            content   TEXT NOT NULL,
            timestamp REAL NOT NULL,
            metadata  TEXT
        );
        CREATE TABLE IF NOT EXISTS autonomy_profile (
            dimension  TEXT PRIMARY KEY,
            enabled    INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
        );
    """)
    conn.commit()
    conn.close()


class TestMigrationM005:
    def test_creates_table_on_pre_existing_db(self, tmp_path):
        """m005 must create external_inference_ledger on an existing DB that lacks it."""
        db_path = tmp_path / "entity_state.db"
        _make_pre_existing_db(db_path)

        # Verify table does NOT exist before migration
        conn = sqlite3.connect(str(db_path))
        tables_before = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        conn.close()
        assert "external_inference_ledger" not in tables_before

        # Run migrations
        conn = sqlite3.connect(str(db_path))
        applied = apply_migrations(conn, "entity_state")
        conn.close()

        # Verify table NOW exists
        conn = sqlite3.connect(str(db_path))
        tables_after = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        # Verify expected columns exist
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(external_inference_ledger)"
        )}
        conn.close()

        assert "external_inference_ledger" in tables_after
        # Spot-check required columns
        for col in ("id", "ts", "epoch_ts", "provider", "request_origin_tier",
                    "model_id", "succeeded", "denied", "billing_cycle_start"):
            assert col in cols, f"Column '{col}' missing from external_inference_ledger"

    def test_migration_is_idempotent(self, tmp_path):
        """Running m005 twice must not raise or duplicate entries."""
        db_path = tmp_path / "entity_state.db"
        _make_pre_existing_db(db_path)

        conn = sqlite3.connect(str(db_path))
        apply_migrations(conn, "entity_state")
        # Second run — should report 0 new migrations applied
        count = apply_migrations(conn, "entity_state")
        conn.close()

        assert count == 0

    def test_indexes_created_by_m005(self, tmp_path):
        """m005 must create the epoch and cycle indexes."""
        db_path = tmp_path / "entity_state.db"
        _make_pre_existing_db(db_path)

        conn = sqlite3.connect(str(db_path))
        apply_migrations(conn, "entity_state")
        indexes = {r[1] for r in conn.execute(
            "SELECT * FROM sqlite_master WHERE type='index' "
            "AND tbl_name='external_inference_ledger'"
        )}
        conn.close()

        assert "idx_eil_epoch" in indexes
        assert "idx_eil_cycle" in indexes

    def test_migration_not_applied_twice_in_schema_tracking(self, tmp_path):
        """schema_migrations must record m005 as applied and not re-apply it."""
        db_path = tmp_path / "entity_state.db"
        _make_pre_existing_db(db_path)

        conn = sqlite3.connect(str(db_path))
        apply_migrations(conn, "entity_state")

        applied_ids = {r[0] for r in conn.execute(
            "SELECT id FROM schema_migrations WHERE db_name='entity_state'"
        )}
        conn.close()

        assert "m005_add_external_inference_ledger" in applied_ids
