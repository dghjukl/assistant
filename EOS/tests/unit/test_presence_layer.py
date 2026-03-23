from runtime.presence_layer import build_presence_state


def test_presence_layer_renders_system_prompt_and_ambient_cues():
    state = build_presence_state(
        current_focus={
            "title": "Review memory health",
            "next_action": "Inspect recent maintenance signals.",
            "source": "maintenance",
        },
        continuity={"has_prior_session": True, "session_ended_at": "2026-03-22T12:00:00Z", "turn_count": 8},
        environment={"summary": {"headline": "desktop reachable; workspace healthy"}},
        capabilities={"status_line": "healthy"},
        recent_events=[{"message": "Backup complete", "source": "backup", "level": "info"}],
        initiative={"enabled": True, "queue_depth": 1},
        idle={"tier": "resting"},
    )

    block = state.render_system_prompt_block()
    ambient = state.ambient_payload()

    assert "## Presence Layer" in block
    assert "Backup complete" in block
    assert ambient["what_ive_been_doing"]["kind"] == "doing"
    assert ambient["what_changed_since_last_time"]["source"] == "backup"
    assert ambient["proactive_checkin"] is not None


def test_presence_layer_highlights_warnings_as_watchpoints():
    state = build_presence_state(
        current_focus={"title": "Monitor runtime", "next_action": "Wait", "source": "maintenance"},
        recent_events=[
            {"message": "Primary server recovered", "source": "health_monitor", "level": "info"},
            {"message": "Backup restore requested", "source": "backup", "level": "warn"},
        ],
        capabilities={"status_line": "healthy"},
        initiative={"enabled": False, "queue_depth": 0},
        idle={"tier": "active"},
    )

    watch = state.ambient_payload()["what_im_keeping_an_eye_on"]
    checkin = state.ambient_payload()["proactive_checkin"]

    assert watch is not None
    assert "Backup restore requested" in watch["text"]
    assert checkin is not None
    assert "backup restore requested" in checkin["text"].lower()
