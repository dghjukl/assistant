from __future__ import annotations

from runtime.current_focus import CurrentFocusService


class _Goal:
    def __init__(self, goal_id: str, description: str, context: str = "", last_updated_at: float = 1.0):
        self.goal_id = goal_id
        self.description = description
        self.context = context
        self.last_updated_at = last_updated_at
        self.priority = "normal"


class _GoalStore:
    def __init__(self, goals):
        self._goals = goals

    def active_goals(self):
        return list(self._goals)


class _InitiativeEngine:
    def current_focus(self):
        return {
            "focus_id": "INI-1",
            "title": "Session Checkpoint",
            "why_now": "Turn counter reached its checkpoint threshold.",
            "next_action": "Run the queued initiative.",
            "status": "active",
            "source": "initiative",
            "updated_at": "2026-03-23T00:00:00Z",
            "metadata": {},
        }


class _InvestigationEngine:
    def current_focus(self):
        return {
            "focus_id": "INV-1",
            "title": "Missing telemetry",
            "why_now": "An unresolved investigation remains open.",
            "next_action": "Run evidence review.",
            "status": "active",
            "source": "investigation",
            "updated_at": "2026-03-23T00:00:00Z",
            "metadata": {},
        }


def test_current_focus_prefers_active_goal():
    svc = CurrentFocusService()
    svc.wire(
        goal_store=_GoalStore([_Goal("goal-1", "Finish deployment", "User asked to finish rollout")]),
        initiative_engine=_InitiativeEngine(),
        investigation_engine=_InvestigationEngine(),
    )

    focus = svc.get_current_focus().to_dict()

    assert focus["focus_id"] == "goal-1"
    assert focus["source"] == "user_goal"
    assert focus["title"] == "Finish deployment"


def test_current_focus_falls_back_to_initiative_then_investigation_then_background():
    svc = CurrentFocusService()
    svc.wire(
        goal_store=_GoalStore([]),
        initiative_engine=_InitiativeEngine(),
        investigation_engine=_InvestigationEngine(),
    )

    focus = svc.get_current_focus().to_dict()
    assert focus["source"] == "initiative"

    svc = CurrentFocusService()
    svc.wire(goal_store=_GoalStore([]), investigation_engine=_InvestigationEngine())
    focus = svc.get_current_focus().to_dict()
    assert focus["source"] == "investigation"

    svc = CurrentFocusService()
    svc.set_background_focus(
        focus_id="maint-1",
        title="Run maintenance",
        why_now="No user goal is active.",
        next_action="Compact memory.",
    )
    focus = svc.get_current_focus().to_dict()
    assert focus["focus_id"] == "maint-1"
    assert focus["source"] == "maintenance"


def test_render_agenda_block_contains_focus_fields():
    svc = CurrentFocusService()
    svc.set_background_focus(
        focus_id="maint-2",
        title="Review memory health",
        why_now="Maintenance interval elapsed.",
        next_action="Run maintenance checks.",
        status="active",
    )

    agenda = svc.render_agenda_block()

    assert "## Current Agenda" in agenda
    assert "Review memory health" in agenda
    assert "Run maintenance checks." in agenda
