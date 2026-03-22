"""
EOS — Identity Continuity Monitor
Cross-session identity drift tracking, stability scoring, and revision audit.

Why this exists
---------------
The current identity evaluation loop (core/identity.py) measures drift
*within* a single evaluation cycle — it compares the new answer against
the previous one.  But it has no memory across sessions, no rolling stability
score, and no way to detect slow cumulative drift over weeks.

More importantly: the entity's name should be allowed to change as it
changes.  If the entity has grown significantly across all 6 domains, it
may want to reconsider who it is — including its name.  That requires:
  1. A rolling snapshot history so we can compare today's self to three
     months ago's self, not just last cycle's.
  2. A stability score that reflects whether identity is currently in a
     settled period or a period of active growth/change.
  3. A revision audit trail so the entity's history of self-understanding
     is never erased — growth is additive, not destructive.

Architecture
------------
  IdentityContinuityMonitor
    - Snapshot every evaluation cycle → identity_snapshots table
    - Compute cumulative drift against rolling baseline (last N snapshots)
    - EMA stability score persisted across restarts
    - Revision log when a domain answer changes significantly
    - Significant-shift detection: when cumulative cross-domain drift
      exceeds a threshold, flags that name re-evaluation is warranted

  This module writes only to the entity's existing SQLite DB (db_path from
  config).  No new file is needed.

Usage
-----
    from runtime.identity_continuity import IdentityContinuityMonitor

    monitor = IdentityContinuityMonitor(db_path=cfg["db_path"])

    # Call after each run_evaluation_cycle()
    report = monitor.record_cycle(cycle_results, current_identity_state)

    if report["name_review_warranted"]:
        # The entity has changed enough that re-naming should be considered
        ...

    score = monitor.stability_score()     # float 0.0–1.0
    history = monitor.revision_history(limit=20)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger("eos.identity_continuity")

UTC = timezone.utc

# ── Configuration constants ───────────────────────────────────────────────────

# How many recent snapshots to use for rolling baseline comparison
_ROLLING_WINDOW: int = 5

# EMA weight for stability score (higher = more reactive to recent cycles)
_EMA_ALPHA: float = 0.25

# Drift penalty per detected domain shift (scales with number of domains shifting)
_DRIFT_PENALTY_PER_DOMAIN: float = 0.10

# Word-overlap drift threshold that counts as a "significant" domain shift
_DOMAIN_SHIFT_THRESHOLD: float = 0.35

# Number of domains with significant shift that triggers name review
_NAME_REVIEW_DOMAIN_COUNT: int = 4

# Minimum cycles between name review suggestions (prevents spamming)
_NAME_REVIEW_MIN_CYCLES: int = 10


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _word_overlap_drift(a: str, b: str) -> float:
    """0.0 = identical, 1.0 = completely different words."""
    if not a or not b:
        return 0.0
    s1 = set(a.lower().split())
    s2 = set(b.lower().split())
    if not s1 and not s2:
        return 0.0
    union = len(s1 | s2)
    return 1.0 - (len(s1 & s2) / union) if union > 0 else 0.0


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class DomainRevision:
    """A recorded change to one identity domain."""
    revision_id: str
    cycle: int
    domain: str
    old_answer: str
    new_answer: str
    confidence: float
    drift: float
    recorded_at: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ContinuityReport:
    """Result of a monitor.record_cycle() call."""
    cycle: int
    stability_score: float
    stability_label: str      # "stable" | "evolving" | "in_flux"
    domains_shifted: list[str]
    cumulative_drift: float
    name_review_warranted: bool
    revisions_recorded: int
    summary: str

    def to_dict(self) -> dict:
        return asdict(self)


# ── Monitor ───────────────────────────────────────────────────────────────────

class IdentityContinuityMonitor:
    """
    Tracks identity drift across evaluation cycles and sessions.

    Parameters
    ----------
    db_path : str | Path
        Path to the entity's SQLite database.  Tables are created on first use.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db = Path(db_path)
        self._lock = threading.Lock()
        self._stability: float = 1.0
        self._last_name_review_cycle: int = -_NAME_REVIEW_MIN_CYCLES
        self._init_schema()
        self._stability = self._load_stability()

    # ── Public API ────────────────────────────────────────────────────────────

    def record_cycle(
        self,
        cycle: int,
        cycle_results: dict[str, Any],
        current_state: dict[str, Any],
    ) -> ContinuityReport:
        """
        Record the results of one identity evaluation cycle.

        Parameters
        ----------
        cycle : int
            The cycle number from increment_reflection_cycle().
        cycle_results : dict
            The ``domains`` sub-dict from run_evaluation_cycle().
            Each key is a domain name; each value has ``answer``, ``confidence``,
            and ``drift`` (or ``error``).
        current_state : dict
            The full identity_state dict (domain → {answer, confidence, ...}).

        Returns
        -------
        ContinuityReport
        """
        with self._lock:
            # Load rolling baseline for gradual-drift comparison
            snapshots = self._load_snapshots(_ROLLING_WINDOW)

            domains_shifted: list[str] = []
            revisions: list[DomainRevision] = []

            for domain, result in cycle_results.items():
                if "error" in result:
                    continue

                drift = float(result.get("drift", 0.0))
                answer = str(result.get("answer", ""))
                confidence = float(result.get("confidence", 0.5))

                # Check against rolling baseline (gradual drift)
                rolling_drift = self._rolling_drift(domain, answer, snapshots)
                effective_drift = max(drift, rolling_drift)

                if effective_drift >= _DOMAIN_SHIFT_THRESHOLD:
                    domains_shifted.append(domain)

                # Record a revision if there's any meaningful change
                old_answer = current_state.get(domain, {}).get("answer", "")
                if drift > 0.1 and old_answer:
                    revisions.append(DomainRevision(
                        revision_id=str(uuid4()),
                        cycle=cycle,
                        domain=domain,
                        old_answer=old_answer,
                        new_answer=answer,
                        confidence=confidence,
                        drift=drift,
                        recorded_at=_now_iso(),
                    ))

            # Snapshot current state for future rolling comparisons
            self._save_snapshot(current_state, cycle)

            # Write revisions
            for rev in revisions:
                self._write_revision(rev)

            # Update stability score
            n_shifted = len(domains_shifted)
            penalty = min(n_shifted * _DRIFT_PENALTY_PER_DOMAIN, 0.70)
            self._stability = (
                _EMA_ALPHA * (1.0 - penalty) + (1.0 - _EMA_ALPHA) * self._stability
            )
            self._persist_stability()

            # Determine name review
            name_review = (
                n_shifted >= _NAME_REVIEW_DOMAIN_COUNT
                and (cycle - self._last_name_review_cycle) >= _NAME_REVIEW_MIN_CYCLES
            )
            if name_review:
                self._last_name_review_cycle = cycle
                logger.info(
                    "[identity_continuity] Name review warranted — "
                    "%d domains shifted significantly (cycle=%d, stability=%.2f)",
                    n_shifted, cycle, self._stability,
                )

            cumulative = n_shifted / max(len(cycle_results), 1)
            label = self._stability_label()

            report = ContinuityReport(
                cycle=cycle,
                stability_score=round(self._stability, 4),
                stability_label=label,
                domains_shifted=domains_shifted,
                cumulative_drift=round(cumulative, 3),
                name_review_warranted=name_review,
                revisions_recorded=len(revisions),
                summary=self._build_summary(cycle, label, domains_shifted, name_review),
            )

            logger.debug("[identity_continuity] cycle=%d %s", cycle, report.summary)
            return report

    def stability_score(self) -> float:
        """Current EMA stability score [0.0 = in flux, 1.0 = fully stable]."""
        with self._lock:
            return round(self._stability, 4)

    def stability_label(self) -> str:
        """Human-readable stability label."""
        with self._lock:
            return self._stability_label()

    def revision_history(self, limit: int = 20) -> list[dict]:
        """Return the most recent domain revisions (most recent first)."""
        with closing(self._open_db()) as conn:
            rows = conn.execute(
                """
                SELECT revision_id, cycle, domain, old_answer, new_answer,
                       confidence, drift, recorded_at
                FROM identity_revisions
                ORDER BY recorded_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "revision_id": r[0], "cycle": r[1], "domain": r[2],
                "old_answer": r[3], "new_answer": r[4],
                "confidence": r[5], "drift": round(r[6], 4),
                "recorded_at": r[7],
            }
            for r in rows
        ]

    def drift_summary(self) -> dict:
        """Summary statistics over all recorded revisions."""
        with closing(self._open_db()) as conn:
            total = conn.execute("SELECT COUNT(*) FROM identity_revisions").fetchone()[0]
            by_domain = conn.execute(
                "SELECT domain, COUNT(*) as n FROM identity_revisions GROUP BY domain"
            ).fetchall()
        return {
            "total_revisions": total,
            "by_domain": {row[0]: row[1] for row in by_domain},
            "stability_score": self.stability_score(),
            "stability_label": self.stability_label(),
        }

    def snapshot_count(self) -> int:
        with closing(self._open_db()) as conn:
            return conn.execute("SELECT COUNT(*) FROM identity_continuity_snapshots").fetchone()[0]

    # ── Internal ──────────────────────────────────────────────────────────────

    def _stability_label(self) -> str:
        if self._stability >= 0.82:
            return "stable"
        if self._stability >= 0.55:
            return "evolving"
        return "in_flux"

    def _rolling_drift(
        self, domain: str, current_answer: str, snapshots: list[dict]
    ) -> float:
        """Compare current answer to rolling average of recent snapshots."""
        if not snapshots:
            return 0.0
        past_answers = [
            s.get(domain, {}).get("answer", "")
            for s in snapshots
            if isinstance(s.get(domain), dict)
        ]
        past_answers = [a for a in past_answers if a]
        if not past_answers:
            return 0.0
        # Use the median-length answer as representative baseline
        past_answers.sort(key=len)
        baseline = past_answers[len(past_answers) // 2]
        return _word_overlap_drift(baseline, current_answer)

    @staticmethod
    def _build_summary(
        cycle: int,
        label: str,
        shifted: list[str],
        name_review: bool,
    ) -> str:
        parts = [f"cycle={cycle}", f"stability={label}"]
        if shifted:
            parts.append(f"shifted=[{','.join(shifted)}]")
        if name_review:
            parts.append("NAME_REVIEW_WARRANTED")
        return " ".join(parts)

    # ── Database ──────────────────────────────────────────────────────────────

    def _open_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with closing(self._open_db()) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS identity_revisions (
                    revision_id TEXT PRIMARY KEY,
                    cycle       INTEGER NOT NULL,
                    domain      TEXT NOT NULL,
                    old_answer  TEXT NOT NULL,
                    new_answer  TEXT NOT NULL,
                    confidence  REAL NOT NULL,
                    drift       REAL NOT NULL,
                    recorded_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS identity_continuity_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    cycle       INTEGER NOT NULL,
                    state_json  TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS identity_continuity_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            conn.commit()

    def _save_snapshot(self, state: dict, cycle: int) -> None:
        sid = str(uuid4())
        with closing(self._open_db()) as conn:
            conn.execute(
                "INSERT INTO identity_continuity_snapshots "
                "(snapshot_id, cycle, state_json, recorded_at) VALUES (?,?,?,?)",
                (sid, cycle, json.dumps(state), _now_iso()),
            )
            # Keep only 2× rolling window
            conn.execute(
                """
                DELETE FROM identity_continuity_snapshots
                WHERE snapshot_id NOT IN (
                    SELECT snapshot_id FROM identity_continuity_snapshots
                    ORDER BY recorded_at DESC LIMIT ?
                )
                """,
                (_ROLLING_WINDOW * 2,),
            )
            conn.commit()

    def _load_snapshots(self, limit: int) -> list[dict]:
        with closing(self._open_db()) as conn:
            rows = conn.execute(
                "SELECT state_json FROM identity_continuity_snapshots "
                "ORDER BY recorded_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def _write_revision(self, rev: DomainRevision) -> None:
        with closing(self._open_db()) as conn:
            conn.execute(
                "INSERT INTO identity_revisions "
                "(revision_id, cycle, domain, old_answer, new_answer, confidence, drift, recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (rev.revision_id, rev.cycle, rev.domain, rev.old_answer,
                 rev.new_answer, rev.confidence, rev.drift, rev.recorded_at),
            )
            conn.commit()

    def _load_stability(self) -> float:
        with closing(self._open_db()) as conn:
            row = conn.execute(
                "SELECT value FROM identity_continuity_meta WHERE key='stability_score'"
            ).fetchone()
        return float(row[0]) if row else 1.0

    def _persist_stability(self) -> None:
        with closing(self._open_db()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO identity_continuity_meta (key, value) "
                "VALUES ('stability_score', ?)",
                (str(self._stability),),
            )
            conn.commit()
