"""Web Tools — Web search and fetch operations"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional


def _jdump(x: Any) -> str:
    try:
        return json.dumps(x, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


def _safe_int(x: Any, default: int) -> int:
    try:
        return int(x)
    except Exception:
        return default


def register(registry: Any, config: Dict[str, Any]) -> None:
    """Register web tools."""
    from runtime.tool_registry import (
        ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy
    )

    web_cfg = config.get("web_tools", {}) if isinstance(config, dict) else {}
    enabled = bool(web_cfg.get("enabled", True))
    max_results = _safe_int(web_cfg.get("max_results", 5), 5)

    # Try to import optional dependencies
    try:
        from duckduckgo_search import DDGS
        search_available = True
    except Exception:
        search_available = False

    try:
        import httpx
        fetch_available = True
    except Exception:
        fetch_available = False

    def web_search_handler(params: Dict[str, Any]) -> str:
        if not search_available:
            return _jdump({"error": "duckduckgo_search not installed"})

        query = str(params.get("query") or "").strip()
        if not query:
            return _jdump({"error": "query is required"})

        num_results = _safe_int(params.get("max_results"), max_results)
        num_results = max(1, min(num_results, max_results))

        try:
            from duckduckgo_search import DDGS
            results = []
            with DDGS() as ddgs:
                for result in ddgs.text(query, max_results=num_results):
                    results.append({
                        "title": result.get("title", ""),
                        "url": result.get("href", ""),
                        "snippet": result.get("body", "")[:500],
                    })
            return _jdump({"query": query, "results": results})
        except Exception as e:
            return _jdump({"error": str(e)})

    registry.register(ToolSpec(
        name="web_search",
        description="Search the web using DuckDuckGo.",
        pack="web_tools",
        tags=["web"],
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        },
        handler=web_search_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled and search_available,
    ))

    def web_fetch_handler(params: Dict[str, Any]) -> str:
        if not fetch_available:
            return _jdump({"error": "httpx not installed"})

        url = str(params.get("url") or "").strip()
        if not url:
            return _jdump({"error": "url is required"})

        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        try:
            import httpx
            response = httpx.get(url, timeout=15, follow_redirects=True)
            response.raise_for_status()

            content_type = response.headers.get("content-type", "").lower()
            if "text/" not in content_type and "application/json" not in content_type:
                return _jdump({"url": url, "error": f"non-text content-type: {content_type[:50]}"})

            text = response.text
            # Basic HTML stripping
            text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<[^>]{1,200}>", " ", text)
            text = re.sub(r"[ \t]{2,}", " ", text)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()

            # Limit output
            max_chars = _safe_int(params.get("max_chars"), 12000)
            max_chars = max(1000, min(max_chars, 50000))
            truncated = len(text) > max_chars
            text = text[:max_chars]

            return _jdump({
                "url": url,
                "status_code": response.status_code,
                "content": text,
                "truncated": truncated,
            })
        except Exception as e:
            return _jdump({"url": url, "error": str(e)})

    registry.register(ToolSpec(
        name="web_fetch",
        description="Fetch and read text content from a URL.",
        pack="web_tools",
        tags=["web"],
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_chars": {"type": "integer"},
            },
            "required": ["url"],
        },
        handler=web_fetch_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled and fetch_available,
    ))
