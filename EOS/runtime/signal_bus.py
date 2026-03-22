"""Central Signal Bus for M.CORE (Phase 12).

Introduces a shared ``SignalEnvelope`` structure and a ``SignalBus`` registry
that all subsystems publish to instead of wiring directly to each other.

Architecture
------------
Signal producers (world-state collector, reflection engine, memory retrieval
diagnostics, initiative candidate generators, tool failure / anomaly detectors,
project drift detectors) all call ``signal_bus.publish(envelope)``.

The bus then:
1. **Normalises** the envelope (fills defaults, canonicalises fields).
2. **Deduplicates / correlates** signals that share a ``correlation_key``.
3. **Scores salience** so downstream consumers can rank what matters now.
4. **Applies loop guards** to suppress self-reinforcing cycles.
5. **Stores** the processed envelope for admin / diagnostic queries.

The bus does **not** execute actions.  Callers read ``get_salient_signals()``
and hand the results to the existing initiative queue / governance layer.

Key design constraints
----------------------
* Pure-Python stdlib only (no heavy external deps).
* Thread-safe: a single ``threading.Lock`` protects the in-memory registry.
* Backward compatible: existing ``WorldStateSignal`` objects can be adapted
  via ``SignalEnvelope.from_world_state_signal()``.
* All public surfaces return plain dicts or dataclass instances — no ORM magic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger("mcore.signal_bus")

# datetime.UTC was added in Python 3.11; provide a fallback for 3.10
if sys.version_info >= (3, 11):
    from datetime import timezone
    UTC = timezone.utc
else:
    UTC = timezone.utc


# ---------------------------------------------------------------------------
# Utility helpers  (must be defined before any dataclass default_factory refs)
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _minutes_ago(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _plus_minutes(minutes: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEVERITY_CRITICAL = "critical"
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"
SEVERITY_INFO = "info"

_SEVERITY_RANK: dict[str, int] = {
    SEVERITY_CRITICAL: 5,
    SEVERITY_HIGH: 4,
    SEVERITY_MEDIUM: 3,
    SEVERITY_LOW: 2,
    SEVERITY_INFO: 1,
}

# Signal types (open-ended — subsystems may add their own)
STYPE_TOOL_FAILURE = "tool_failure"
STYPE_TOOL_RETRY = "tool_retry"
STYPE_LOG_ANOMALY = "log_anomaly"
STYPE_HIGH_RETRIEVAL = "high_retrieval"
STYPE_ZERO_RETRIEVAL = "zero_retrieval"
STYPE_PROJECT_DRIFT_STALLED = "project_drift_stalled"
STYPE_PROJECT_DRIFT_LOOP = "project_drift_loop"
STYPE_SYSTEM_HEALTH = "system_health"
STYPE_REASONING_LOOP = "reasoning_loop"
STYPE_REFLECTION = "reflection"
STYPE_INITIATIVE_CANDIDATE = "initiative_candidate"
STYPE_ANOMALY = "anomaly"


# ---------------------------------------------------------------------------
# SignalEnvelope
# ---------------------------------------------------------------------------

@dataclass
class SignalEnvelope:
    """Shared signal structure for all M.CORE subsystems.

    Fields
    ------
    signal_id         Globally unique identifier (UUID4 by default).
    correlation_key   Stable key used to group / deduplicate related signals
                      (e.g. "tool_failure:fs_write").  Required for dedup.
    source            Subsystem that produced this signal
                      (e.g. "tool_health_collector", "reflection_engine").
    signal_type       Semantic category (use STYPE_* constants).
    related_entity    Optional entity or scope this signal concerns
                      (project_id, tool_name, topic, etc.).
    timestamp         ISO-8601 UTC string; auto-set to now if omitted.
    severity          "critical" | "high" | "medium" | "low" | "info".
    confidence        0.0–1.0 float.
    recurrence_count  How many times this correlation_key has been seen
                      (maintained by the bus).
    payload           Subsystem-specific detail dict.
    """

    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_key: str = ""
    source: str = "unknown"
    signal_type: str = STYPE_ANOMALY
    related_entity: str = ""
    timestamp: str = field(default_factory=lambda: _iso_now())
    severity: str = SEVERITY_MEDIUM
    confidence: float = 0.5
    recurrence_count: int = 1
    payload: dict[str, Any] = field(default_factory=dict)

    # Bus-assigned fields (set during publish)
    salience_score: float = 0.0
    suppressed: bool = False
    suppression_reason: str = ""
    dedup_group: str = ""   # correlation_key of the canonical signal in group

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def from_world_state_signal(cls, wss: Any) -> "SignalEnvelope":
        """Adapt a legacy ``WorldStateSignal`` into a ``SignalEnvelope``."""
        meta = wss.metadata if hasattr(wss, "metadata") else {}
        priority = getattr(wss, "priority", "medium")
        severity_map = {"high": SEVERITY_HIGH, "medium": SEVERITY_MEDIUM, "low": SEVERITY_LOW}
        return cls(
            correlation_key=getattr(wss, "signal_id", ""),
            source=getattr(wss, "source", "world_state_collector"),
            signal_type=_map_wss_category(getattr(wss, "category", "")),
            related_entity=str(meta.get("linked_project") or meta.get("topic") or meta.get("tool_name") or ""),
            severity=severity_map.get(priority, SEVERITY_MEDIUM),
            confidence=float(meta.get("confidence", 0.65)),
            payload={
                "rationale": getattr(wss, "rationale", ""),
                "category": getattr(wss, "category", ""),
                **meta,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "correlation_key": self.correlation_key,
            "source": self.source,
            "signal_type": self.signal_type,
            "related_entity": self.related_entity,
            "timestamp": self.timestamp,
            "severity": self.severity,
            "confidence": self.confidence,
            "recurrence_count": self.recurrence_count,
            "salience_score": round(self.salience_score, 4),
            "suppressed": self.suppressed,
            "suppression_reason": self.suppression_reason,
            "dedup_group": self.dedup_group,
            "payload": self.payload,
        }


def _map_wss_category(category: str) -> str:
    _map = {
        "tool_failure_investigation": STYPE_TOOL_FAILURE,
        "tool_retry_pattern": STYPE_TOOL_RETRY,
        "log_anomaly_investigation": STYPE_LOG_ANOMALY,
        "memory_consolidation": STYPE_HIGH_RETRIEVAL,
        "stalled_project_follow_up": STYPE_PROJECT_DRIFT_STALLED,
        "unresolved_decision_resurfacing": STYPE_REASONING_LOOP,
        "prepared_next_step_suggestion": STYPE_INITIATIVE_CANDIDATE,
        "upcoming_deadline_reminder": STYPE_INITIATIVE_CANDIDATE,
    }
    return _map.get(category, STYPE_ANOMALY)


# ---------------------------------------------------------------------------
# Salience Scorer
# ---------------------------------------------------------------------------

@dataclass
class SalienceConfig:
    """Weights and thresholds for the salience scoring formula."""

    # Component weights (must roughly sum to 1.0 in practice)
    weight_severity: float = 0.30
    weight_confidence: float = 0.20
    weight_recurrence: float = 0.25
    weight_source_agreement: float = 0.15
    weight_relation_to_active: float = 0.10

    # Recurrence: score saturates at this count
    recurrence_saturation: int = 10

    # Defer/ignore penalty: each deferral reduces salience by this fraction
    defer_penalty_per_deferral: float = 0.08
    max_defer_penalty: float = 0.40

    # Minimum salience to be considered actionable
    actionable_threshold: float = 0.35


def _severity_score(severity: str) -> float:
    rank = _SEVERITY_RANK.get(severity, 2)
    return rank / max(_SEVERITY_RANK.values())  # normalise 0–1


class SalienceScorer:
    """Computes a 0–1 salience score for each signal envelope.

    Factors
    -------
    1. Severity           – higher severity → higher salience.
    2. Confidence         – uncertain signals score lower.
    3. Recurrence         – repeated signals are more salient (up to saturation).
    4. Source agreement   – same correlation_key from multiple sources boosts score.
    5. Active-work relation – signals mentioning entities in active_entities score higher.
    6. Defer history      – signals that were recently ignored/deferred score lower.
    """

    def __init__(self, config: SalienceConfig | None = None) -> None:
        self.config = config or SalienceConfig()

    def score(
        self,
        envelope: SignalEnvelope,
        *,
        source_counts: dict[str, int],       # correlation_key → distinct source count
        active_entities: set[str],            # project IDs, tool names currently active
        defer_counts: dict[str, int],         # correlation_key → times deferred/ignored
    ) -> float:
        cfg = self.config

        # 1. Severity
        s_severity = _severity_score(envelope.severity) * cfg.weight_severity

        # 2. Confidence
        s_confidence = float(envelope.confidence) * cfg.weight_confidence

        # 3. Recurrence (log-scaled, saturated)
        recurrence_ratio = min(envelope.recurrence_count, cfg.recurrence_saturation) / cfg.recurrence_saturation
        s_recurrence = recurrence_ratio * cfg.weight_recurrence

        # 4. Source agreement
        n_sources = source_counts.get(envelope.correlation_key, 1)
        agreement_ratio = min(n_sources, 5) / 5.0
        s_agreement = agreement_ratio * cfg.weight_source_agreement

        # 5. Relation to active work
        related = envelope.related_entity.lower() if envelope.related_entity else ""
        active_match = any(related and (e.lower() in related or related in e.lower()) for e in active_entities)
        s_active = cfg.weight_relation_to_active if active_match else 0.0

        raw = s_severity + s_confidence + s_recurrence + s_agreement + s_active

        # 6. Defer penalty
        n_deferred = defer_counts.get(envelope.correlation_key, 0)
        penalty = min(n_deferred * cfg.defer_penalty_per_deferral, cfg.max_defer_penalty)
        final = max(0.0, raw - penalty)

        return round(min(final, 1.0), 6)


# ---------------------------------------------------------------------------
# Loop Guard
# ---------------------------------------------------------------------------

@dataclass
class LoopGuardConfig:
    """Thresholds for self-reinforcing cycle detection."""

    # If a correlation_key appears more than this many times within the window, suppress it
    recurrence_suppress_threshold: int = 8
    # Rolling window for recurrence tracking
    window_minutes: int = 60
    # After suppression, how long before the key can resurface (cooldown)
    cooldown_minutes: int = 120
    # Suppress a correlation_key if it came from >= this many sources AND keeps recurring
    multi_source_loop_threshold: int = 3


@dataclass
class LoopGuardDecision:
    correlation_key: str
    suppressed: bool
    reason: str
    decided_at: str = field(default_factory=_iso_now)


class LoopGuard:
    """Detects and suppresses self-reinforcing signal cycles.

    Patterns detected
    -----------------
    * **Burst suppression**: same correlation_key fires > N times in a rolling window.
    * **Cooldown enforcement**: a suppressed key cannot resurface until cooldown expires.
    * **Downstream-trigger loop**: an initiative category generates signals that keep
      re-triggering the same initiative (detected via payload ``triggered_by_initiative``).
    """

    def __init__(self, config: LoopGuardConfig | None = None) -> None:
        self.config = config or LoopGuardConfig()
        # correlation_key → list of timestamps (str ISO)
        self._occurrences: dict[str, list[str]] = defaultdict(list)
        # correlation_key → cooldown expiry (str ISO)
        self._cooldowns: dict[str, str] = {}
        self._decisions: list[LoopGuardDecision] = []

    def _purge_old(self, key: str) -> None:
        cutoff = _minutes_ago(self.config.window_minutes)
        self._occurrences[key] = [ts for ts in self._occurrences[key] if ts >= cutoff]

    def _in_cooldown(self, key: str) -> bool:
        expiry = self._cooldowns.get(key)
        if not expiry:
            return False
        return _iso_now() < expiry

    def evaluate(self, envelope: SignalEnvelope) -> LoopGuardDecision:
        key = envelope.correlation_key or envelope.signal_id
        cfg = self.config

        # Cooldown check first
        if self._in_cooldown(key):
            expiry = self._cooldowns[key]
            decision = LoopGuardDecision(
                correlation_key=key,
                suppressed=True,
                reason=f"loop-guard cooldown active until {expiry}",
            )
            self._decisions.append(decision)
            return decision

        # Record occurrence
        self._purge_old(key)
        self._occurrences[key].append(envelope.timestamp or _iso_now())

        count = len(self._occurrences[key])
        if count > cfg.recurrence_suppress_threshold:
            # Set cooldown
            expiry = _plus_minutes(cfg.cooldown_minutes)
            self._cooldowns[key] = expiry
            reason = (
                f"burst suppression: {count} occurrences in {cfg.window_minutes}min window; "
                f"cooldown until {expiry}"
            )
            decision = LoopGuardDecision(correlation_key=key, suppressed=True, reason=reason)
            self._decisions.append(decision)
            return decision

        # Downstream-trigger loop detection
        triggered_by = envelope.payload.get("triggered_by_initiative")
        if triggered_by and envelope.signal_type == STYPE_INITIATIVE_CANDIDATE:
            related_occ = self._occurrences.get(f"initiative_trigger:{triggered_by}", [])
            if len(related_occ) >= cfg.recurrence_suppress_threshold // 2:
                decision = LoopGuardDecision(
                    correlation_key=key,
                    suppressed=True,
                    reason=f"downstream initiative loop: initiative '{triggered_by}' repeatedly re-triggers itself",
                )
                self._decisions.append(decision)
                return decision
            self._occurrences[f"initiative_trigger:{triggered_by}"].append(_iso_now())

        decision = LoopGuardDecision(correlation_key=key, suppressed=False, reason="")
        return decision

    def clear_cooldown(self, correlation_key: str) -> bool:
        """Manually lift a cooldown (admin action)."""
        if correlation_key in self._cooldowns:
            del self._cooldowns[correlation_key]
            return True
        return False

    def recent_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            {
                "correlation_key": d.correlation_key,
                "suppressed": d.suppressed,
                "reason": d.reason,
                "decided_at": d.decided_at,
            }
            for d in self._decisions[-limit:]
        ]

    def active_cooldowns(self) -> dict[str, str]:
        now = _iso_now()
        return {k: v for k, v in self._cooldowns.items() if v > now}


# ---------------------------------------------------------------------------
# Deduplication / Correlation Registry
# ---------------------------------------------------------------------------

class SignalCorrelator:
    """Groups signals sharing a ``correlation_key`` and tracks per-key metadata.

    Within a time window, only the highest-severity representative per
    correlation_key is kept as the canonical signal.  Others are marked as
    correlated members.
    """

    def __init__(self, window_minutes: int = 60) -> None:
        self.window_minutes = window_minutes
        # correlation_key → list of (timestamp, source, severity, signal_id)
        self._groups: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def _purge_old(self, key: str) -> None:
        cutoff = _minutes_ago(self.window_minutes)
        self._groups[key] = [e for e in self._groups[key] if e["timestamp"] >= cutoff]

    def add(self, envelope: SignalEnvelope) -> None:
        key = envelope.correlation_key or envelope.signal_id
        self._purge_old(key)
        self._groups[key].append({
            "signal_id": envelope.signal_id,
            "source": envelope.source,
            "severity": envelope.severity,
            "timestamp": envelope.timestamp,
        })

    def recurrence_count(self, correlation_key: str) -> int:
        self._purge_old(correlation_key)
        return len(self._groups.get(correlation_key, []))

    def source_count(self, correlation_key: str) -> int:
        self._purge_old(correlation_key)
        entries = self._groups.get(correlation_key, [])
        return len({e["source"] for e in entries})

    def source_counts_map(self) -> dict[str, int]:
        # Fix #10: replace the O(K²) loop — which called source_count(k) per key
        # (each invoking _purge_old, iterating the list twice) — with a single
        # O(K×L) pass that purges and counts distinct sources simultaneously.
        cutoff = _minutes_ago(self.window_minutes)
        result: dict[str, int] = {}
        stale_keys = []
        for key, entries in list(self._groups.items()):
            live = [e for e in entries if e["timestamp"] >= cutoff]
            if live:
                self._groups[key] = live
                result[key] = len({e["source"] for e in live})
            else:
                stale_keys.append(key)
        for k in stale_keys:
            del self._groups[k]
        return result

    def is_duplicate(self, envelope: SignalEnvelope) -> bool:
        """True if an identical (same signal_id) envelope has already been seen."""
        key = envelope.correlation_key or envelope.signal_id
        return any(e["signal_id"] == envelope.signal_id for e in self._groups.get(key, []))

    def group_summary(self) -> list[dict[str, Any]]:
        result = []
        for key, entries in self._groups.items():
            if not entries:
                continue
            result.append({
                "correlation_key": key,
                "count": len(entries),
                "sources": sorted({e["source"] for e in entries}),
                "highest_severity": max(entries, key=lambda e: _SEVERITY_RANK.get(e["severity"], 0))["severity"],
                "latest_ts": max(e["timestamp"] for e in entries),
            })
        return sorted(result, key=lambda r: r["latest_ts"], reverse=True)


# ---------------------------------------------------------------------------
# Signal Bus
# ---------------------------------------------------------------------------

class SignalBus:
    """Central signal registry for M.CORE.

    Usage::

        bus = SignalBus()

        # Publish a signal
        envelope = SignalEnvelope(
            correlation_key="tool_failure:fs_write",
            source="tool_health_collector",
            signal_type=STYPE_TOOL_FAILURE,
            related_entity="fs_write",
            severity=SEVERITY_HIGH,
            confidence=0.85,
            payload={"failure_count": 4},
        )
        bus.publish(envelope)

        # Retrieve top signals for the initiative layer
        salient = bus.get_salient_signals(top_n=5)

        # Adapt a legacy WorldStateSignal
        bus.publish_world_state_signal(wss)
    """

    # S-03: severities that are persisted to the durable JSONL sink immediately
    # on publish so they survive a process crash before the initiative layer
    # has had a chance to process them.
    _DURABLE_SEVERITIES: frozenset[str] = frozenset({SEVERITY_HIGH, SEVERITY_CRITICAL})

    def __init__(
        self,
        *,
        salience_config: SalienceConfig | None = None,
        loop_guard_config: LoopGuardConfig | None = None,
        correlator_window_minutes: int = 60,
        registry_max_size: int = 2000,
        state_path: Path | str | None = None,
        durable_signal_log_path: Path | str | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._scorer = SalienceScorer(salience_config)
        self._loop_guard = LoopGuard(loop_guard_config)
        self._correlator = SignalCorrelator(correlator_window_minutes)
        self._registry: list[SignalEnvelope] = []          # all processed envelopes
        self._suppressed: list[SignalEnvelope] = []        # suppressed signals
        self._active_entities: set[str] = set()
        self._defer_counts: dict[str, int] = defaultdict(int)
        self._registry_max_size = registry_max_size
        self._state_path: Path | None = Path(state_path) if state_path else None
        # S-03: durable JSONL sink for high/critical signals
        self._durable_log_path: Path | None = (
            Path(durable_signal_log_path) if durable_signal_log_path else None
        )
        self._durable_log_lock = threading.Lock()
        # R-01: counter for durable log write failures — exposed via diagnostics()
        self._durable_write_failures: int = 0
        # Track publish count for bounded periodic saves (every 50 publishes)
        self._publish_count: int = 0
        self._SAVE_INTERVAL: int = 50

        # Subscribe API — lightweight callback registry (separate lock to allow
        # callbacks to publish without deadlocking on self._lock).
        # _subscriptions: subscription_id → (callback, signal_types_filter, sources_filter, min_severity)
        self._subscriptions: dict[str, tuple] = {}
        self._subs_lock: threading.Lock = threading.Lock()

        # S-03: load any unprocessed durable signals from the JSONL log on boot
        if self._durable_log_path:
            self._load_durable_signals()

    # ------------------------------------------------------------------
    # S-03: Durable signal persistence (crash-safe JSONL sink)
    # ------------------------------------------------------------------

    def _write_durable_signal(self, envelope: SignalEnvelope) -> None:
        """Append a high/critical signal to the durable JSONL sink immediately.

        Each record is a single JSON line with a ``processed_at`` field set to
        None (null) until the initiative layer marks it as consumed.  On boot,
        ``_load_durable_signals`` replays unprocessed records so no critical
        signals are silently lost across a restart.
        """
        if self._durable_log_path is None:
            return
        if envelope.severity not in self._DURABLE_SEVERITIES:
            return
        record = {
            "signal_id": envelope.signal_id,
            "severity": envelope.severity,
            "signal_type": envelope.signal_type,
            "source": envelope.source,
            "correlation_key": envelope.correlation_key,
            "payload": envelope.payload,
            "timestamp": envelope.timestamp,
            "processed_at": None,
        }
        try:
            with self._durable_log_lock:
                self._durable_log_path.parent.mkdir(parents=True, exist_ok=True)
                with self._durable_log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, default=str) + "\n")
        except OSError:
            # R-01: count failures so diagnostics() can surface a non-zero value
            # to the health dashboard without blocking the publish() hot path.
            with self._durable_log_lock:
                self._durable_write_failures += 1
            _log.debug("[signal_bus] durable log write failed (failures=%d)", self._durable_write_failures)

    def _load_durable_signals(self) -> None:
        """Replay unprocessed high/critical signals from the durable JSONL log.

        Fix #03: Route recovered signals through publish() so they receive full
        pipeline treatment — loop guard evaluation, salience scoring, and
        deduplication against already-registered signals — rather than being
        appended raw to the registry with salience_score=0.

        Fix #17: After successfully re-injecting signals, atomically rewrite the
        durable log with those records marked as processed_at=<now>.  This
        prevents the same signals from being replayed on every subsequent restart.

        Called once at bus construction time.  Signals with ``processed_at``
        already set are skipped.  Successfully re-injected signals are logged
        at WARNING level so the operator can see what was recovered.
        """
        if self._durable_log_path is None or not self._durable_log_path.exists():
            return
        try:
            lines = self._durable_log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return

        recovered = 0
        now_ts = _iso_now()
        # Track which signal_ids we successfully re-inject so we can mark them
        # processed in the JSONL rewrite below.
        recovered_ids: set[str] = set()

        for raw in lines:
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if record.get("processed_at") is not None:
                continue  # already processed in a previous session
            try:
                # Fix #03: Build the envelope then route through publish() for
                # full pipeline treatment (loop guard, salience, dedup).
                # Set a fresh signal_id so the recovered signal doesn't collide
                # with any in-session duplicate-detection based on the original id.
                original_id = record.get("signal_id") or ""
                envelope = SignalEnvelope(
                    signal_id=str(uuid.uuid4()),  # fresh id for in-session dedup
                    severity=record.get("severity", SEVERITY_HIGH),
                    signal_type=record.get("signal_type", "recovered_signal"),
                    source=record.get("source", "durable_log_recovery"),
                    correlation_key=record.get("correlation_key") or "",
                    payload={**(record.get("payload") or {}), "recovered_from_durable_log": True,
                             "original_signal_id": original_id},
                    timestamp=record.get("timestamp") or now_ts,
                )
                # publish() acquires the lock internally; safe to call here
                # because __init__ has already finished setting up all state.
                self.publish(envelope)
                if original_id:
                    recovered_ids.add(original_id)
                recovered += 1
            except Exception:
                continue

        if recovered > 0:
            _log.warning(
                "[signal_bus] Recovered %d unprocessed high/critical signals from durable log.",
                recovered,
            )

        # Fix #17: Rewrite the durable log atomically, marking recovered records
        # as processed so they are not replayed on the next restart.
        if recovered_ids and self._durable_log_path is not None:
            try:
                updated_lines: list[str] = []
                for raw in lines:
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        updated_lines.append(raw)
                        continue
                    sig_id = record.get("signal_id", "")
                    if sig_id in recovered_ids and record.get("processed_at") is None:
                        record["processed_at"] = now_ts
                    updated_lines.append(json.dumps(record, default=str))
                tmp = self._durable_log_path.with_suffix(".tmp")
                with self._durable_log_lock:
                    tmp.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
                    os.replace(tmp, self._durable_log_path)
            except OSError as _exc:
                _log.debug("[signal_bus] Could not mark durable log records as processed: %s", _exc)

    # ------------------------------------------------------------------
    # Active-entity management (called by initiative layer)
    # ------------------------------------------------------------------

    def set_active_entities(self, entities: set[str] | list[str]) -> None:
        """Inform the bus which project IDs / tool names are currently active."""
        with self._lock:
            self._active_entities = set(entities)

    def add_active_entity(self, entity: str) -> None:
        with self._lock:
            self._active_entities.add(entity)

    def remove_active_entity(self, entity: str) -> None:
        with self._lock:
            self._active_entities.discard(entity)

    # ------------------------------------------------------------------
    # Defer / ignore feedback (from initiative governance layer)
    # ------------------------------------------------------------------

    def record_defer(self, correlation_key: str) -> None:
        """Record that the governance layer deferred/ignored this signal."""
        with self._lock:
            self._defer_counts[correlation_key] += 1
        # Persist after every defer update — defers are infrequent so the
        # write cost is negligible and ensures the count survives a crash.
        if self._state_path:
            self._save_state_unlocked()

    # ── State persistence (crash-safe) ───────────────────────────────────────

    def save_state(self, path: Path | str | None = None) -> bool:
        """Atomically save LoopGuard cooldowns and defer counts to *path*.

        Args:
            path: File path override.  Uses ``self._state_path`` when None.

        Returns:
            True on success, False on failure.
        """
        target = Path(path) if path else self._state_path
        if not target:
            return False
        with self._lock:
            return self._save_state_to(target)

    def _save_state_unlocked(self) -> None:
        """Write state without acquiring the lock (caller must already hold it)."""
        if self._state_path:
            try:
                self._save_state_to(self._state_path)
            except Exception:
                pass

    def _save_state_to(self, path: Path) -> bool:
        """Perform the atomic write to *path*."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": 1,
                "saved_at": _iso_now(),
                "cooldowns": dict(self._loop_guard._cooldowns),
                "defer_counts": dict(self._defer_counts),
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            return True
        except Exception as exc:
            _log.debug("[signal_bus] State save failed: %s", exc)
            return False

    def load_state(self, path: Path | str | None = None) -> bool:
        """Load persisted LoopGuard cooldowns and defer counts from *path*.

        Args:
            path: File path override.  Uses ``self._state_path`` when None.

        Returns:
            True if state was loaded, False if the file was missing or invalid.
        """
        target = Path(path) if path else self._state_path
        if not target or not target.exists():
            return False
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return False
            now = _iso_now()
            with self._lock:
                # Restore cooldowns — only keep entries that haven't expired yet
                for key, expiry in (data.get("cooldowns") or {}).items():
                    if isinstance(expiry, str) and expiry > now:
                        self._loop_guard._cooldowns[key] = expiry
                # Restore defer counts
                for key, count in (data.get("defer_counts") or {}).items():
                    if isinstance(count, int) and count > 0:
                        self._defer_counts[key] = count
            _log.info(
                "[signal_bus] State loaded: %d cooldowns, %d defer entries",
                len(self._loop_guard._cooldowns),
                len(self._defer_counts),
            )
            return True
        except Exception as exc:
            _log.warning("[signal_bus] State load failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish(self, envelope: SignalEnvelope) -> SignalEnvelope:
        """Normalise, deduplicate, score, and register a signal envelope.

        Returns the processed envelope (with salience_score and suppression
        flags set).  The caller does not need to use the return value — the
        bus stores it internally.
        """
        with self._lock:
            # 1. Normalise
            envelope = self._normalise(envelope)

            # 2. Skip exact duplicate signal_id
            if self._correlator.is_duplicate(envelope):
                return envelope

            # 3. Record in correlator (updates recurrence / source counts)
            self._correlator.add(envelope)

            # 4. Update recurrence count on envelope
            envelope.recurrence_count = self._correlator.recurrence_count(
                envelope.correlation_key or envelope.signal_id
            )

            # 5. Loop guard
            decision = self._loop_guard.evaluate(envelope)
            if decision.suppressed:
                envelope.suppressed = True
                envelope.suppression_reason = decision.reason
                self._suppressed.append(envelope)
                self._trim(self._suppressed)
                # Persist when a new cooldown is set so it survives a restart
                if self._state_path and "cooldown" in decision.reason:
                    self._save_state_to(self._state_path)
                return envelope

            # 6. Salience scoring
            envelope.salience_score = self._scorer.score(
                envelope,
                source_counts=self._correlator.source_counts_map(),
                active_entities=self._active_entities,
                defer_counts=self._defer_counts,
            )

            # 7. Store
            self._registry.append(envelope)
            self._trim(self._registry)

            # Bounded periodic save — every _SAVE_INTERVAL publishes
            self._publish_count += 1
            if self._state_path and self._publish_count % self._SAVE_INTERVAL == 0:
                self._save_state_to(self._state_path)

            # S-03: mark envelope as needing durable write (done outside lock below)
            _needs_durable_write = envelope.severity in self._DURABLE_SEVERITIES

        # S-03: write high/critical signals to the durable JSONL log outside the
        # bus lock so file I/O never stalls signal publishing in other threads.
        if _needs_durable_write:
            self._write_durable_signal(envelope)

        # Dispatch to subscribers (outside all locks; callbacks may publish).
        if not envelope.suppressed:
            self._dispatch_to_subscribers(envelope)

        return envelope

    # ------------------------------------------------------------------
    # Subscribe API
    # ------------------------------------------------------------------

    def subscribe(
        self,
        callback,                          # Callable[[SignalEnvelope], None]
        *,
        signal_types: "frozenset[str] | set[str] | None" = None,
        sources: "frozenset[str] | set[str] | None" = None,
        min_severity: str = SEVERITY_INFO,
    ) -> str:
        """
        Register a callback to be called when a matching signal is published.

        The callback is invoked synchronously in the publishing thread,
        **outside** the bus lock, so it is safe for callbacks to call
        ``bus.publish()`` without deadlocking.

        Parameters
        ----------
        callback : Callable[[SignalEnvelope], None]
            Function to call with the matching signal.
        signal_types : set[str] | None
            If given, only signals whose ``signal_type`` is in the set will
            trigger the callback.  ``None`` means all types.
        sources : set[str] | None
            If given, only signals from one of these sources will trigger.
            ``None`` means all sources.
        min_severity : str
            Minimum severity for the callback to fire (default: "info" → all).

        Returns
        -------
        str
            A subscription_id that can be passed to ``unsubscribe()``.
        """
        sub_id = str(uuid.uuid4())
        entry = (
            callback,
            frozenset(signal_types) if signal_types is not None else None,
            frozenset(sources)      if sources      is not None else None,
            min_severity,
        )
        with self._subs_lock:
            self._subscriptions[sub_id] = entry
        return sub_id

    def unsubscribe(self, subscription_id: str) -> bool:
        """
        Remove a previously registered callback.

        Returns True if the subscription was found and removed.
        """
        with self._subs_lock:
            return self._subscriptions.pop(subscription_id, None) is not None

    def _dispatch_to_subscribers(self, envelope: SignalEnvelope) -> None:
        """Call all matching subscriber callbacks (outside the main bus lock)."""
        with self._subs_lock:
            subs = list(self._subscriptions.values())

        sev_rank = _SEVERITY_RANK.get(envelope.severity, 1)

        for callback, type_filter, source_filter, min_sev in subs:
            try:
                # Check severity threshold
                if sev_rank < _SEVERITY_RANK.get(min_sev, 1):
                    continue
                # Check signal_type filter
                if type_filter is not None and envelope.signal_type not in type_filter:
                    continue
                # Check source filter
                if source_filter is not None and envelope.source not in source_filter:
                    continue
                callback(envelope)
            except Exception as exc:
                _log.debug("[signal_bus] subscriber callback raised: %s", exc)

    def publish_world_state_signal(self, wss: Any) -> SignalEnvelope:
        """Convenience wrapper: adapt a WorldStateSignal and publish it."""
        envelope = SignalEnvelope.from_world_state_signal(wss)
        return self.publish(envelope)

    def publish_many(self, envelopes: list[SignalEnvelope]) -> list[SignalEnvelope]:
        return [self.publish(e) for e in envelopes]

    def publish_world_state_signals(self, signals: list[Any]) -> list[SignalEnvelope]:
        return [self.publish_world_state_signal(s) for s in signals]

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def get_salient_signals(
        self,
        *,
        top_n: int = 20,
        min_salience: float = 0.0,
        signal_types: list[str] | None = None,
        exclude_suppressed: bool = True,
    ) -> list[SignalEnvelope]:
        """Return signals ranked by salience, highest first."""
        with self._lock:
            pool = [
                e for e in self._registry
                if (not exclude_suppressed or not e.suppressed)
                and e.salience_score >= min_salience
                and (signal_types is None or e.signal_type in signal_types)
            ]
            pool.sort(key=lambda e: e.salience_score, reverse=True)
            return pool[:top_n]

    def get_all_signals(self, limit: int = 200) -> list[SignalEnvelope]:
        with self._lock:
            return list(self._registry[-limit:])

    def get_suppressed_signals(self, limit: int = 100) -> list[SignalEnvelope]:
        with self._lock:
            return list(self._suppressed[-limit:])

    # ------------------------------------------------------------------
    # Admin / diagnostics
    # ------------------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """Full admin-visible diagnostic snapshot."""
        with self._lock:
            all_sigs = list(self._registry)
            suppressed = list(self._suppressed)
            salient = sorted(all_sigs, key=lambda e: e.salience_score, reverse=True)[:20]

            return {
                "registry_size": len(all_sigs),
                "suppressed_count": len(suppressed),
                "active_entities": sorted(self._active_entities),
                "recent_signals": [e.to_dict() for e in all_sigs[-50:]],
                "salient_signals": [e.to_dict() for e in salient],
                "suppressed_signals": [e.to_dict() for e in suppressed[-30:]],
                "correlation_groups": self._correlator.group_summary(),
                "loop_guard": {
                    "recent_decisions": self._loop_guard.recent_decisions(limit=30),
                    "active_cooldowns": self._loop_guard.active_cooldowns(),
                },
                "defer_counts": dict(self._defer_counts),
                "salience_config": {
                    "weight_severity": self._scorer.config.weight_severity,
                    "weight_confidence": self._scorer.config.weight_confidence,
                    "weight_recurrence": self._scorer.config.weight_recurrence,
                    "weight_source_agreement": self._scorer.config.weight_source_agreement,
                    "weight_relation_to_active": self._scorer.config.weight_relation_to_active,
                    "actionable_threshold": self._scorer.config.actionable_threshold,
                },
                "loop_guard_config": {
                    "recurrence_suppress_threshold": self._loop_guard.config.recurrence_suppress_threshold,
                    "window_minutes": self._loop_guard.config.window_minutes,
                    "cooldown_minutes": self._loop_guard.config.cooldown_minutes,
                },
                # R-01: expose durable write failures so the health dashboard can alert
                "durable_write_failures": self._durable_write_failures,
                "durable_log_healthy": self._durable_write_failures == 0,
            }

    def clear_cooldown(self, correlation_key: str) -> bool:
        """Admin: lift a loop-guard cooldown for a given correlation key."""
        with self._lock:
            return self._loop_guard.clear_cooldown(correlation_key)

    def reset(self) -> None:
        """Admin: clear all state (useful for testing)."""
        with self._lock:
            self._registry.clear()
            self._suppressed.clear()
            self._defer_counts.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _normalise(self, env: SignalEnvelope) -> SignalEnvelope:
        """Fill defaults, canonicalise severity, clamp confidence."""
        if not env.signal_id:
            env.signal_id = str(uuid.uuid4())
        if not env.timestamp:
            env.timestamp = _iso_now()
        if env.severity not in _SEVERITY_RANK:
            env.severity = SEVERITY_MEDIUM
        env.confidence = max(0.0, min(1.0, float(env.confidence)))
        if not env.correlation_key:
            # Derive a stable correlation key from source + signal_type + entity
            raw = f"{env.source}:{env.signal_type}:{env.related_entity}"
            env.correlation_key = "auto:" + hashlib.sha256(raw.encode()).hexdigest()[:16]
        return env

    def _trim(self, lst: list) -> None:
        if len(lst) > self._registry_max_size:
            del lst[: len(lst) - self._registry_max_size]


# ---------------------------------------------------------------------------
# Singleton accessor (optional convenience)
# ---------------------------------------------------------------------------

_DEFAULT_BUS: SignalBus | None = None
_BUS_LOCK = threading.Lock()


def get_default_bus(state_path: Path | str | None = None) -> SignalBus:
    """Return the process-level default SignalBus, creating it if necessary.

    On first creation the bus will attempt to load persisted state from
    *state_path* (defaults to ``data/signal_bus_state.json`` relative to the
    module root) so that loop-guard cooldowns and defer counts survive restarts.
    """
    global _DEFAULT_BUS
    with _BUS_LOCK:
        if _DEFAULT_BUS is None:
            # Resolve default state path via the canonical StatePathRegistry.
            # Falls back to an inline path if the registry is not yet initialized
            # (e.g. called from a test or an isolated import).
            if state_path is None:
                try:
                    from .state_path_registry import get_path as _get_state_path
                    state_path = _get_state_path("signal_bus_state")
                except Exception:
                    _repo_root = Path(__file__).parent.parent
                    state_path = _repo_root / "data" / "signal_bus_state.json"
            _bus = SignalBus(state_path=state_path)
            _bus.load_state()   # no-op if file does not exist
            _DEFAULT_BUS = _bus
        return _DEFAULT_BUS


def reset_default_bus() -> None:
    """Replace the default bus (useful for testing)."""
    global _DEFAULT_BUS
    with _BUS_LOCK:
        _DEFAULT_BUS = None


