"""Tool: Web Search — DuckDuckGo (no API key required)."""
from __future__ import annotations
import httpx


async def search(query: str, max_results: int = 5) -> str:
    params = {
        "q": query, "format": "json",
        "no_html": "1", "skip_disambig": "1",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get("https://api.duckduckgo.com/", params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            return f"Search failed: {exc}"

    results = []
    if data.get("AbstractText"):
        results.append(f"Summary: {data['AbstractText']}\nSource: {data.get('AbstractURL', '')}")
    for topic in data.get("RelatedTopics", [])[:max_results]:
        if isinstance(topic, dict) and topic.get("Text"):
            results.append(f"• {topic['Text']}\n  {topic.get('FirstURL', '')}")

    return (
        f"Search results for '{query}':\n\n" + "\n\n".join(results)
        if results
        else f"No results found for: {query}"
    )
