"""
Unit tests for the ExternalInferenceLedger.

Tests cover:
- Schema initialisation (no error on first run)
- record_attempt writes and reads back
- cycle_totals aggregation: spent, counts
- daily_request_count filtering
- recent_history ordering and limit
- denied rows excluded from spend totals
- multiple cycles do not bleed into each other
"""
from __future__ import annotations

from datetime import date, timezone
from pathlib import Path

import pytest

from runtime.external_inference_ledger import (
    CycleTotals,
    ExternalInferenceLedger,
    LedgerEntry,
    init_ledger,
    get_ledger,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def ledger(tmp_path) -> ExternalInferenceLedger:
    return ExternalInferenceLedger(db_path=tmp_path / "ledger_test.db")


def _today_cycle() -> str:
    return date.today().replace(day=1).isoformat()


def _make_entry(
    *,
    cycle: str | None = None,
    denied: bool = False,
    succeeded: bool = True,
    actual_cost: float | None = None,
    estimated_cost: float = 0.005,
    day: str | None = None,
) -> LedgerEntry:
    today = day or date.today().isoformat()
    ts    = f"{today}T12:00:00+00:00"
    import time
    from datetime import datetime
    epoch = datetime.fromisoformat(ts).timestamp() if "+" in ts else time.time()
    return LedgerEntry(
        ts                  = ts,
        epoch_ts            = epoch,
        provider            = "huggingface",
        request_origin_tier = "localhost",
        request_origin_ip   = "127.0.0.1",
        request_reason      = "test",
        model_id            = "test/model",
        estimated_cost_usd  = estimated_cost,
        actual_cost_usd     = actual_cost,
        tokens_input        = 100,
        tokens_output       = 50,
        approval_mode       = "always",
        auto_approved       = True,
        succeeded           = succeeded,
        denied              = denied,
        billing_cycle_start = cycle or _today_cycle(),
        response_latency_ms = 250 if not denied else None,
    )


# ── A. Schema & basic write ───────────────────────────────────────────────────


class TestSchemaAndWrite:
    def test_init_creates_schema(self, ledger):
        assert ledger.total_rows() == 0

    def test_record_attempt_returns_id(self, ledger):
        row_id = ledger.record_attempt(_make_entry())
        assert row_id >= 1

    def test_total_rows_increments(self, ledger):
        ledger.record_attempt(_make_entry())
        ledger.record_attempt(_make_entry())
        assert ledger.total_rows() == 2

    def test_recent_history_returns_list(self, ledger):
        ledger.record_attempt(_make_entry())
        rows = ledger.recent_history(limit=10)
        assert len(rows) == 1
        assert rows[0]["provider"] == "huggingface"

    def test_recent_history_respects_limit(self, ledger):
        for _ in range(10):
            ledger.record_attempt(_make_entry())
        rows = ledger.recent_history(limit=3)
        assert len(rows) == 3

    def test_recent_history_newest_first(self, ledger):
        import time
        e1 = _make_entry()
        e1.epoch_ts = 1000.0
        e1.ts = "2024-01-01T00:00:00+00:00"
        ledger.record_attempt(e1)

        e2 = _make_entry()
        e2.epoch_ts = 2000.0
        e2.ts = "2024-01-02T00:00:00+00:00"
        ledger.record_attempt(e2)

        rows = ledger.recent_history(limit=10)
        assert rows[0]["epoch_ts"] >= rows[1]["epoch_ts"]


# ── B. Cycle totals ───────────────────────────────────────────────────────────


class TestCycleTotals:
    def test_empty_cycle_returns_zero_totals(self, ledger):
        totals = ledger.cycle_totals("2099-01-01")
        assert totals.total_spent_usd == 0.0
        assert totals.request_count   == 0
        assert totals.denied_count    == 0
        assert totals.succeeded_count == 0

    def test_spent_uses_actual_cost_when_available(self, ledger):
        cycle = "2025-01-01"
        ledger.record_attempt(_make_entry(cycle=cycle, actual_cost=0.123))
        totals = ledger.cycle_totals(cycle)
        assert abs(totals.total_spent_usd - 0.123) < 1e-6

    def test_spent_falls_back_to_estimate_when_actual_is_null(self, ledger):
        cycle = "2025-01-01"
        ledger.record_attempt(_make_entry(cycle=cycle, estimated_cost=0.007,
                                          actual_cost=None, succeeded=False))
        totals = ledger.cycle_totals(cycle)
        # succeeded=False, denied=False → falls back to estimated
        assert abs(totals.total_spent_usd - 0.007) < 1e-6

    def test_denied_rows_not_counted_in_spend(self, ledger):
        cycle = "2025-01-01"
        ledger.record_attempt(_make_entry(cycle=cycle, actual_cost=99.99,
                                          denied=True, succeeded=False))
        totals = ledger.cycle_totals(cycle)
        assert totals.total_spent_usd == 0.0
        assert totals.request_count   == 0
        assert totals.denied_count    == 1

    def test_request_count_excludes_denied(self, ledger):
        cycle = "2025-02-01"
        ledger.record_attempt(_make_entry(cycle=cycle, succeeded=True))
        ledger.record_attempt(_make_entry(cycle=cycle, denied=True))
        ledger.record_attempt(_make_entry(cycle=cycle, succeeded=True))
        totals = ledger.cycle_totals(cycle)
        assert totals.request_count == 2
        assert totals.denied_count  == 1

    def test_cycles_isolated(self, ledger):
        ledger.record_attempt(_make_entry(cycle="2025-01-01", actual_cost=5.0))
        ledger.record_attempt(_make_entry(cycle="2025-02-01", actual_cost=3.0))
        jan = ledger.cycle_totals("2025-01-01")
        feb = ledger.cycle_totals("2025-02-01")
        assert abs(jan.total_spent_usd - 5.0) < 1e-6
        assert abs(feb.total_spent_usd - 3.0) < 1e-6


# ── C. Daily request count ────────────────────────────────────────────────────


class TestDailyRequestCount:
    def test_zero_when_no_rows(self, ledger):
        cycle = _today_cycle()
        count = ledger.daily_request_count(cycle)
        assert count == 0

    def test_counts_non_denied_today(self, ledger):
        cycle = _today_cycle()
        today = date.today().isoformat()
        ledger.record_attempt(_make_entry(cycle=cycle, day=today, succeeded=True))
        ledger.record_attempt(_make_entry(cycle=cycle, day=today, succeeded=True))
        count = ledger.daily_request_count(cycle, day=today)
        assert count == 2

    def test_denied_not_counted_in_daily(self, ledger):
        cycle = _today_cycle()
        today = date.today().isoformat()
        ledger.record_attempt(_make_entry(cycle=cycle, day=today, denied=True))
        count = ledger.daily_request_count(cycle, day=today)
        assert count == 0

    def test_different_day_not_counted(self, ledger):
        cycle = "2025-03-01"
        ledger.record_attempt(_make_entry(cycle=cycle, day="2025-03-01", succeeded=True))
        count = ledger.daily_request_count(cycle, day="2025-03-02")
        assert count == 0


# ── D. Module singleton ───────────────────────────────────────────────────────


class TestLedgerSingleton:
    def test_init_ledger_sets_singleton(self, tmp_path):
        db   = tmp_path / "singleton_test.db"
        inst = init_ledger(db)
        assert inst is get_ledger()

    def test_singleton_none_before_init(self, tmp_path):
        import runtime.external_inference_ledger as _mod
        _mod._ledger = None
        assert get_ledger() is None
        # Restore the singleton to a valid ledger so subsequent fixtures and
        # tests are not broken.  Use tmp_path so pytest handles file cleanup;
        # never use tempfile.mktemp() + os.unlink() on Windows — SQLite holds
        # an OS-level file lock past the Python context manager until GC runs.
        init_ledger(tmp_path / "restore.db")
