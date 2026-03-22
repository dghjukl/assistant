"""Workspace Tools — Sandboxed file operations within workspace root

All operations are confined to the workspace root directory. Paths must resolve
within the workspace boundary; escape attempts are blocked.

Configuration:
  workspace_tools:
    enabled: true
    workspace_root: "data/workspace"    # Relative to project_root
    allow_delete: false                 # Enable deletion operations
    allow_exec: false                   # Enable script execution
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
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
    """Register workspace tools into the registry."""
    from runtime.tool_registry import (
        ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy
    )

    ws_cfg = config.get("workspace_tools", {}) if isinstance(config, dict) else {}
    enabled = bool(ws_cfg.get("enabled", True))
    allow_delete = bool(ws_cfg.get("allow_delete", False))
    allow_exec = bool(ws_cfg.get("allow_exec", False))

    project_root = Path(config.get("project_root", ".")).resolve()
    workspace_root_str = ws_cfg.get("workspace_root", "data/workspace")
    workspace_root = (project_root / workspace_root_str).resolve()

    def _confine(p: Path) -> Optional[Tuple[Path, Optional[str]]]:
        """Resolve and confine a path to workspace root. Returns (resolved_path, error)."""
        try:
            if not p.is_absolute():
                p = workspace_root / p
            p = p.resolve()
            # Check confinement
            try:
                p.relative_to(workspace_root)
                return p, None
            except ValueError:
                return None, f"Path escape attempt: {p} is outside workspace"
        except Exception as e:
            return None, str(e)

    # ── Read operations ────────────────────────────────────────────────────

    def workspace_list_handler(params: Dict[str, Any]) -> str:
        path_str = str(params.get("path") or "").strip() or "."
        depth = _safe_int(params.get("depth"), 2)
        depth = max(0, min(depth, 10))
        max_entries = _safe_int(params.get("max_entries"), 500)
        max_entries = max(1, min(max_entries, 5000))

        resolved = _confine(Path(path_str))
        if resolved[1]:
            return _jdump({"error": resolved[1]})
        target = resolved[0]

        if not target.exists():
            return _jdump({"path": str(target.relative_to(workspace_root)), "exists": False, "entries": []})

        entries: List[Dict[str, Any]] = []
        base_parts = len(target.parts)

        for dirpath, dirnames, filenames in os.walk(target):
            dp = Path(dirpath)
            current_depth = len(dp.parts) - base_parts
            if current_depth > depth:
                dirnames[:] = []
                continue

            for fn in sorted(filenames):
                fp = dp / fn
                try:
                    st = fp.stat()
                    entries.append({
                        "type": "file",
                        "path": str(fp.relative_to(workspace_root)),
                        "size_bytes": st.st_size,
                        "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                    })
                except OSError:
                    entries.append({"type": "file", "path": str(fp.relative_to(workspace_root)), "error": "stat failed"})

                if len(entries) >= max_entries:
                    break

            for dn in sorted(dirnames):
                dp2 = dp / dn
                entries.append({
                    "type": "directory",
                    "path": str(dp2.relative_to(workspace_root)),
                })

            if len(entries) >= max_entries:
                break

        return _jdump({
            "workspace_root": str(workspace_root),
            "path": str(target.relative_to(workspace_root)),
            "exists": True,
            "entries": entries,
        })

    registry.register(ToolSpec(
        name="workspace_list",
        description="List files and directories in workspace.",
        pack="workspace_tools",
        tags=["files"],
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "depth": {"type": "integer"},
                "max_entries": {"type": "integer"},
            },
        },
        handler=workspace_list_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    def workspace_read_handler(params: Dict[str, Any]) -> str:
        path_str = str(params.get("path") or "").strip()
        if not path_str:
            return _jdump({"error": "path is required"})

        resolved = _confine(Path(path_str))
        if resolved[1]:
            return _jdump({"error": resolved[1]})
        target = resolved[0]

        if not target.exists():
            return _jdump({"error": f"Not found: {target.relative_to(workspace_root)}"})
        if not target.is_file():
            return _jdump({"error": f"Not a file: {target.relative_to(workspace_root)}"})

        try:
            text = target.read_text(encoding="utf-8", errors="replace")
            lines = _safe_int(params.get("lines"), -1)
            if lines > 0:
                text = "\n".join(text.splitlines()[:lines])
            return _jdump({
                "path": str(target.relative_to(workspace_root)),
                "size_bytes": len(text.encode("utf-8")),
                "content": text,
            })
        except Exception as e:
            return _jdump({"error": str(e)})

    registry.register(ToolSpec(
        name="workspace_read",
        description="Read a file from workspace.",
        pack="workspace_tools",
        tags=["files"],
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "lines": {"type": "integer"},
            },
            "required": ["path"],
        },
        handler=workspace_read_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    # ── Write operations ───────────────────────────────────────────────────

    def workspace_write_handler(params: Dict[str, Any]) -> str:
        path_str = str(params.get("path") or "").strip()
        content = str(params.get("content") or "")

        if not path_str:
            return _jdump({"error": "path is required"})

        resolved = _confine(Path(path_str))
        if resolved[1]:
            return _jdump({"error": resolved[1]})
        target = resolved[0]

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return _jdump({
                "path": str(target.relative_to(workspace_root)),
                "size_bytes": len(content.encode("utf-8")),
                "written": True,
            })
        except Exception as e:
            return _jdump({"error": str(e)})

    registry.register(ToolSpec(
        name="workspace_write",
        description="Write or create a file in workspace.",
        pack="workspace_tools",
        tags=["files"],
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        handler=workspace_write_handler,
        risk_level=ToolRiskLevel.REVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.SOFT_CONFIRM,
        enabled=enabled,
    ))

    def workspace_stat_handler(params: Dict[str, Any]) -> str:
        path_str = str(params.get("path") or "").strip()
        if not path_str:
            return _jdump({"error": "path is required"})

        resolved = _confine(Path(path_str))
        if resolved[1]:
            return _jdump({"error": resolved[1]})
        target = resolved[0]

        if not target.exists():
            return _jdump({
                "path": str(target.relative_to(workspace_root)),
                "exists": False,
            })

        try:
            st = target.stat()
            return _jdump({
                "path": str(target.relative_to(workspace_root)),
                "exists": True,
                "is_file": target.is_file(),
                "is_dir": target.is_dir(),
                "size_bytes": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            })
        except Exception as e:
            return _jdump({"error": str(e)})

    registry.register(ToolSpec(
        name="workspace_stat",
        description="Get file/directory info in workspace.",
        pack="workspace_tools",
        tags=["files"],
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        handler=workspace_stat_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    # ── Delete operations ──────────────────────────────────────────────────

    def workspace_delete_handler(params: Dict[str, Any]) -> str:
        if not allow_delete:
            return _jdump({"error": "Deletion disabled (workspace.allow_delete=false)"})

        path_str = str(params.get("path") or "").strip()
        if not path_str:
            return _jdump({"error": "path is required"})

        resolved = _confine(Path(path_str))
        if resolved[1]:
            return _jdump({"error": resolved[1]})
        target = resolved[0]

        if not target.exists():
            return _jdump({"error": f"Not found: {target.relative_to(workspace_root)}"})

        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            return _jdump({
                "path": str(target.relative_to(workspace_root)),
                "deleted": True,
            })
        except Exception as e:
            return _jdump({"error": str(e)})

    registry.register(ToolSpec(
        name="workspace_delete",
        description="Delete a file or directory in workspace.",
        pack="workspace_tools",
        tags=["files"],
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        handler=workspace_delete_handler,
        risk_level=ToolRiskLevel.IRREVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.OPERATOR_ONLY,
        confirmation_policy=ConfirmationPolicy.HARD_CONFIRM,
        enabled=enabled and allow_delete,
    ))
