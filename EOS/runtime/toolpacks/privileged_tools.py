"""Privileged Tools — High-risk system operations"""

from __future__ import annotations

import json
from typing import Any, Dict


def _jdump(x: Any) -> str:
    try:
        return json.dumps(x, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


def register(registry: Any, config: Dict[str, Any]) -> None:
    from runtime.tool_registry import ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy

    cfg = config.get("privileged_tools", {}) if isinstance(config, dict) else {}
    enabled = bool(cfg.get("enabled", False))

    def placeholder_handler(params: Dict[str, Any]) -> str:
        return _jdump({"error": "Privileged tools disabled by default", "hint": "Enable in config.privileged_tools.enabled"})

    # Register placeholder tools - disabled by default
    registry.register(ToolSpec(
        name="placeholder_privileged",
        description="Placeholder for privileged operations (disabled by default).",
        pack="privileged_tools",
        tags=["privileged", "system"],
        parameters={"type": "object", "properties": {}, "required": []},
        handler=placeholder_handler,
        risk_level=ToolRiskLevel.IRREVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.OPERATOR_ONLY,
        confirmation_policy=ConfirmationPolicy.HARD_CONFIRM,
        enabled=False,
    ))
