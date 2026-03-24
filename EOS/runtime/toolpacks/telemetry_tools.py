"""Telemetry Tools — System health and metrics

Configuration:
  telemetry:
    enabled: true
"""

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

    cfg_tel = config.get("telemetry", {}) if isinstance(config, dict) else {}
    enabled = bool(cfg_tel.get("enabled", True))

    def get_system_health_handler(params: Dict[str, Any]) -> str:
        """Return system health metrics."""
        try:
            import psutil
        except Exception:
            psutil = None

        health = {
            "timestamp": str(__import__("datetime").datetime.now().__import__("datetime").timezone.utc),
            "cpu_percent": None,
            "memory_percent": None,
            "disk_usage": None,
        }

        try:
            if psutil:
                health["cpu_percent"] = psutil.cpu_percent(interval=0.1)
                health["memory_percent"] = psutil.virtual_memory().percent
                health["disk_usage"] = dict(psutil.disk_usage("/")._asdict())
        except Exception:
            pass

        return _jdump(health)

    registry.register(ToolSpec(
        name="get_system_health",
        description="Return system CPU, memory, and disk usage.",
        pack="telemetry_tools",
        tags=["system", "telemetry"],
        parameters={"type": "object", "properties": {}, "required": []},
        handler=get_system_health_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))
