"""Tool broker — narrows full tool registry to contextual shortlist.

Works with ToolRegistry to provide intelligent tool discovery based on
user input and context tags.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ToolBroker:
    """Limits tool registry to relevant shortlist for current turn.

    The broker prevents prompt bloat by:
    1. Filtering enabled tools only
    2. Filtering by domain tags relevant to current context
    3. Scoring by lexical overlap with user input
    4. Returning only name + brief description (no full schemas)

    Usage::

        from runtime.tool_registry import ToolRegistry
        from runtime.tool_broker import ToolBroker

        registry = ToolRegistry()
        # ... register tools ...

        broker = ToolBroker(registry=registry, max_shortlist=12)
        shortlist = broker.narrow(
            user_text="I need to read a file",
            context_tags=["files"],
        )
        # Returns: [{"name": "head_file", "description": "..."}, ...]
    """

    def __init__(self, registry: Any = None, max_shortlist: int = 12):
        """Initialize the broker with a ToolRegistry.

        Parameters
        ----------
        registry : ToolRegistry
            The ToolRegistry to pull tools from
        max_shortlist : int
            Maximum number of tools to return in a shortlist
        """
        self.registry = registry
        self.max_shortlist = max_shortlist

    def narrow(
        self,
        user_text: str,
        context_tags: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Return relevant tool shortlist based on user input and context.

        Parameters
        ----------
        user_text : str
            The user's input text to match against tool descriptions
        context_tags : list[str], optional
            Domain tags to filter by (e.g., ["files", "git"])

        Returns
        -------
        list[dict[str, Any]]
            List of dicts with 'name' and 'description' only (no schemas)
        """
        if self.registry is None:
            return []

        # Get all enabled tools
        all_tools = self.registry.all_enabled()
        if not all_tools:
            return []

        if context_tags is None:
            context_tags = []

        # Filter and score candidates
        candidates = []
        for tool_spec in all_tools:
            # Check if tags overlap with context
            if context_tags and not any(tag in tool_spec.tags for tag in context_tags):
                continue

            # Score by lexical overlap with user text
            text_lower = user_text.lower()
            desc_lower = tool_spec.description.lower()
            name_lower = tool_spec.name.lower()

            score = 0.0
            # Exact match in description or name
            if desc_lower in text_lower or name_lower in text_lower:
                score = 1.0
            else:
                # Word-level overlap
                user_words = set(text_lower.split())
                desc_words = set(desc_lower.split())
                name_words = set(name_lower.split())
                if user_words & (desc_words | name_words):
                    score = 0.5

            if score > 0:
                candidates.append((score, tool_spec))

        # Sort by score (descending) and truncate
        candidates.sort(key=lambda x: -x[0])
        shortlist = [spec for _, spec in candidates[: self.max_shortlist]]

        # Return name + description only (no full schemas)
        return [
            {
                "name": spec.name,
                "description": spec.description,
            }
            for spec in shortlist
        ]
