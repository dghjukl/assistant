"""Git Tools — Version control operations

Provides tools for:
- Status, log, diff, show
- Add, commit, push, pull
- Branch and remote operations
- Safe repo initialization

Configuration:
  git_tools:
    enabled: true
    allow_write: false             # Set true to enable add/commit
    repo_root: ""                  # Defaults to project_root
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


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
    """Register git tools into the registry."""
    from runtime.tool_registry import (
        ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy
    )

    git_cfg = config.get("git", {}) if isinstance(config, dict) else {}
    enabled = bool(git_cfg.get("enabled", True))
    allow_write = bool(git_cfg.get("allow_write", False))

    project_root = str(config.get("project_root", "."))
    repo_root = str(git_cfg.get("repo_root") or project_root).strip() or project_root

    def _run_git(args_list: List[str], cwd: str, timeout: int = 20) -> Dict[str, Any]:
        """Core git command execution."""
        try:
            p = subprocess.run(
                ["git", *args_list],
                cwd=cwd,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
            )
            out = (p.stdout or "").strip()
            err = (p.stderr or "").strip()
            combined = out + (("\n" + err) if err else "")
            return {
                "ok": (p.returncode == 0),
                "returncode": p.returncode,
                "output": combined,
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timeout"}
        except FileNotFoundError:
            return {"ok": False, "error": "git not found on PATH"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # git_status
    def git_status_handler(params: Dict[str, Any]) -> str:
        r = _run_git(["status", "--porcelain"], repo_root)
        if not r.get("ok"):
            return _jdump({"error": r.get("error", "unknown error")})
        return _jdump({"status": r.get("output")})

    registry.register(ToolSpec(
        name="git_status",
        description="Show git repository status.",
        pack="git_tools",
        tags=["git"],
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=git_status_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    # git_log
    def git_log_handler(params: Dict[str, Any]) -> str:
        lines = _safe_int(params.get("lines"), 20)
        lines = max(1, min(lines, 100))
        r = _run_git(["log", f"--oneline", f"-{lines}"], repo_root)
        if not r.get("ok"):
            return _jdump({"error": r.get("error", "unknown error")})
        return _jdump({"log": r.get("output")})

    registry.register(ToolSpec(
        name="git_log",
        description="Show recent git commit log.",
        pack="git_tools",
        tags=["git"],
        parameters={
            "type": "object",
            "properties": {"lines": {"type": "integer"}},
        },
        handler=git_log_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    # git_diff
    def git_diff_handler(params: Dict[str, Any]) -> str:
        target = str(params.get("target") or "HEAD").strip() or "HEAD"
        r = _run_git(["diff", target], repo_root)
        if not r.get("ok"):
            return _jdump({"error": r.get("error", "unknown error")})
        output = r.get("output", "")[:10000]
        return _jdump({"diff": output})

    registry.register(ToolSpec(
        name="git_diff",
        description="Show git diff.",
        pack="git_tools",
        tags=["git"],
        parameters={
            "type": "object",
            "properties": {"target": {"type": "string"}},
        },
        handler=git_diff_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    # git_add
    def git_add_handler(params: Dict[str, Any]) -> str:
        if not allow_write:
            return _jdump({"error": "git write ops disabled (git.allow_write)"})
        pattern = str(params.get("pattern") or ".").strip() or "."
        r = _run_git(["add", pattern], repo_root)
        if not r.get("ok"):
            return _jdump({"error": r.get("error", "unknown error")})
        return _jdump({"added": pattern})

    registry.register(ToolSpec(
        name="git_add",
        description="Stage files for commit.",
        pack="git_tools",
        tags=["git"],
        parameters={
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
        },
        handler=git_add_handler,
        risk_level=ToolRiskLevel.DRAFT,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.SOFT_CONFIRM,
        enabled=enabled and allow_write,
    ))

    # git_commit
    def git_commit_handler(params: Dict[str, Any]) -> str:
        if not allow_write:
            return _jdump({"error": "git write ops disabled (git.allow_write)"})
        message = str(params.get("message") or "").strip()
        if not message:
            return _jdump({"error": "message is required"})
        r = _run_git(["commit", "-m", message], repo_root)
        if not r.get("ok"):
            return _jdump({"error": r.get("error", "unknown error")})
        return _jdump({"committed": True, "output": r.get("output")})

    registry.register(ToolSpec(
        name="git_commit",
        description="Commit staged changes.",
        pack="git_tools",
        tags=["git"],
        parameters={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
        handler=git_commit_handler,
        risk_level=ToolRiskLevel.REVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.SOFT_CONFIRM,
        enabled=enabled and allow_write,
    ))
