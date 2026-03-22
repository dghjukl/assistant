"""Cognition Tracer — structured introspection layer for the Cognition admin tab.

Records per-turn traces, memory influence data, reflection events, and state
deltas without touching or replacing any existing logging.  All data is kept
in in-memory ring buffers; nothing is written to disk by this module.

Usage
-----
    from runtime.cognition_tracer import CognitionTracer

    tracer = CognitionTracer(
        turn_ring_size=200,
        reflection_ring_size=100,
        state_ring_size=100,
        enabled=True,
    )

    # Called once per turn from the web-server chat handler:
    tracer.record_turn(turn_trace)
    tracer.record_memory(memory_trace)
    tracer.record_state_delta(state_delta)

    # Called from reflection loops when they emit conclusions:
    tracer.record_reflection(reflection_event)
"""

from __future__ import annotations

import collections
import threading
from datetime import datetime, timezone
from typing import Any

_UTC = timezone.utc


def _now_iso() -> str:
    return datetime.now(_UTC).isoformat().replace("+00:00", "Z")


class CognitionTracer:
    """Thread-safe in-memory store for structured cognition traces.

    All public ``record_*``, ``list_*``, ``get_*``, and ``summary()`` methods
    take a short-lived re-entrant lock for method-local consistency. Query
    methods return shallow copies so callers cannot mutate internal buffers.
    All public ``record_*`` methods are no-ops when ``enabled=False`` so
    performance cost is near-zero when the feature is toggled off.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        turn_ring_size: int = 200,
        reflection_ring_size: int = 100,
        state_ring_size: int = 100,
    ) -> None:
        self.enabled = enabled
        # Ring buffers keyed by trace type
        self._turns: collections.deque[dict[str, Any]] = collections.deque(
            maxlen=turn_ring_size
        )
        self._memory: collections.deque[dict[str, Any]] = collections.deque(
            maxlen=turn_ring_size
        )
        self._reflections: collections.deque[dict[str, Any]] = collections.deque(
            maxlen=reflection_ring_size
        )
        self._state_deltas: collections.deque[dict[str, Any]] = collections.deque(
            maxlen=state_ring_size
        )
        self._lock = threading.RLock()
        # Index: turn_id → position (latest wins)
        self._turn_index: dict[str, int] = {}

    # ──────────────────────────────────────────────────────────────────────────
    # Record methods
    # ──────────────────────────────────────────────────────────────────────────

    def record_turn(self, trace: dict[str, Any]) -> None:
        """Append a turn_trace record.

        Expected fields (all optional — only available fields are stored):
          turn_id            str       e.g. "T42"
          timestamp          str       ISO-8601; auto-filled if absent
          user_input         str       raw user text
          model_server       str       "gpu_frontend" | "gpu_reasoning"
          escalation         bool      whether reasoning escalation occurred
          escalation_reason  str|None  why escalation happened
          tool_routing       str       route decision label
          tools_invoked      list[str] tool names actually called
          retrieval_results  list      memory IDs or short summaries
          response_route     str       final route taken
          elapsed_ms         int       total turn latency
        """
        with self._lock:
            if not self.enabled:
                return
            entry: dict[str, Any] = {
                "timestamp": _now_iso(),
                **trace,
            }
            self._turns.append(entry)

    def record_memory(self, trace: dict[str, Any]) -> None:
        """Append a memory_trace record for a turn.

        Expected fields:
          turn_id            str
          timestamp          str       auto-filled if absent
          items_retrieved    list      memory items fetched from the store
          items_injected     list      items that made it into the prompt
          ranking_scores     dict      {memory_id: score} if available
          used_in_assembly   list[str] IDs actually used in final prompt
        """
        with self._lock:
            if not self.enabled:
                return
            entry: dict[str, Any] = {
                "timestamp": _now_iso(),
                **trace,
            }
            self._memory.append(entry)

    def record_reflection(self, event: dict[str, Any]) -> None:
        """Append a reflection_event record.

        Expected fields:
          reflection_id      str       unique identifier
          timestamp          str       auto-filled if absent
          inputs_reviewed    list      summaries of inputs to the reflection
          conclusions        list[str] generated conclusions
          suggestions        list[str] improvement hypotheses
          similar_before     bool|int  whether/how many similar reflections seen
          trigger            str       what caused this reflection ("scheduled"|"turn")
        """
        with self._lock:
            if not self.enabled:
                return
            entry: dict[str, Any] = {
                "timestamp": _now_iso(),
                **event,
            }
            self._reflections.append(entry)

    def record_state_delta(self, delta: dict[str, Any]) -> None:
        """Append a state_delta record for a turn.

        Expected fields:
          turn_id            str
          timestamp          str       auto-filled if absent
          snapshot_before    dict      state keys/values before the turn
          snapshot_after     dict      state keys/values after the turn
          diff               dict      keys that changed with old/new values
          new_memory_entries list      memory IDs written during this turn
        """
        with self._lock:
            if not self.enabled:
                return
            entry: dict[str, Any] = {
                "timestamp": _now_iso(),
                **delta,
            }
            self._state_deltas.append(entry)

    # ──────────────────────────────────────────────────────────────────────────
    # Query methods (used by API endpoints)
    # ──────────────────────────────────────────────────────────────────────────

    def list_turns(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the *limit* most recent turn traces (newest last)."""
        with self._lock:
            items = list(self._turns)
            return [dict(item) for item in items[-limit:]]

    def get_turn(self, turn_id: str) -> dict[str, Any] | None:
        """Return the most recent trace for *turn_id*, or None."""
        with self._lock:
            for entry in reversed(self._turns):
                if entry.get("turn_id") == turn_id:
                    return dict(entry)
            return None

    def get_memory_for_turn(self, turn_id: str) -> dict[str, Any] | None:
        """Return the memory trace for *turn_id*, or None."""
        with self._lock:
            for entry in reversed(self._memory):
                if entry.get("turn_id") == turn_id:
                    return dict(entry)
            return None

    def list_memory(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the *limit* most recent memory traces."""
        with self._lock:
            items = list(self._memory)
            return [dict(item) for item in items[-limit:]]

    def list_reflections(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the *limit* most recent reflection events."""
        with self._lock:
            items = list(self._reflections)
            return [dict(item) for item in items[-limit:]]

    def get_state_delta_for_turn(self, turn_id: str) -> dict[str, Any] | None:
        """Return the state delta for *turn_id*, or None."""
        with self._lock:
            for entry in reversed(self._state_deltas):
                if entry.get("turn_id") == turn_id:
                    return dict(entry)
            return None

    def list_state_deltas(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the *limit* most recent state deltas."""
        with self._lock:
            items = list(self._state_deltas)
            return [dict(item) for item in items[-limit:]]

    def summary(self) -> dict[str, Any]:
        """Lightweight summary for the admin header / health check."""
        with self._lock:
            return {
                "enabled": self.enabled,
                "turn_count": len(self._turns),
                "memory_trace_count": len(self._memory),
                "reflection_count": len(self._reflections),
                "state_delta_count": len(self._state_deltas),
            }
