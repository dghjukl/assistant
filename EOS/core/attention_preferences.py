"""
EOS — Durable Attention / Preference Store
Persists small behavioral preferences and recurring salience signals separately
from raw interaction memory.

The store is intentionally lightweight and deterministic.  It tracks durable
signals that should keep mattering across sessions, such as:
  - preferred_projects
  - recurring_concerns
  - favored_interaction_style
  - monitored_topics
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from core.memory import get_db


CATEGORY_PREFERRED_PROJECTS = "preferred_projects"
CATEGORY_RECURRING_CONCERNS = "recurring_concerns"
CATEGORY_INTERACTION_STYLE = "favored_interaction_style"
CATEGORY_MONITORED_TOPICS = "monitored_topics"

ALL_CATEGORIES: tuple[str, ...] = (
    CATEGORY_PREFERRED_PROJECTS,
    CATEGORY_RECURRING_CONCERNS,
    CATEGORY_INTERACTION_STYLE,
    CATEGORY_MONITORED_TOPICS,
)

_STYLE_PATTERNS: dict[str, tuple[str, ...]] = {
    "concise": ("be concise", "keep it concise", "brief", "short answer", "keep it short"),
    "detailed": ("detailed", "thorough", "in depth", "deep dive"),
    "step_by_step": ("step by step", "walk me through", "one step at a time"),
    "bullet_points": ("bullet points", "bullets", "list it out"),
    "direct": ("be direct", "straight answer", "straightforward", "don't sugarcoat"),
    "proactive": ("check in", "follow up", "keep me posted", "keep an eye on"),
}

_MONITOR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:keep an eye on|monitor|watch|track|check on|keep tabs on)\s+([^.;,\n]+)", re.I),
    re.compile(r"(?:follow up on|remind me about)\s+([^.;,\n]+)", re.I),
)

_CONCERN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:worried about|concerned about|keep in mind|important to me(?: is)?|don't let me forget)\s+([^.;,\n]+)", re.I),
)

_GENERIC_TOPICS = {
    "it",
    "this",
    "that",
    "things",
    "stuff",
    "everything",
    "anything",
    "something",
    "what's been happening",
    "the current thread",
}


def _ensure_schema() -> None:
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS attention_preferences (
                category      TEXT NOT NULL,
                topic         TEXT NOT NULL,
                score         REAL NOT NULL DEFAULT 0.0,
                note          TEXT NOT NULL DEFAULT '',
                origin        TEXT NOT NULL DEFAULT '',
                first_seen_at REAL NOT NULL,
                last_seen_at  REAL NOT NULL,
                updated_at    REAL NOT NULL,
                metadata      TEXT,
                PRIMARY KEY (category, topic)
            )
            """
        )
        conn.commit()


def _normalize_topic(value: Any) -> str:
    topic = re.sub(r"\s+", " ", str(value or "").strip().strip("'\""))
    topic = topic.rstrip(".!?;,")
    return topic[:140]


def _should_track_topic(topic: str) -> bool:
    lowered = topic.lower().strip()
    return bool(lowered) and lowered not in _GENERIC_TOPICS and len(lowered) >= 3


def record_preference(
    category: str,
    topic: str,
    *,
    strength: float = 1.0,
    note: str = "",
    origin: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Insert or reinforce a durable preference/salience signal."""
    if category not in ALL_CATEGORIES:
        return

    normalized = _normalize_topic(topic)
    if not _should_track_topic(normalized):
        return

    _ensure_schema()
    now = time.time()
    serialized = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)

    with get_db() as conn:
        row = conn.execute(
            "SELECT score, note, origin, first_seen_at, metadata FROM attention_preferences "
            "WHERE category=? AND topic=?",
            (category, normalized),
        ).fetchone()

        if row:
            existing_note = str(row["note"] or "")
            existing_origin = str(row["origin"] or "")
            existing_meta = str(row["metadata"] or "")
            score = min(float(row["score"] or 0.0) + max(strength, 0.1), 5.0)
            conn.execute(
                "UPDATE attention_preferences "
                "SET score=?, note=?, origin=?, last_seen_at=?, updated_at=?, metadata=? "
                "WHERE category=? AND topic=?",
                (
                    score,
                    note or existing_note,
                    origin or existing_origin,
                    now,
                    now,
                    serialized if metadata else existing_meta,
                    category,
                    normalized,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO attention_preferences "
                "(category, topic, score, note, origin, first_seen_at, last_seen_at, updated_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    category,
                    normalized,
                    max(strength, 0.1),
                    note,
                    origin,
                    now,
                    now,
                    now,
                    serialized,
                ),
            )
        conn.commit()


def list_preferences(category: str, *, limit: int = 3) -> list[dict[str, Any]]:
    _ensure_schema()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT category, topic, score, note, origin, first_seen_at, last_seen_at, updated_at, metadata "
            "FROM attention_preferences WHERE category=? "
            "ORDER BY score DESC, last_seen_at DESC LIMIT ?",
            (category, max(limit, 1)),
        ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        meta_raw = row["metadata"]
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except Exception:
            meta = {}
        items.append({
            "category": row["category"],
            "topic": row["topic"],
            "score": float(row["score"] or 0.0),
            "note": row["note"] or "",
            "origin": row["origin"] or "",
            "first_seen_at": float(row["first_seen_at"] or 0.0),
            "last_seen_at": float(row["last_seen_at"] or 0.0),
            "updated_at": float(row["updated_at"] or 0.0),
            "metadata": meta,
        })
    return items


def summarize_attention_taste(*, limit_per_category: int = 3) -> dict[str, Any]:
    """Return a compact runtime summary of durable attention/taste signals."""
    categories = {
        category: list_preferences(category, limit=limit_per_category)
        for category in ALL_CATEGORIES
    }

    projects = categories[CATEGORY_PREFERRED_PROJECTS]
    concerns = categories[CATEGORY_RECURRING_CONCERNS]
    styles = categories[CATEGORY_INTERACTION_STYLE]
    monitored = categories[CATEGORY_MONITORED_TOPICS]

    watch_topics: list[str] = []
    for item in monitored + concerns:
        topic = str(item.get("topic") or "").strip()
        if topic and topic not in watch_topics:
            watch_topics.append(topic)

    compact_parts: list[str] = []
    if projects:
        compact_parts.append("projects=" + ", ".join(item["topic"] for item in projects[:2]))
    if concerns:
        compact_parts.append("concerns=" + ", ".join(item["topic"] for item in concerns[:2]))
    if styles:
        compact_parts.append("style=" + ", ".join(item["topic"] for item in styles[:2]))
    if monitored:
        compact_parts.append("watch=" + ", ".join(item["topic"] for item in monitored[:2]))

    style_topics = [str(item.get("topic") or "") for item in styles]
    initiative_bias = {
        "session_checkpoint": 0.2 + (0.4 if projects or watch_topics else 0.0),
        "self_reflection": 0.2 + (0.2 if "detailed" in style_topics else 0.0),
        "memory_consolidation": 0.2 + (0.3 if any("memory" in topic.lower() for topic in watch_topics) else 0.0),
        "identity_probe": 0.2 + (0.2 if any("identity" in topic.lower() for topic in watch_topics) else 0.0),
    }

    return {
        "categories": categories,
        "preferred_projects": projects,
        "recurring_concerns": concerns,
        "favored_interaction_style": styles,
        "monitored_topics": monitored,
        "watch_topics": watch_topics,
        "compact": " | ".join(compact_parts) if compact_parts else "no durable attention preferences recorded yet",
        "status_line": " · ".join(compact_parts[:3]) if compact_parts else "no durable attention preferences recorded yet",
        "initiative_bias": initiative_bias,
    }


def build_attention_taste_block(summary: dict[str, Any] | None = None) -> str:
    summary = summary or summarize_attention_taste()

    def _topics(key: str) -> str:
        items = summary.get(key, [])
        if not items:
            return "none recorded"
        return ", ".join(str(item.get("topic") or "") for item in items[:3])

    return "\n".join([
        "## Durable Attention / Taste",
        f"- Preferred projects: {_topics('preferred_projects')}",
        f"- Recurring concerns: {_topics('recurring_concerns')}",
        f"- Favored interaction style: {_topics('favored_interaction_style')}",
        f"- Monitor automatically: {_topics('monitored_topics')}",
    ])


def _extract_topics(text: str, patterns: tuple[re.Pattern[str], ...]) -> list[str]:
    extracted: list[str] = []
    for pattern in patterns:
        for match in pattern.findall(text):
            topic = _normalize_topic(match)
            if _should_track_topic(topic) and topic not in extracted:
                extracted.append(topic)
    return extracted


def record_turn_attention(
    *,
    user_text: str,
    assistant_text: str = "",
    current_focus: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    """
    Best-effort heuristic extraction of durable preference signals from a turn.

    The output is returned for observability/tests and persisted immediately.
    """
    current_focus = dict(current_focus or {})
    user_text = str(user_text or "")
    assistant_text = str(assistant_text or "")
    lower = user_text.lower()

    recorded: dict[str, list[str]] = {category: [] for category in ALL_CATEGORIES}

    focus_title = _normalize_topic(current_focus.get("title") or "")
    focus_source = str(current_focus.get("source") or "")
    if focus_title and focus_source in {"user_goal", "initiative", "investigation"}:
        record_preference(
            CATEGORY_PREFERRED_PROJECTS,
            focus_title,
            strength=0.75,
            note="Observed as a recurring active focus.",
            origin=f"focus:{focus_source}",
            metadata={"source": focus_source},
        )
        recorded[CATEGORY_PREFERRED_PROJECTS].append(focus_title)

    for style, phrases in _STYLE_PATTERNS.items():
        if any(phrase in lower for phrase in phrases):
            record_preference(
                CATEGORY_INTERACTION_STYLE,
                style,
                strength=0.9,
                note="Observed from an explicit interaction-style instruction.",
                origin="turn.style",
                metadata={"matched_in": "user_text"},
            )
            recorded[CATEGORY_INTERACTION_STYLE].append(style)

    monitor_topics = _extract_topics(user_text, _MONITOR_PATTERNS)
    concern_topics = _extract_topics(user_text, _CONCERN_PATTERNS)

    for topic in monitor_topics:
        record_preference(
            CATEGORY_MONITORED_TOPICS,
            topic,
            strength=1.0,
            note="User asked for ongoing monitoring or follow-up.",
            origin="turn.monitor",
        )
        recorded[CATEGORY_MONITORED_TOPICS].append(topic)

    for topic in concern_topics:
        record_preference(
            CATEGORY_RECURRING_CONCERNS,
            topic,
            strength=0.9,
            note="User framed this as an ongoing concern or durable priority.",
            origin="turn.concern",
        )
        recorded[CATEGORY_RECURRING_CONCERNS].append(topic)

    if "preferred project" in lower or "favorite project" in lower:
        project = focus_title or _normalize_topic(user_text)
        if _should_track_topic(project):
            record_preference(
                CATEGORY_PREFERRED_PROJECTS,
                project,
                strength=0.8,
                note="User explicitly framed this as a preferred project.",
                origin="turn.project",
            )
            recorded[CATEGORY_PREFERRED_PROJECTS].append(project)

    if "keep me posted" in lower or "let me know if" in lower:
        topic = focus_title or _normalize_topic(assistant_text[:120] or user_text[:120])
        if _should_track_topic(topic):
            record_preference(
                CATEGORY_MONITORED_TOPICS,
                topic,
                strength=0.7,
                note="User indicated they want future updates.",
                origin="turn.followup",
            )
            recorded[CATEGORY_MONITORED_TOPICS].append(topic)

    for category in recorded:
        deduped: list[str] = []
        for topic in recorded[category]:
            if topic not in deduped:
                deduped.append(topic)
        recorded[category] = deduped

    return recorded
