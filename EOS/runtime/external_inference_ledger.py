"""
EOS — External Inference Usage Ledger
======================================
Persistent, append-only SQLite ledger that records every external inference
attempt (allowed, denied, or failed).  Budget calculations are derived from
this table — the local ledger is the authoritative source of truth for spend
tracking, regardless of what the remote provider reports.

Schema fields
-------------
  id                   — auto-increment primary key
  ts                   — ISO-8601 UTC timestamp of the attempt
  epoch_ts             — Unix float for fast range queries
  provider             — always "huggingface" for now
  request_origin_tier  — "localhost" | "lan" | "external"
  request_origin_ip    — client IP string (for audit trail)
  request_reason       — human-readable why external inference was considered
  model_id             — HuggingFace model identifier used or intended
  estimated_cost_usd   — conservative pre-call estimate
  actual_cost_usd      — post-call known cost (null if not available)
  tokens_input         — prompt token count (null if unknown)
  tokens_output        — completion token count (null if unknown)
  approval_mode        — policy mode at time of attempt
  auto_approved        — 1 if allowed without user interaction, 0 otherwise
  succeeded            — 1 if the external call completed, 0 otherwise
  denied               — 1 if the request was blocked before any call, 0 otherwise
  denial_reason        — machine key if denied (e.g. "budget_exceeded")
  billing_cycle_start  — ISO-8601 date of the cycle this attempt belongs to
  response_latency_ms  — round-trip latency in milliseconds (null if denied/failed)
  error_detail         — short error string if the call failed

Usage
-----
    from runtime.external_inference_ledger import get_ledger, init_ledger

    ledger = get_ledger()          # after init_ledger(db_path) at startup
    ledger.record_attempt(...)     # write one row
    ledger.cycle_totals(...)       # read spend & count for a billing cycle
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("eos.ext_inference.ledger")

# ── Schema DDL ────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS external_inference_ledger (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                   TEXT    NOT NULL,
    epoch_ts             REAL    NOT NULL,
    provider             TEXT    NOT NULL DEFAULT 'huggingface',
    request_origin_tier  TEXT    NOT NULL DEFAULT 'localhost',
    request_origin_ip    TEXT    NOT NULL DEFAULT '',
    request_reason       TEXT    NOT NULL DEFAULT '',
    model_id             TEXT    NOT NULL DEFAULT '',
    estimated_cost_usd   REAL    NOT NULL DEFAULT 0.0,
    actual_cost_usd      REAL,
    tokens_input         INTEGER,
    tokens_output        INTEGER,
    approval_mode        TEXT    NOT NULL DEFAULT '',
    auto_approved        INTEGER NOT NULL DEFAULT 0,
    succeeded            INTEGER NOT NULL DEFAULT 0,
    denied               INTEGER NOT NULL DEFAULT 0,
    denial_reason        TEXT,
    billing_cycle_start  TEXT    NOT NULL DEFAULT '',
    response_latency_ms  INTEGER,
    error_detail         TEXT
);
CREATE INDEX IF NOT EXISTS idx_eil_epoch ON external_inference_ledger(epoch_ts);
CREATE INDEX IF NOT EXISTS idx_eil_cycle ON external_inference_ledger(billing_cycle_start);
"""

# ── Dataclass for a single ledger record ──────────────────────────────────────


@dataclass
class LedgerEntry:
    """Represents one row in external_inference_ledger."""
    provider:             str   = "huggingface"
    request_origin_tier:  str   = "localhost"
    request_origin_ip:    str   = ""
    request_reason:       str   = ""
    model_id:             str   = ""
    estimated_cost_usd:   float = 0.0
    actual_cost_usd:      Optional[float] = None
    tokens_input:         Optional[int]   = None
    tokens_output:        Optional[int]   = None
    approval_mode:        str   = ""
    auto_approved:        bool  = False
    succeeded:            bool  = False
    denied:               bool  = False
    denial_reason:        Optional[str]  = None
    billing_cycle_start:  str   = ""
    response_latency_ms:  Optional[int]  = None
    error_detail:         Optional[str]  = None
    # Filled automatically on write
    ts:       str   = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    epoch_ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "ts":                   self.ts,
            "epoch_ts":             self.epoch_ts,
            "provider":             self.provider,
            "request_origin_tier":  self.request_origin_tier,
            "request_origin_ip":    self.request_origin_ip,
            "request_reason":       self.request_reason,
            "model_id":             self.model_id,
            "estimated_cost_usd":   self.estimated_cost_usd,
            "actual_cost_usd":      self.actual_cost_usd,
            "tokens_input":         self.tokens_input,
            "tokens_output":        self.tokens_output,
            "approval_mode":        self.approval_mode,
            "auto_approved":        self.auto_approved,
            "succeeded":            self.succeeded,
            "denied":               self.denied,
            "denial_reason":        self.denial_reason,
            "billing_cycle_start":  self.billing_cycle_start,
            "response_latency_ms":  self.response_latency_ms,
            "error_detail":         self.error_detail,
        }


# ── CycleTotals dataclass ─────────────────────────────────────────────────────


@dataclass
class CycleTotals:
    """Aggregated spend and request counts for a billing cycle."""
    cycle_start:           str
    total_spent_usd:       float
    request_count:         int      # total non-denied attempts
    denied_count:          int      # blocked before any call
    succeeded_count:       int
    failed_count:          int      # attempted but external call failed
    estimated_spent_usd:   float    # sum of estimates (for denied-inclusive view)
    daily_counts:          dict     = field(default_factory=dict)  # date_str → count


# ── Ledger class ──────────────────────────────────────────────────────────────


class ExternalInferenceLedger:
    """
    Append-only ledger for external inference attempts.

    Thread-safety: SQLite connections are created per-call with check_same_thread=False.
    This is safe for concurrent reads; writes use a single short transaction.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._ensure_schema()

    # ── Public API ────────────────────────────────────────────────────────────

    def record_attempt(self, entry: LedgerEntry) -> int:
        """Write one ledger row.  Returns the new row id."""
        sql = """
            INSERT INTO external_inference_ledger (
                ts, epoch_ts, provider, request_origin_tier, request_origin_ip,
                request_reason, model_id, estimated_cost_usd, actual_cost_usd,
                tokens_input, tokens_output, approval_mode, auto_approved,
                succeeded, denied, denial_reason, billing_cycle_start,
                response_latency_ms, error_detail
            ) VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
        """
        params = (
            entry.ts, entry.epoch_ts, entry.provider, entry.request_origin_tier,
            entry.request_origin_ip, entry.request_reason, entry.model_id,
            entry.estimated_cost_usd, entry.actual_cost_usd,
            entry.tokens_input, entry.tokens_output, entry.approval_mode,
            int(entry.auto_approved), int(entry.succeeded), int(entry.denied),
            entry.denial_reason, entry.billing_cycle_start,
            entry.response_latency_ms, entry.error_detail,
        )
        with self._conn() as conn:
            cur = conn.execute(sql, params)
            row_id = cur.lastrowid or 0
        logger.debug("[ledger] Recorded entry id=%d denied=%s succeeded=%s",
                     row_id, entry.denied, entry.succeeded)
        return row_id

    def cycle_totals(self, cycle_start: str) -> CycleTotals:
        """
        Return aggregated spend and counts for the given billing cycle.

        cycle_start — ISO-8601 date string (YYYY-MM-DD) matching billing_cycle_start.
        """
        with self._conn() as conn:
            row = conn.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN denied=0 THEN COALESCE(actual_cost_usd, estimated_cost_usd) ELSE 0 END), 0.0) AS total_spent,
                    COALESCE(SUM(estimated_cost_usd), 0.0)                                                               AS est_spent,
                    COUNT(CASE WHEN denied=0 THEN 1 END)                                                                 AS req_count,
                    COUNT(CASE WHEN denied=1 THEN 1 END)                                                                 AS den_count,
                    COUNT(CASE WHEN succeeded=1 THEN 1 END)                                                              AS suc_count,
                    COUNT(CASE WHEN denied=0 AND succeeded=0 THEN 1 END)                                                 AS fail_count
                FROM external_inference_ledger
                WHERE billing_cycle_start = ?
            """, (cycle_start,)).fetchone()

            # Daily breakdown (non-denied only)
            daily_rows = conn.execute("""
                SELECT DATE(ts) as day, COUNT(*) as cnt
                FROM external_inference_ledger
                WHERE billing_cycle_start = ? AND denied = 0
                GROUP BY DATE(ts)
            """, (cycle_start,)).fetchall()

        daily: dict = {r["day"]: r["cnt"] for r in daily_rows} if daily_rows else {}
        return CycleTotals(
            cycle_start=cycle_start,
            total_spent_usd=float(row["total_spent"]),
            request_count=int(row["req_count"]),
            denied_count=int(row["den_count"]),
            succeeded_count=int(row["suc_count"]),
            failed_count=int(row["fail_count"]),
            estimated_spent_usd=float(row["est_spent"]),
            daily_counts=daily,
        )

    def daily_request_count(self, cycle_start: str, day: Optional[str] = None) -> int:
        """Count non-denied requests for *day* (YYYY-MM-DD).  Defaults to today."""
        today = day or date.today().isoformat()
        with self._conn() as conn:
            row = conn.execute("""
                SELECT COUNT(*) as cnt
                FROM external_inference_ledger
                WHERE billing_cycle_start = ? AND denied = 0 AND DATE(ts) = ?
            """, (cycle_start, today)).fetchone()
        return int(row["cnt"]) if row else 0

    def recent_history(self, limit: int = 50) -> List[dict]:
        """Return up to *limit* most-recent ledger rows as dicts."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM external_inference_ledger
                ORDER BY epoch_ts DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def total_rows(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) as cnt FROM external_inference_ledger").fetchone()
        return int(row["cnt"]) if row else 0

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_DDL)
        logger.debug("[ledger] Schema ensured at %s", self._db_path)


# ── Module-level singleton ────────────────────────────────────────────────────

_ledger: Optional[ExternalInferenceLedger] = None


def init_ledger(db_path: str | Path) -> ExternalInferenceLedger:
    """Initialise the module-level ledger singleton.  Call once at startup."""
    global _ledger
    _ledger = ExternalInferenceLedger(db_path)
    logger.info("[ledger] ExternalInferenceLedger ready at %s", db_path)
    return _ledger


def get_ledger() -> Optional[ExternalInferenceLedger]:
    """Return the active ledger, or None if not yet initialised."""
    return _ledger
