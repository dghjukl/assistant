"""
EOS — Retrieval Service
Thin wrapper around core/memory.py vector store.
"""
from __future__ import annotations

from core.memory import store_memory, search_memory


def remember(text: str, source: str = "conversation", **meta) -> str:
    """Store a text chunk in long-term memory with optional metadata overrides."""
    return store_memory(
        text,
        source=source,
        salience_score=meta.get("salience_score", 0.5),
        emotional_weight=meta.get("emotional_weight", 0.3),
        tags=meta.get("tags", ""),
    )


def recall(query: str, top_k: int | None = None) -> list[dict]:
    """Search long-term memory for relevant passages."""
    return search_memory(query, top_k=top_k)


def recall_as_context(query: str, top_k: int | None = None) -> str:
    """Return retrieved memories formatted as a context block."""
    results = recall(query, top_k)
    if not results:
        return ""
    lines = ["Relevant memories:"]
    for r in results:
        lines.append(f"  - {r['text']}")
    return "\n".join(lines)
