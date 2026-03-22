"""Service Control Tools — Windows service management"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from typing import Any, Dict, Tuple


def _jdump(x: Any) -> str:
    try:
        return json.dumps(x, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


def _is_windows() -> bool:
    return platform.system().lower() == "windows"


def _run_ps(cmd: str, timeout: float = 15.0) -> Tuple[bool, str]:
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            errors="replace",
        )
        return True, out.strip()
    except Exception as e:
        return False, str(e)


def register(registry: Any, config: Dict[str, Any]) -> None:
    from runtime.tool_registry import ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy

    sc_cfg = config.get("service_control", {}) if isinstance(config, dict) else {}
    enabled = bool(sc_cfg.get("enabled", False))

    def list_services_handler(params: Dict[str, Any]) -> str:
        if not _is_windows():
            return _jdump({"error": "Windows-only tool"})
        if not enabled:
            return _jdump({"error": "Service control disabled"})
        try:
            ok, out = _run_ps("Get-Service | Select-Object Name,Status,StartType | ConvertTo-Json -Depth 2")
            if ok:
                data = json.loads(out) if out else []
                if isinstance(data, dict):
                    data = [data]
                return _jdump({"ok": True, "count": len(data), "services": data})
            return _jdump({"ok": False, "error": out})
        except Exception as e:
            return _jdump({"ok": False, "error": str(e)})

    registry.register(ToolSpec(
        name="list_services",
        description="List Windows services.",
        pack="service_control_tools",
        tags=["system", "services"],
        parameters={"type": "object", "properties": {"name_contains": {"type": "string"}}, "required": []},
        handler=list_services_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))
