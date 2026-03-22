"""Tool: Memory Query — Search and store long-term memories."""
from __future__ import annotations
from services.retrieval import recall_as_context, remember


async def query_memory(question: str, top_k: int = 5) -> str:
    return recall_as_context(question, top_k) or "No relevant memories found."


async def save_memory(text: str, source: str = "explicit") -> str:
    remember(text, source=source)
    return f"Stored in memory: {text[:80]}..."
