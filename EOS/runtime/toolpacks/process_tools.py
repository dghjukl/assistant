"""Process Tools — List and control processes

Configuration:
  process:
    enabled: false
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, List


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

    proc_cfg = config.get("process", {}) if isinstance(config, dict) else {}
    enabled = bool(proc_cfg.get("enabled", False))

    def list_processes_handler(params: Dict[str, Any]) -> str:
        max_results = _safe_int(params.get("max_results"), 50)
        max_results = max(1, min(max_results, 500))
        name_filter = str(params.get("name_contains") or "").strip().lower()
        procs: List[Dict[str, Any]] = []
        try:
            if os.name == "nt":
                out = subprocess.check_output(["tasklist", "/FO", "CSV", "/NH"], text=True, errors="replace")
                for line in out.splitlines():
                    line = line.strip()
                    if not line or line.count('","') < 3:
                        continue
                    parts = [p.strip().strip('"') for p in line.split('","')]
                    if len(parts) < 2:
                        continue
                    img = parts[0]
                    pid = parts[1]
                    if name_filter and name_filter not in img.lower():
                        continue
                    procs.append({"name": img, "pid": pid})
                    if len(procs) >= max_results:
                        break
            else:
                out = subprocess.check_output(["ps", "-eo", "pid,comm"], text=True, errors="replace")
                for line in out.splitlines()[1:]:
                    line = line.strip()
                    if not line:
                        continue
                    pid, comm = line.split(None, 1)
                    if name_filter and name_filter not in comm.lower():
                        continue
                    procs.append({"name": comm, "pid": pid})
                    if len(procs) >= max_results:
                        break
        except Exception as e:
            return _jdump({"ok": False, "error": str(e)})
        return _jdump({"ok": True, "count": len(procs), "processes": procs})

    registry.register(ToolSpec(
        name="list_processes",
        description="List running processes.",
        pack="process_tools",
        tags=["system", "processes"],
        parameters={"type": "object", "properties": {"name_contains": {"type": "string"}, "max_results": {"type": "integer"}}, "required": []},
        handler=list_processes_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=True,
    ))
