"""Package Tools — Python package management"""

from __future__ import annotations

import json
import subprocess
import sys
import platform
from typing import Any, Dict


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
    from runtime.tool_registry import ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy

    pkg_cfg = config.get("packages", {}) if isinstance(config, dict) else {}
    enabled = bool(pkg_cfg.get("enabled", True))

    def python_env_handler(params: Dict[str, Any]) -> str:
        return _jdump({
            "executable": sys.executable,
            "version": sys.version,
            "prefix": sys.prefix,
            "platform": platform.platform(),
        })

    registry.register(ToolSpec(
        name="python_env",
        description="Show Python runtime environment info.",
        pack="package_tools",
        tags=["python", "packages"],
        parameters={"type": "object", "properties": {}, "required": []},
        handler=python_env_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    def pip_version_handler(params: Dict[str, Any]) -> str:
        try:
            out = subprocess.check_output([sys.executable, "-m", "pip", "--version"], text=True, timeout=10).strip()
            return _jdump({"ok": True, "pip": out})
        except Exception as e:
            return _jdump({"ok": False, "error": str(e)})

    registry.register(ToolSpec(
        name="pip_version",
        description="Show pip version.",
        pack="package_tools",
        tags=["python", "packages"],
        parameters={"type": "object", "properties": {}, "required": []},
        handler=pip_version_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    def pip_freeze_handler(params: Dict[str, Any]) -> str:
        max_lines = _safe_int(params.get("max_lines"), 4000)
        max_lines = max(10, min(max_lines, 20000))
        try:
            out = subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True, timeout=10)
            lines = out.splitlines()
            truncated = len(lines) > max_lines
            if truncated:
                lines = lines[:max_lines]
            return _jdump({"ok": True, "count": len(lines), "truncated": truncated, "lines": lines})
        except Exception as e:
            return _jdump({"ok": False, "error": str(e)})

    registry.register(ToolSpec(
        name="pip_freeze",
        description="Return pip freeze output.",
        pack="package_tools",
        tags=["python", "packages"],
        parameters={"type": "object", "properties": {"max_lines": {"type": "integer"}}, "required": []},
        handler=pip_freeze_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))
