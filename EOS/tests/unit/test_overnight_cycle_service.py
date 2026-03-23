from __future__ import annotations

from datetime import datetime, timezone

from runtime.overnight_cycle import (
    DAY_ACTIVE,
    DEEP_NIGHT,
    EARLY_NIGHT,
    PREWAKE,
    OvernightCycleConfig,
    OvernightCycleService,
    current_phase,
)
from runtime.overnight_store import OvernightCycleStore


NOW = datetime(2026, 3, 23, 21, 0, tzinfo=timezone.utc)


class _TopologyStub:
    def primary_endpoint(self) -> str:
        return "http://127.0.0.1:18080"



def _service(tmp_path):
    cfg = {
        "overnight_cycle": {
            "enabled": True,
            "conversation_declare_enabled": True,
            "early_phase_hours": 2.0,
            "deep_phase_start_hours": 2.0,
            "prewake_lead_hours": 1.5,
            "allow_investigations_overnight": True,
            "allow_memory_maintenance_overnight": True,
            "allow_initiative_overnight": True,
            "cancel_on_live_return": True,
            "live_override_grace_minutes": 20.0,
            "llm_fallback_enabled": False,
        }
    }
    return OvernightCycleService(cfg, OvernightCycleStore(tmp_path / "overnight.db"))



def test_current_phase_transitions():
    config = OvernightCycleConfig()
    window = {
        "away_start_time": "2026-03-23T22:00:00Z",
        "expected_return_time": "2026-03-24T09:00:00Z",
    }

    assert current_phase(datetime(2026, 3, 23, 21, 30, tzinfo=timezone.utc), NOW, window, config) == DAY_ACTIVE
    assert current_phase(datetime(2026, 3, 23, 22, 45, tzinfo=timezone.utc), datetime(2026, 3, 23, 22, 0, tzinfo=timezone.utc), window, config) == EARLY_NIGHT
    assert current_phase(datetime(2026, 3, 24, 1, 30, tzinfo=timezone.utc), datetime(2026, 3, 23, 22, 0, tzinfo=timezone.utc), window, config) == DEEP_NIGHT
    assert current_phase(datetime(2026, 3, 24, 8, 0, tzinfo=timezone.utc), datetime(2026, 3, 23, 22, 0, tzinfo=timezone.utc), window, config) == PREWAKE
    assert current_phase(datetime(2026, 3, 24, 7, 15, tzinfo=timezone.utc), datetime(2026, 3, 24, 7, 10, tzinfo=timezone.utc), window, config) == DAY_ACTIVE



def test_service_declares_and_marks_live_return(tmp_path):
    service = _service(tmp_path)

    declared = service.handle_user_turn(
        "Probably getting off around 10 and back on about 8.",
        now=NOW,
        topology=_TopologyStub(),
    )
    assert declared["is_declaration"] is True
    status = declared["status"]
    assert status["current_window"]["status"] == "scheduled"

    live_return = service.handle_user_turn(
        "Actually I'm still here and need one more thing.",
        now=datetime(2026, 3, 23, 22, 45, tzinfo=timezone.utc),
        topology=_TopologyStub(),
    )
    assert live_return["live_return_detected"] is True
    assert live_return["record"]["status"] == "ended"
    assert live_return["status"]["status"] == "none"



def test_service_status_exposes_posture(tmp_path):
    service = _service(tmp_path)
    service.handle_user_turn(
        "I'm going to bed soon. I'll be back in the morning.",
        now=NOW,
        topology=_TopologyStub(),
    )
    service.note_interaction(now=datetime(2026, 3, 23, 21, 35, tzinfo=timezone.utc))

    status = service.get_status(now=datetime(2026, 3, 24, 1, 30, tzinfo=timezone.utc), include_history=True)

    assert status["phase"] == DEEP_NIGHT
    assert status["posture"]["allow_initiative"] is True
    assert status["posture"]["allow_memory_maintenance"] is True
    assert status["recent_history"]
