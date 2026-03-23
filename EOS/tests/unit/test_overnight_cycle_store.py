from __future__ import annotations

from runtime.overnight_store import OvernightCycleStore



def test_store_lifecycle_supersede_cancel_and_return(tmp_path):
    store = OvernightCycleStore(tmp_path / "entity_state.db")

    first = store.create_declaration(
        away_start_time="2026-03-23T22:00:00Z",
        expected_return_time="2026-03-24T09:00:00Z",
        confidence=0.9,
        source="conversation",
        source_text="heading off",
        parser_details={"example": 1},
    )
    assert store.fetch_current()["id"] == first["id"]

    second = store.create_declaration(
        away_start_time="2026-03-24T00:00:00Z",
        expected_return_time="2026-03-24T10:00:00Z",
        confidence=0.8,
        source="conversation",
        source_text="sleeping in",
    )
    assert store.fetch_current()["id"] == second["id"]
    assert store.get(first["id"])["status"] == "superseded"

    updated = store.update_expected_return_time(second["id"], "2026-03-24T10:30:00Z")
    assert updated["expected_return_time"] == "2026-03-24T10:30:00Z"

    cancelled = store.cancel_current(cancelled_at="2026-03-23T22:30:00Z")
    assert cancelled["status"] == "cancelled"

    third = store.create_declaration(
        away_start_time="2026-03-24T01:00:00Z",
        expected_return_time="2026-03-24T08:30:00Z",
        confidence=0.7,
        source="conversation",
        source_text="bed",
    )
    ended = store.mark_return(third["id"], actual_return_time="2026-03-24T07:15:00Z")
    assert ended["status"] == "ended"
    assert ended["actual_return_time"] == "2026-03-24T07:15:00Z"

    history = store.recent_history(limit=5)
    assert len(history) == 3
