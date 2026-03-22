"""
EOS — Focus Engine
Active cognitive attention model.

Answers five questions at any moment:
  1. What am I currently concerned with?
  2. What am I tracking right now?
  3. What seems unresolved?
  4. What matters most at the moment?
  5. What changed recently?

The FocusEngine reads runtime signals every turn and produces a FocusState.
The state accumulates naturally — concerns grow when reinforced, decay when
unmentioned, and are pruned when they fall below threshold.

Design principles
-----------------
* No model calls — pure deterministic signal aggregation.  Zero latency.
* Additive + decay: weight-based concern lifecycle.
* Compact: the rendered block is a short working-memory scratchpad.
* Honest: every field reflects what the runtime actually observed.
* Persistent: FocusState is serialisable to the session record and survives
  across turns within a session.

Usage (called from orchestrator.py once per turn)
-------------------------------------------------
    from runtime.focus_engine import FocusEngine

    focus = FocusEngine()

    # Once per turn — pass signals from the turn
    focus.update(signals)       # signals is a list[FocusSignal]

    state = focus.state         # FocusState
    block = focus.render()      # str — inject into system prompt context block
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("eos.focus_engine")

UTC = timezone.utc


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# ── Constants ─────────────────────────────────────────────────────────────────

_INITIAL_WEIGHT: float    = 1.0
_REINFORCE_INCREMENT: float = 0.4
_DECAY_FACTOR: float      = 0.75
_PRUNE_THRESHOLD: float   = 0.15
_MAX_ACTIVE_CONCERNS: int = 6
_MAX_TRACKING: int        = 5
_MAX_UNRESOLVED: int      = 4
_CARRY_FORWARD_THRESHOLD: float = 1.5
_CARRY_FORWARD_RESTORED_WEIGHT: float = 0.9


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Concern:
    """A single item of active cognitive attention with a decaying weight."""
    label: str
    source: str        # memory | tool | initiative | project | turn | signal
    weight: float = _INITIAL_WEIGHT
    first_seen_turn: int = 0
    last_seen_turn: int = 0

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "source": self.source,
            "weight": round(self.weight, 3),
            "first_seen_turn": self.first_seen_turn,
            "last_seen_turn": self.last_seen_turn,
        }


@dataclass
class FocusSignal:
    """
    A signal from the runtime that the FocusEngine should incorporate.

    source     — where it came from (e.g. "tool_failure", "memory_hit", "initiative")
    label      — short human-readable label for the concern it represents
    weight_hint — optional override for initial weight (default: _INITIAL_WEIGHT)
    tracking   — if True, added to the tracking list instead of concerns
    unresolved — if True, added to the unresolved list
    recent_change — if True, added to the recent-changes list
    """
    source: str
    label: str
    weight_hint: float = _INITIAL_WEIGHT
    tracking: bool = False
    unresolved: bool = False
    recent_change: bool = False


@dataclass
class FocusState:
    """Snapshot of the entity's current cognitive focus."""
    turn: int
    sampled_at: str
    primary_concern: Optional[str]
    concerns: list[Concern]       # active, weighted concerns
    tracking: list[str]           # what I'm watching
    unresolved: list[str]         # open questions / unfinished threads
    recent_changes: list[str]     # notable changes this turn
    carry_forward: list[str]      # concerns above carry-forward threshold

    def to_dict(self) -> dict:
        return {
            "turn": self.turn,
            "sampled_at": self.sampled_at,
            "primary_concern": self.primary_concern,
            "concerns": [c.to_dict() for c in self.concerns],
            "tracking": self.tracking,
            "unresolved": self.unresolved,
            "recent_changes": self.recent_changes,
            "carry_forward": self.carry_forward,
        }

    def render_block(self) -> str:
        """
        Compact prompt block for injection into the primary model's context.
        Kept intentionally short — working-memory scratchpad, not a status report.
        """
        if not self.concerns and not self.tracking and not self.unresolved:
            return ""

        lines = ["[focus]"]
        if self.primary_concern:
            lines.append(f"  primary: {self.primary_concern}")
        if self.concerns:
            top = [c.label for c in self.concerns[:3]]
            lines.append(f"  concerns: {', '.join(top)}")
        if self.tracking:
            lines.append(f"  tracking: {', '.join(self.tracking[:3])}")
        if self.unresolved:
            lines.append(f"  unresolved: {', '.join(self.unresolved[:2])}")
        if self.recent_changes:
            lines.append(f"  changed: {', '.join(self.recent_changes[:2])}")
        lines.append("[/focus]")
        return "\n".join(lines)


# ── FocusEngine ───────────────────────────────────────────────────────────────

class FocusEngine:
    """
    Manages the entity's active cognitive focus across turns.

    Call ``update(signals, turn)`` once per turn.
    Read ``state`` for the current FocusState.
    Read ``render()`` for a compact prompt-injection string.
    """

    def __init__(self) -> None:
        self._turn: int = 0
        self._concerns: dict[str, Concern] = {}   # label → Concern
        self._tracking: list[str] = []
        self._unresolved: list[str] = []
        self._recent_changes: list[str] = []
        self._state: Optional[FocusState] = None

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def state(self) -> Optional[FocusState]:
        return self._state

    def render(self) -> str:
        """Return the focus block string (empty string if no focus data)."""
        return self._state.render_block() if self._state else ""

    def update(self, signals: list[FocusSignal], turn: int | None = None) -> FocusState:
        """
        Incorporate new signals from this turn and produce a new FocusState.

        Parameters
        ----------
        signals : list[FocusSignal]
            Signals produced by the orchestrator/tools/initiative for this turn.
        turn : int, optional
            Turn counter.  Auto-increments if not provided.
        """
        if turn is not None:
            self._turn = turn
        else:
            self._turn += 1

        # Clear per-turn transient lists
        self._recent_changes = []
        seen_labels: set[str] = set()

        # Process incoming signals
        for sig in signals:
            label = sig.label
            if not label:
                continue

            if sig.recent_change:
                self._recent_changes.append(label)

            if sig.tracking:
                if label not in self._tracking:
                    self._tracking.insert(0, label)
                self._tracking = self._tracking[:_MAX_TRACKING]
                continue

            if sig.unresolved:
                if label not in self._unresolved:
                    self._unresolved.insert(0, label)
                self._unresolved = self._unresolved[:_MAX_UNRESOLVED]
                continue

            # Concern reinforcement or creation
            seen_labels.add(label)
            if label in self._concerns:
                self._concerns[label].weight += _REINFORCE_INCREMENT
                self._concerns[label].last_seen_turn = self._turn
            else:
                self._concerns[label] = Concern(
                    label=label,
                    source=sig.source,
                    weight=sig.weight_hint,
                    first_seen_turn=self._turn,
                    last_seen_turn=self._turn,
                )

        # Decay concerns not seen this turn
        for label in list(self._concerns.keys()):
            if label not in seen_labels:
                self._concerns[label].weight *= _DECAY_FACTOR
                if self._concerns[label].weight < _PRUNE_THRESHOLD:
                    del self._concerns[label]

        # Sort by weight descending, cap
        sorted_concerns = sorted(
            self._concerns.values(), key=lambda c: c.weight, reverse=True
        )[:_MAX_ACTIVE_CONCERNS]

        primary = sorted_concerns[0].label if sorted_concerns else None

        # Compute carry-forward candidates
        carry_forward = [
            c.label for c in sorted_concerns
            if c.weight >= _CARRY_FORWARD_THRESHOLD
        ]

        self._state = FocusState(
            turn=self._turn,
            sampled_at=_now_iso(),
            primary_concern=primary,
            concerns=sorted_concerns,
            tracking=list(self._tracking),
            unresolved=list(self._unresolved),
            recent_changes=list(self._recent_changes),
            carry_forward=carry_forward,
        )
        return self._state

    def restore_from_dict(self, d: dict) -> None:
        """
        Restore state from a serialised FocusState dict (e.g. on session resume).
        Carry-forward concerns are injected at restored weight.
        """
        if not d:
            return
        self._turn = d.get("turn", 0)
        self._tracking = d.get("tracking", [])
        self._unresolved = d.get("unresolved", [])
        # Restore carry-forward concerns
        for label in d.get("carry_forward", []):
            self._concerns[label] = Concern(
                label=label,
                source="carry_forward",
                weight=_CARRY_FORWARD_RESTORED_WEIGHT,
                first_seen_turn=self._turn,
                last_seen_turn=self._turn,
            )

    def clear(self) -> None:
        """Reset all focus state."""
        self._concerns.clear()
        self._tracking.clear()
        self._unresolved.clear()
        self._recent_changes.clear()
        self._state = None

    # ── Signal builder helpers ────────────────────────────────────────────────

    @staticmethod
    def signal_from_tool_failure(tool_name: str) -> FocusSignal:
        return FocusSignal(
            source="tool_failure",
            label=f"tool:{tool_name} failing",
            weight_hint=1.2,
            unresolved=True,
        )

    @staticmethod
    def signal_from_memory_hit(topic: str) -> FocusSignal:
        return FocusSignal(
            source="memory_hit",
            label=topic,
        )

    @staticmethod
    def signal_from_initiative(initiative_type: str) -> FocusSignal:
        return FocusSignal(
            source="initiative",
            label=f"initiative:{initiative_type}",
            tracking=True,
        )

    @staticmethod
    def signal_from_turn_topic(topic: str) -> FocusSignal:
        return FocusSignal(
            source="turn",
            label=topic,
            weight_hint=1.0,
        )
