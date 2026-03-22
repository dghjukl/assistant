"""
EOS — Backup and Restore Service
Snapshots and restores the complete entity state.

What gets backed up
-------------------
  entity_state.db        Core SQLite database (identity, relational, goals,
                         autonomy, interaction log, reflection log)
  memory_store/          ChromaDB vector memory (semantic recall)
  workspace/             Entity's persistent file environment
  entity_lifecycle.json  Operational history record
  session_continuity.json Previous session excerpt
  shutdown_ledger.json   Clean/unclean shutdown record

What is NOT backed up
---------------------
  backups/               (no recursive backups)
  google_token.json      Credential, not state
  *.tmp                  Temp files

Backup storage
--------------
  data/backups/{YYYYMMDD_HHMMSS}_{label}/
    manifest.json    — metadata, file list, integrity hashes
    entity_state.db
    memory_store/
    workspace/
    *.json

Auto-backup policy
------------------
On startup, if the most recent backup is older than auto_backup_interval_hours
(default 24 h) OR there is no backup at all, a background backup is created.
This ensures there is always a recent recovery point.

Restore
-------
restore_backup(backup_id) swaps the current data directory contents with the
backup, moving the current state to a .pre_restore_{timestamp} directory so
it can be manually inspected if needed.  Requires a process restart to take
full effect (SQLite connections and ChromaDB handles need to be re-opened).

Integrity check
---------------
integrity_check() verifies that all expected data files are present and
readable, without touching running state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("eos.backup_service")
UTC = timezone.utc

_AUTO_BACKUP_INTERVAL_HOURS = 24
_MAX_BACKUPS = 10          # oldest backups beyond this limit are pruned
_HASH_CHUNK  = 65536       # bytes per read when hashing


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _now_tag() -> str:
    """Compact timestamp string for directory names."""
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


def _sha256(path: Path) -> str:
    """Return hex SHA-256 of a file."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _dir_size(path: Path) -> int:
    """Return total byte size of all files under path."""
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except Exception:
        pass
    return total


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class BackupManifest:
    backup_id:    str
    created_at:   str
    label:        str
    backup_path:  str
    files:        list[dict]   # [{name, type, size_bytes, sha256?}]
    total_size_bytes: int
    eos_version:  str
    trigger:      str          # "auto" | "manual" | "admin"
    notes:        str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BackupManifest":
        return cls(
            backup_id        = d["backup_id"],
            created_at       = d["created_at"],
            label            = d.get("label", ""),
            backup_path      = d["backup_path"],
            files            = d.get("files", []),
            total_size_bytes = int(d.get("total_size_bytes", 0)),
            eos_version      = d.get("eos_version", "unknown"),
            trigger          = d.get("trigger", "manual"),
            notes            = d.get("notes", ""),
        )


@dataclass
class IntegrityReport:
    ok: bool
    checked_at: str
    findings: list[dict]   # [{path, status, detail}]

    def to_dict(self) -> dict:
        return asdict(self)


# ── Service ───────────────────────────────────────────────────────────────────

class BackupService:
    """
    Manages snapshots and restores of the complete entity state.

    Parameters
    ----------
    cfg : dict
        Runtime config.  Reads:
          db_path              (default: "data/entity_state.db")
          retrieval.chroma_path (default: "data/memory_store")
          workspace_tools.workspace_root (default: "data/workspace")
          backup.backup_path   (default: "data/backups")
          backup.max_backups   (default: 10)
          backup.auto_backup_interval_hours (default: 24)
          project_root         (default: ".")
    """

    def __init__(self, cfg: dict) -> None:
        project_root = Path(cfg.get("project_root", ".")).resolve()
        backup_cfg   = cfg.get("backup", {})

        self._data_dir    = project_root / "data"
        self._db_path     = project_root / cfg.get("db_path", "data/entity_state.db")
        self._chroma_path = project_root / cfg.get("retrieval", {}).get("chroma_path", "data/memory_store")

        ws_root_rel       = cfg.get("workspace_tools", {}).get("workspace_root", "data/workspace")
        self._ws_path     = (project_root / ws_root_rel).resolve()

        backup_path_rel   = backup_cfg.get("backup_path", "data/backups")
        self._backup_root = (project_root / backup_path_rel).resolve()
        self._backup_root.mkdir(parents=True, exist_ok=True)

        self._max_backups = int(backup_cfg.get("max_backups", _MAX_BACKUPS))
        self._auto_interval_hours = float(
            backup_cfg.get("auto_backup_interval_hours", _AUTO_BACKUP_INTERVAL_HOURS)
        )
        self._cfg = cfg

    # ── Public API ────────────────────────────────────────────────────────────

    def create_backup(
        self,
        label: str = "",
        trigger: str = "manual",
        notes: str = "",
    ) -> BackupManifest:
        """
        Create a full state snapshot.

        Returns the BackupManifest.  Raises on failure.
        """
        tag        = _now_tag()
        safe_label = label.replace(" ", "_")[:30] if label else ""
        dir_name   = f"{tag}_{safe_label}" if safe_label else tag
        backup_dir = self._backup_root / dir_name
        backup_dir.mkdir(parents=True, exist_ok=True)

        logger.info("[backup] Creating backup → %s (trigger=%s)", dir_name, trigger)

        files: list[dict] = []
        total_bytes = 0

        # ── SQLite (online backup via sqlite3 API) ────────────────────────
        db_dest = backup_dir / "entity_state.db"
        if self._db_path.exists():
            try:
                src_conn  = sqlite3.connect(str(self._db_path))
                dest_conn = sqlite3.connect(str(db_dest))
                src_conn.backup(dest_conn)
                src_conn.close()
                dest_conn.close()
                sz = db_dest.stat().st_size
                files.append({
                    "name": "entity_state.db",
                    "type": "sqlite",
                    "size_bytes": sz,
                    "sha256": _sha256(db_dest),
                })
                total_bytes += sz
                logger.debug("[backup] SQLite backup done (%d bytes)", sz)
            except Exception as exc:
                logger.warning("[backup] SQLite backup failed: %s", exc)
                files.append({"name": "entity_state.db", "type": "sqlite", "error": str(exc)})

        # ── ChromaDB directory copy ───────────────────────────────────────
        if self._chroma_path.exists():
            chroma_dest = backup_dir / "memory_store"
            try:
                shutil.copytree(str(self._chroma_path), str(chroma_dest))
                sz = _dir_size(chroma_dest)
                files.append({
                    "name": "memory_store",
                    "type": "directory",
                    "size_bytes": sz,
                })
                total_bytes += sz
                logger.debug("[backup] ChromaDB copy done (%d bytes)", sz)
            except Exception as exc:
                logger.warning("[backup] ChromaDB copy failed: %s", exc)
                files.append({"name": "memory_store", "type": "directory", "error": str(exc)})

        # ── Workspace copy ────────────────────────────────────────────────
        if self._ws_path.exists():
            ws_dest = backup_dir / "workspace"
            try:
                shutil.copytree(str(self._ws_path), str(ws_dest))
                sz = _dir_size(ws_dest)
                files.append({
                    "name": "workspace",
                    "type": "directory",
                    "size_bytes": sz,
                })
                total_bytes += sz
                logger.debug("[backup] Workspace copy done (%d bytes)", sz)
            except Exception as exc:
                logger.warning("[backup] Workspace copy failed: %s", exc)
                files.append({"name": "workspace", "type": "directory", "error": str(exc)})

        # ── JSON state files ──────────────────────────────────────────────
        json_files = [
            "entity_lifecycle.json",
            "session_continuity.json",
            "shutdown_ledger.json",
        ]
        for fname in json_files:
            src = self._data_dir / fname
            if src.exists():
                try:
                    dest = backup_dir / fname
                    shutil.copy2(str(src), str(dest))
                    sz = dest.stat().st_size
                    files.append({
                        "name": fname,
                        "type": "json",
                        "size_bytes": sz,
                        "sha256": _sha256(dest),
                    })
                    total_bytes += sz
                except Exception as exc:
                    logger.warning("[backup] Failed to copy %s: %s", fname, exc)

        # ── Write manifest ────────────────────────────────────────────────
        version = _eos_version()
        manifest = BackupManifest(
            backup_id        = dir_name,
            created_at       = _now_iso(),
            label            = label,
            backup_path      = str(backup_dir),
            files            = files,
            total_size_bytes = total_bytes,
            eos_version      = version,
            trigger          = trigger,
            notes            = notes,
        )
        (backup_dir / "manifest.json").write_text(
            json.dumps(manifest.to_dict(), indent=2),
            encoding="utf-8",
        )

        logger.info(
            "[backup] Backup complete: %s (%s, %d items)",
            dir_name,
            _human_size(total_bytes),
            len(files),
        )

        # Prune old backups
        self._prune_old_backups()

        return manifest

    def list_backups(self) -> list[BackupManifest]:
        """Return all backups, newest first."""
        manifests: list[BackupManifest] = []
        if not self._backup_root.exists():
            return manifests
        for entry in sorted(self._backup_root.iterdir(), reverse=True):
            if not entry.is_dir():
                continue
            mf_path = entry / "manifest.json"
            if not mf_path.exists():
                continue
            try:
                manifests.append(
                    BackupManifest.from_dict(
                        json.loads(mf_path.read_text(encoding="utf-8"))
                    )
                )
            except Exception as exc:
                logger.debug("[backup] Failed to load manifest %s: %s", mf_path, exc)
        return manifests

    def get_backup(self, backup_id: str) -> Optional[BackupManifest]:
        backup_dir = self._backup_root / backup_id
        mf_path = backup_dir / "manifest.json"
        if not mf_path.exists():
            return None
        try:
            return BackupManifest.from_dict(
                json.loads(mf_path.read_text(encoding="utf-8"))
            )
        except Exception:
            return None

    def restore_backup(self, backup_id: str) -> dict:
        """
        Restore entity state from a backup.

        The current data directory is moved to data/backups/.pre_restore_{ts}
        before the restore proceeds so it can be recovered manually.

        Returns a result dict: {ok, backup_id, pre_restore_path, note}

        Important: A process restart is required for the restore to take
        full effect (running SQLite connections and ChromaDB handles need
        to be re-opened).
        """
        backup_dir = self._backup_root / backup_id
        if not backup_dir.exists():
            return {"ok": False, "error": f"Backup not found: {backup_id}"}

        manifest = self.get_backup(backup_id)
        if manifest is None:
            return {"ok": False, "error": "Could not read backup manifest"}

        tag = _now_tag()
        pre_restore_dir = self._backup_root / f".pre_restore_{tag}"
        logger.info("[backup] Restoring from %s → current state saved to %s", backup_id, pre_restore_dir.name)

        errors: list[str] = []

        # ── SQLite ────────────────────────────────────────────────────────
        db_src = backup_dir / "entity_state.db"
        if db_src.exists():
            try:
                if self._db_path.exists():
                    pre_restore_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(self._db_path), str(pre_restore_dir / "entity_state.db"))
                src_conn  = sqlite3.connect(str(db_src))
                dest_conn = sqlite3.connect(str(self._db_path))
                src_conn.backup(dest_conn)
                src_conn.close()
                dest_conn.close()
                logger.info("[backup] SQLite restored.")
            except Exception as exc:
                errors.append(f"SQLite restore failed: {exc}")
                logger.error("[backup] SQLite restore failed: %s", exc)

        # ── ChromaDB ──────────────────────────────────────────────────────
        chroma_src = backup_dir / "memory_store"
        if chroma_src.exists():
            try:
                if self._chroma_path.exists():
                    pre_restore_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(str(self._chroma_path),
                                    str(pre_restore_dir / "memory_store"))
                    shutil.rmtree(str(self._chroma_path))
                shutil.copytree(str(chroma_src), str(self._chroma_path))
                logger.info("[backup] ChromaDB restored.")
            except Exception as exc:
                errors.append(f"ChromaDB restore failed: {exc}")
                logger.error("[backup] ChromaDB restore failed: %s", exc)

        # ── Workspace ─────────────────────────────────────────────────────
        ws_src = backup_dir / "workspace"
        if ws_src.exists():
            try:
                if self._ws_path.exists():
                    pre_restore_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(str(self._ws_path),
                                    str(pre_restore_dir / "workspace"))
                    shutil.rmtree(str(self._ws_path))
                shutil.copytree(str(ws_src), str(self._ws_path))
                logger.info("[backup] Workspace restored.")
            except Exception as exc:
                errors.append(f"Workspace restore failed: {exc}")
                logger.error("[backup] Workspace restore failed: %s", exc)

        # ── JSON state files ──────────────────────────────────────────────
        json_files = [
            "entity_lifecycle.json",
            "session_continuity.json",
            "shutdown_ledger.json",
        ]
        for fname in json_files:
            src = backup_dir / fname
            if src.exists():
                try:
                    dest = self._data_dir / fname
                    if dest.exists():
                        pre_restore_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(dest), str(pre_restore_dir / fname))
                    shutil.copy2(str(src), str(dest))
                except Exception as exc:
                    errors.append(f"{fname} restore failed: {exc}")

        result = {
            "ok": len(errors) == 0,
            "backup_id": backup_id,
            "restored_at": _now_iso(),
            "pre_restore_path": str(pre_restore_dir) if pre_restore_dir.exists() else None,
            "errors": errors,
            "note": "Restart required for changes to take full effect.",
        }
        if errors:
            logger.warning("[backup] Restore completed with %d error(s): %s", len(errors), errors)
        else:
            logger.info("[backup] Restore complete from %s.", backup_id)
        return result

    def integrity_check(self) -> IntegrityReport:
        """
        Verify that all expected data components are present and minimally
        readable.  Does not modify any state.
        """
        findings: list[dict] = []
        all_ok = True

        def _check(label: str, path: Path, kind: str = "file", optional: bool = False) -> None:
            nonlocal all_ok
            if not path.exists():
                findings.append({"path": label, "status": "missing"})
                if not optional:
                    all_ok = False
                return
            if kind == "sqlite":
                try:
                    conn = sqlite3.connect(str(path))
                    tables = [r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()]
                    conn.close()
                    findings.append({
                        "path": label,
                        "status": "ok",
                        "tables": tables,
                        "size_bytes": path.stat().st_size,
                    })
                except Exception as exc:
                    findings.append({"path": label, "status": "error", "detail": str(exc)})
                    all_ok = False
            elif kind == "dir":
                n = sum(1 for _ in path.rglob("*") if _.is_file())
                findings.append({
                    "path": label,
                    "status": "ok",
                    "file_count": n,
                    "size_bytes": _dir_size(path),
                })
            else:
                sz = path.stat().st_size
                findings.append({"path": label, "status": "ok", "size_bytes": sz})

        _check("entity_state.db",        self._db_path,     "sqlite")
        _check("memory_store/",          self._chroma_path, "dir")
        _check("workspace/",             self._ws_path,     "dir")
        _check("entity_lifecycle.json",  self._data_dir / "entity_lifecycle.json")
        _check("session_continuity.json",self._data_dir / "session_continuity.json", optional=True)
        _check("shutdown_ledger.json",   self._data_dir / "shutdown_ledger.json")

        return IntegrityReport(
            ok          = all_ok,
            checked_at  = _now_iso(),
            findings    = findings,
        )

    def needs_auto_backup(self) -> bool:
        """True if no backup exists or most recent is older than the interval."""
        backups = self.list_backups()
        if not backups:
            return True
        latest = backups[0]
        try:
            from datetime import datetime
            created = datetime.fromisoformat(latest.created_at.replace("Z", "+00:00"))
            age_hours = (datetime.now(UTC) - created).total_seconds() / 3600
            return age_hours >= self._auto_interval_hours
        except Exception:
            return True

    # ── Internal ──────────────────────────────────────────────────────────────

    def _prune_old_backups(self) -> None:
        """Remove oldest backups beyond _max_backups limit."""
        backups = self.list_backups()
        to_remove = backups[self._max_backups:]
        for bk in to_remove:
            try:
                shutil.rmtree(bk.backup_path)
                logger.info("[backup] Pruned old backup: %s", bk.backup_id)
            except Exception as exc:
                logger.warning("[backup] Failed to prune %s: %s", bk.backup_id, exc)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _eos_version() -> str:
    for candidate in [
        Path(__file__).parent.parent / "VERSION",
        Path(__file__).parent.parent / "version.txt",
    ]:
        if candidate.exists():
            try:
                return candidate.read_text().strip()
            except OSError:
                pass
    return "unknown"


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    else:
        return f"{n / (1024 * 1024):.1f}MB"
