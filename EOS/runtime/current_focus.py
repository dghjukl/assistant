"""
EOS — Current Focus Service
One authoritative "what the entity is attending to now" record shared across
chat, admin, and background subsystems.

Resolution order
----------------
1. Active user goal from GoalStore
2. Pending or active initiative from InitiativeEngine
3. Open/active investigation from InvestigationEngine
4. Background maintenance/reflection focus set explicitly by loops

The service keeps the output schema stable even when no source is available,
so callers can always ask for the current focus without reconstructing it from
multiple runtime systems.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

UTC = timezone.utc


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class CurrentFocusRecord:
    focus_id: str
    title: str
    why_now: str
    next_action: str
    status: str
    source: str
    updated_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CurrentFocusService:
    """Resolve and cache the entity's current focus across runtime subsystems."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._goal_store = None
        self._initiative_engine = None
        self._investigation_engine = None
        self._maintenance_focus: CurrentFocusRecord | None = None

    def wire(
        self,
        *,
        goal_store=None,
        initiative_engine=None,
        investigation_engine=None,
    ) -> None:
        if goal_store is not None:
            self._goal_store = goal_store
        if initiative_engine is not None:
            self._initiative_engine = initiative_engine
        if investigation_engine is not None:
            self._investigation_engine = investigation_engine

    def set_background_focus(
        self,
        *,
        title: str,
        why_now: str,
        next_action: str,
        status: str = "active",
        source: str = "maintenance",
        focus_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CurrentFocusRecord:
        record = CurrentFocusRecord(
            focus_id=focus_id or f"FOCUS-{uuid.uuid4().hex[:10]}",
            title=title.strip(),
            why_now=why_now.strip(),
            next_action=next_action.strip(),
            status=status,
            source=source,
            updated_at=_now_iso(),
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._maintenance_focus = record
        return record

    def clear_background_focus(self, *, focus_id: str | None = None) -> None:
        with self._lock:
            if focus_id and self._maintenance_focus and self._maintenance_focus.focus_id != focus_id:
                return
            self._maintenance_focus = None

    def get_current_focus(self) -> CurrentFocusRecord:
        with self._lock:
            maintenance = self._maintenance_focus

        dynamic = (
            self._goal_focus()
            or self._initiative_focus()
            or self._investigation_focus()
            or maintenance
        )
        if dynamic is not None:
            return dynamic

        return CurrentFocusRecord(
            focus_id="focus-idle",
            title="Stand by for the next meaningful task",
            why_now="No active user goal, initiative, investigation, or maintenance task is currently in flight.",
            next_action="Wait for user input or the next scheduled background cycle.",
            status="waiting",
            source="maintenance",
            updated_at=_now_iso(),
        )

    def render_agenda_block(self) -> str:
        focus = self.get_current_focus()
        return "\n".join([
            "## Current Agenda",
            f"- Focus: {focus.title}",
            f"- Why now: {focus.why_now}",
            f"- Next: {focus.next_action}",
            f"- Status: {focus.status} · Source: {focus.source} · Updated: {focus.updated_at}",
        ])

    def _goal_focus(self) -> CurrentFocusRecord | None:
        store = self._goal_store
        if store is None:
            return None
        try:
            active = store.active_goals()
        except Exception:
            return None
        if not active:
            return None

        goal = active[0]
        why_now = (goal.context or "").strip() or "This is the highest-priority active user goal."
        return CurrentFocusRecord(
            focus_id=goal.goal_id,
            title=goal.description,
            why_now=why_now,
            next_action="Advance this goal in the next response or action.",
            status="active",
            source="user_goal",
            updated_at=_epoch_to_iso(goal.last_updated_at),
            metadata={"priority": goal.priority},
        )

    def _initiative_focus(self) -> CurrentFocusRecord | None:
        engine = self._initiative_engine
        if engine is None or not hasattr(engine, "current_focus"):
            return None
        try:
            item = engine.current_focus()
        except Exception:
            return None
        if not item:
            return None
        return CurrentFocusRecord(**item)

    def _investigation_focus(self) -> CurrentFocusRecord | None:
        engine = self._investigation_engine
        if engine is None or not hasattr(engine, "current_focus"):
            return None
        try:
            item = engine.current_focus()
        except Exception:
            return None
        if not item:
            return None
        return CurrentFocusRecord(**item)


def _epoch_to_iso(value: float | int | None) -> str:
    if value is None:
        return _now_iso()
    return datetime.fromtimestamp(float(value), tz=UTC).isoformat().replace("+00:00", "Z")
