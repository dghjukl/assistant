"""Diff Tools — Unified diff and patch operations"""

from __future__ import annotations

import json
from typing import Any, Dict


def _jdump(x: Any) -> str:
    try:
        return json.dumps(x, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


def register(registry: Any, config: Dict[str, Any]) -> None:
    """Register diff tools into the registry."""
    from runtime.tool_registry import (
        ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy
    )

    enabled = config.get("diff", {}).get("enabled", True) if isinstance(config.get("diff"), dict) else True

    def unified_diff_handler(params: Dict[str, Any]) -> str:
        """Compute unified diff between two strings."""
        import difflib
        from_text = str(params.get("from_text") or "")
        to_text = str(params.get("to_text") or "")
        from_file = str(params.get("from_file") or "a")
        to_file = str(params.get("to_file") or "b")

        from_lines = from_text.splitlines(keepends=True)
        to_lines = to_text.splitlines(keepends=True)
        diff = list(difflib.unified_diff(from_lines, to_lines, fromfile=from_file, tofile=to_file))
        return _jdump({"diff": "".join(diff)})

    registry.register(ToolSpec(
        name="unified_diff",
        description="Compute unified diff between two strings.",
        pack="diff_tools",
        tags=["text"],
        parameters={
            "type": "object",
            "properties": {
                "from_text": {"type": "string"},
                "to_text": {"type": "string"},
                "from_file": {"type": "string"},
                "to_file": {"type": "string"},
            },
        },
        handler=unified_diff_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))
