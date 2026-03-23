from runtime.entity_state_service import select_behavior_mode
from core.entity import build_system_prompt


class _Snapshot:
    name_clause = "Your name is EOS."
    identity_clause = "  [values]: steady"
    relational_clause = "  partner: trusted"
    autonomy_clause = "Autonomy is available."
    goals_block = ""
    workspace_block = ""
    worldview_block = ""
    environment_block = ""
    session_primer = ""
    behavior_block = "## Active Outward Behavior\n- Mode: resume_prior_work"
    runtime_status_block = "## Runtime Status\nServers: primary OK"


def test_behavior_mode_prefers_resuming_prior_work_when_prior_session_and_active_focus():
    summary = select_behavior_mode(
        current_focus={
            "title": "Finish rollout",
            "source": "user_goal",
            "status": "active",
            "next_action": "Continue the rollout checklist.",
        },
        session={
            "has_prior_session": True,
            "session_ended_at": "2026-03-22T12:00:00Z",
            "turn_count": 8,
        },
        environment={"summary": {"headline": "workspace healthy"}},
        capabilities={"status_line": "healthy=3/3"},
        initiative={"queue_depth": 0, "idle_seconds": 5},
        interaction_count=1,
    )

    assert summary["mode"] == "resume_prior_work"
    assert "Finish rollout" in summary["rationale"]


def test_behavior_mode_notices_environment_change_before_other_stances():
    summary = select_behavior_mode(
        current_focus={
            "title": "Stand by",
            "source": "maintenance",
            "status": "waiting",
            "next_action": "Wait for user input.",
        },
        session={"has_prior_session": False},
        environment={"summary": {"headline": "tool server degraded"}},
        capabilities={"status_line": "healthy=2/3, unhealthy=1"},
        initiative={"queue_depth": 3, "idle_seconds": 20},
        interaction_count=4,
    )

    assert summary["mode"] == "notice_environment_change"
    assert "tool server degraded" in summary["rationale"].lower()


def test_build_system_prompt_includes_selected_behavior_block():
    prompt = build_system_prompt(entity_snapshot=_Snapshot())

    assert "## Active Outward Behavior" in prompt
    assert "- Mode: resume_prior_work" in prompt
