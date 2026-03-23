from core.attention_preferences import (
    CATEGORY_INTERACTION_STYLE,
    CATEGORY_MONITORED_TOPICS,
    CATEGORY_PREFERRED_PROJECTS,
    list_preferences,
    record_preference,
    record_turn_attention,
    summarize_attention_taste,
)
from core.memory import configure, init_db


def test_attention_preferences_persist_and_summarize(minimal_cfg):
    configure({
        **minimal_cfg,
        "retrieval": {
            "chroma_path": minimal_cfg["retrieval"]["chroma_path"],
            "embed_model": "models/embedding/all-MiniLM-L6-v2",
            "collection": "test_memory",
            "top_k": 3,
        },
    })
    init_db()

    record_preference(CATEGORY_PREFERRED_PROJECTS, "Release planning", strength=1.4, origin="test")
    record_preference(CATEGORY_MONITORED_TOPICS, "backup health", strength=1.1, origin="test")

    summary = summarize_attention_taste()

    assert summary["preferred_projects"][0]["topic"] == "Release planning"
    assert "backup health" in summary["watch_topics"]
    assert summary["initiative_bias"]["session_checkpoint"] > 0.2


def test_record_turn_attention_extracts_style_monitoring_and_focus(minimal_cfg):
    configure({
        **minimal_cfg,
        "retrieval": {
            "chroma_path": minimal_cfg["retrieval"]["chroma_path"],
            "embed_model": "models/embedding/all-MiniLM-L6-v2",
            "collection": "test_memory",
            "top_k": 3,
        },
    })
    init_db()

    recorded = record_turn_attention(
        user_text="Please be concise and keep an eye on deployment rollback risk.",
        current_focus={"title": "Finish deployment rollout", "source": "user_goal"},
    )

    style_topics = [item["topic"] for item in list_preferences(CATEGORY_INTERACTION_STYLE)]
    monitored_topics = [item["topic"] for item in list_preferences(CATEGORY_MONITORED_TOPICS)]
    project_topics = [item["topic"] for item in list_preferences(CATEGORY_PREFERRED_PROJECTS)]

    assert "concise" in style_topics
    assert any("deployment rollback risk" in topic for topic in monitored_topics)
    assert "Finish deployment rollout" in project_topics
    assert "concise" in recorded[CATEGORY_INTERACTION_STYLE]
