"""System Command Tools — Allowlisted system commands"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Dict


def _jdump(x: Any) -> str:
    try:
        return json.dumps(x, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


def register(registry: Any, config: Dict[str, Any]) -> None:
    from runtime.tool_registry import ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy

    sys_cfg = config.get("system_cmd", {}) if isinstance(config, dict) else {}
    enabled = bool(sys_cfg.get("enabled", False))

    # Default allowlist
    allowlist = sys_cfg.get("allowlist", ["ipconfig", "netstat", "whoami", "hostname", "systeminfo"])
    if isinstance(allowlist, str):
        allowlist = [c.strip() for c in allowlist.split(",") if c.strip()]
    if not isinstance(allowlist, list):
        allowlist = ["ipconfig", "netstat", "whoami"]
    allowlist_norm = [c.lower().strip() for c in allowlist]

    def run_command_handler(params: Dict[str, Any]) -> str:
        if not enabled:
            return "System command tools disabled"
        cmd = str(params.get("cmd") or "").strip()
        if not cmd:
            return "Missing cmd"
        # Check against allowlist
        cmd_name = os.path.basename(cmd).lower().replace(".exe", "").replace(".cmd", "").replace(".bat", "")
        if cmd_name not in allowlist_norm:
            return f"Command not allowed: {cmd_name}"
        # Reject pipes/redirections
        if re.search(r"[|;&<>`\n\r]", cmd):
            return "Unsafe characters in command"
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            return _jdump({
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": (proc.stdout or "")[:4000],
                "stderr": (proc.stderr or "")[:4000],
            })
        except subprocess.TimeoutExpired:
            return _jdump({"ok": False, "error": "Command timeout"})
        except Exception as e:
            return _jdump({"ok": False, "error": str(e)})

    registry.register(ToolSpec(
        name="run_command",
        description="Run an allowlisted system command.",
        pack="system_cmd_tools",
        tags=["system", "commands"],
        parameters={"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
        handler=run_command_handler,
        risk_level=ToolRiskLevel.IRREVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.OPERATOR_ONLY,
        confirmation_policy=ConfirmationPolicy.HARD_CONFIRM,
        enabled=enabled,
    ))
