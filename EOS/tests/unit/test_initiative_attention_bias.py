from runtime.initiative_engine import InitiativeEngine


def test_initiative_engine_prefers_attention_biased_candidate():
    engine = InitiativeEngine({"initiative": {"idle_threshold_seconds": 0}})
    engine._turn_count = 5

    result = engine.evaluate(
        memory_retrieved_count=3,
        attention_summary={
            "initiative_bias": {
                "session_checkpoint": 0.9,
                "self_reflection": 0.1,
                "memory_consolidation": 0.1,
            }
        },
    )

    assert result["selected"] is not None
    assert result["selected"]["initiative_type"] == "session_checkpoint"
