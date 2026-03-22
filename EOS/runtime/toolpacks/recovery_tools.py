"""Recovery Tools — System recovery utilities"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


def _jdump(x: Any) -> str:
    try:
        return json.dumps(x, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


def register(registry: Any, config: Dict[str, Any]) -> None:
    from runtime.tool_registry import ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy

    cfg_rec = config.get("recovery", {}) if isinstance(config, dict) else {}
    enabled = bool(cfg_rec.get("enabled", True))
    project_root = Path(str(config.get("project_root", "."))).resolve()

    def check_disk_space_handler(params: Dict[str, Any]) -> str:
        try:
            import shutil
            stat = shutil.disk_usage("/")
            return _jdump({
                "total_bytes": stat.total,
                "used_bytes": stat.used,
                "free_bytes": stat.free,
                "percent_used": round(100 * stat.used / stat.total, 2) if stat.total else None,
            })
        except Exception as e:
            return _jdump({"error": str(e)})

    registry.register(ToolSpec(
        name="check_disk_space",
        description="Check disk space usage.",
        pack="recovery_tools",
        tags=["system", "recovery"],
        parameters={"type": "object", "properties": {}, "required": []},
        handler=check_disk_space_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    def clear_python_cache_handler(params: Dict[str, Any]) -> str:
        if not enabled:
            return "Recovery tools disabled"
        try:
            import shutil
            cache_dir = project_root / "__pycache__"
            if cache_dir.exists():
                shutil.rmtree(cache_dir)
            return _jdump({"ok": True, "path": str(cache_dir), "cleared": True})
        except Exception as e:
            return _jdump({"ok": False, "error": str(e)})

    registry.register(ToolSpec(
        name="clear_python_cache",
        description="Clear __pycache__ directories.",
        pack="recovery_tools",
        tags=["system", "recovery"],
        parameters={"type": "object", "properties": {}, "required": []},
        handler=clear_python_cache_handler,
        risk_level=ToolRiskLevel.REVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.OPERATOR_ONLY,
        confirmation_policy=ConfirmationPolicy.SOFT_CONFIRM,
        enabled=enabled,
    ))

    # ── Backup / restore tools ────────────────────────────────────────────────

    def _get_backup_service():
        """Lazily resolve BackupService from the live server globals or create fresh."""
        try:
            import webui.server as _srv
            bs = getattr(_srv, "_backup_service", None)
            if bs is not None:
                return bs
        except Exception:
            pass
        # Fall back to a fresh instance using config
        try:
            from runtime.backup_service import BackupService
            return BackupService(config)
        except Exception as exc:
            raise RuntimeError(f"BackupService unavailable: {exc}") from exc

    def backup_list_handler(params: Dict[str, Any]) -> str:
        """List all state snapshots."""
        try:
            bs = _get_backup_service()
            backups = bs.list_backups()
            return _jdump({
                "total": len(backups),
                "backups": [b.to_dict() for b in backups],
            })
        except Exception as exc:
            return _jdump({"ok": False, "error": str(exc)})

    registry.register(ToolSpec(
        name="backup_list",
        description=(
            "List all EOS state snapshots. Returns backup IDs, timestamps, labels, "
            "sizes, and whether each covers SQLite, ChromaDB, workspace, and JSON state."
        ),
        pack="recovery_tools",
        tags=["system", "recovery", "backup"],
        parameters={"type": "object", "properties": {}, "required": []},
        handler=backup_list_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    def backup_create_handler(params: Dict[str, Any]) -> str:
        """Create a new state snapshot."""
        try:
            bs = _get_backup_service()
            label  = str(params.get("label", "")).strip()
            notes  = str(params.get("notes", "")).strip()
            manifest = bs.create_backup(
                label=label or "entity_initiated",
                trigger="entity_tool",
                notes=notes,
            )
            return _jdump({
                "ok": True,
                "backup_id":  manifest.backup_id,
                "created_at": manifest.created_at,
                "size_bytes": manifest.size_bytes,
                "components": manifest.components,
            })
        except Exception as exc:
            return _jdump({"ok": False, "error": str(exc)})

    registry.register(ToolSpec(
        name="backup_create",
        description=(
            "Create a full EOS state snapshot covering SQLite memory DB, ChromaDB vector store, "
            "workspace files, and JSON state files.  Use before making significant changes or "
            "when instructed by your partner.  Accepts an optional label and notes."
        ),
        pack="recovery_tools",
        tags=["system", "recovery", "backup"],
        parameters={
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Short label for the snapshot (e.g. 'pre-experiment').",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional free-text notes to store with the snapshot.",
                },
            },
            "required": [],
        },
        handler=backup_create_handler,
        risk_level=ToolRiskLevel.REVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    def integrity_check_handler(params: Dict[str, Any]) -> str:
        """Run integrity check on all EOS state components."""
        try:
            bs = _get_backup_service()
            report = bs.integrity_check()
            return _jdump(report.to_dict())
        except Exception as exc:
            return _jdump({"ok": False, "error": str(exc)})

    registry.register(ToolSpec(
        name="integrity_check",
        description=(
            "Check integrity of all EOS state components: SQLite DB, ChromaDB vector store, "
            "workspace directory, and JSON state files.  Returns pass/fail per component plus "
            "a list of any issues found.  Safe to call at any time."
        ),
        pack="recovery_tools",
        tags=["system", "recovery", "diagnostics"],
        parameters={"type": "object", "properties": {}, "required": []},
        handler=integrity_check_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    def backup_restore_handler(params: Dict[str, Any]) -> str:
        """Restore a named backup snapshot."""
        try:
            backup_id = str(params.get("backup_id", "")).strip()
            if not backup_id:
                return _jdump({"ok": False, "error": "backup_id is required"})
            bs = _get_backup_service()
            result = bs.restore_backup(backup_id)
            return _jdump({
                "ok": True,
                "backup_id":       backup_id,
                "pre_restore_id":  result.get("pre_restore_id"),
                "components_restored": result.get("components_restored", []),
                "note": "EOS must be restarted for the restored state to take effect.",
            })
        except FileNotFoundError as exc:
            return _jdump({"ok": False, "error": f"Backup not found: {exc}"})
        except Exception as exc:
            return _jdump({"ok": False, "error": str(exc)})

    registry.register(ToolSpec(
        name="backup_restore",
        description=(
            "Restore EOS to a previous state snapshot identified by backup_id. "
            "The current state is automatically saved as a safety snapshot before overwriting. "
            "EOS must be restarted after restore for changes to take effect. "
            "Use backup_list to discover available backup IDs."
        ),
        pack="recovery_tools",
        tags=["system", "recovery", "backup"],
        parameters={
            "type": "object",
            "properties": {
                "backup_id": {
                    "type": "string",
                    "description": "The backup_id returned by backup_list (e.g. '20250321_143022_auto').",
                },
            },
            "required": ["backup_id"],
        },
        handler=backup_restore_handler,
        risk_level=ToolRiskLevel.IRREVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.OPERATOR_ONLY,
        confirmation_policy=ConfirmationPolicy.HARD_CONFIRM,
        enabled=enabled,
    ))
