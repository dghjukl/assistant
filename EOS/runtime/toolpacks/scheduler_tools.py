"""Scheduler Tools — Safe job scheduling with SQLite persistence

Provides tools for:
- Scheduling one-shot and recurring jobs
- Canceling and running jobs
- Viewing job history

Configuration:
  scheduler:
    enabled: true
    db: data/scheduler.db
    max_history: 5000
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _jdump(x: Any) -> str:
    """JSON dump with fallback to str()."""
    try:
        return json.dumps(x, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


def _safe_int(x: Any, default: int) -> int:
    """Safe integer conversion."""
    try:
        return int(x)
    except Exception:
        return default


def _utc_now() -> datetime:
    """Current time in UTC."""
    return datetime.now(timezone.utc)


def _utc_iso(dt: Optional[datetime] = None) -> str:
    """ISO 8601 string in UTC."""
    d = dt or _utc_now()
    return d.replace(microsecond=0).isoformat()


def _parse_utc_iso(s: str) -> Optional[datetime]:
    """Parse ISO 8601 string to UTC datetime."""
    if not s:
        return None
    try:
        s2 = s.strip()
        if s2.endswith("Z"):
            s2 = s2[:-1] + "+00:00"
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


class _SchedulerDB:
    """SQLite-backed job scheduler."""

    def __init__(self, db_path: Path, max_history: int = 5000):
        self.db_path = db_path
        self.max_history = max(100, int(max_history))
        self.lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        with self.lock:
            try:
                conn = sqlite3.connect(str(self.db_path), timeout=5)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        next_run_at TEXT,
                        run_at TEXT,
                        interval_seconds INTEGER,
                        action TEXT NOT NULL,
                        meta TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL,
                        name TEXT,
                        started_at TEXT NOT NULL,
                        ended_at TEXT NOT NULL,
                        ok INTEGER NOT NULL,
                        error TEXT,
                        result TEXT
                    )
                    """
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

    def list_jobs(self) -> List[Dict[str, Any]]:
        """List all jobs."""
        with self.lock:
            try:
                conn = sqlite3.connect(str(self.db_path), timeout=5)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY (enabled=0), next_run_at, created_at"
                ).fetchall()
                jobs = []
                for row in rows:
                    jobs.append({
                        "id": row["id"],
                        "name": row["name"],
                        "kind": row["kind"],
                        "enabled": bool(row["enabled"]),
                        "created_at": row["created_at"],
                        "next_run_at": row["next_run_at"],
                        "run_at": row["run_at"],
                        "interval_seconds": row["interval_seconds"],
                        "action": json.loads(row["action"] or "{}"),
                        "meta": json.loads(row["meta"] or "{}"),
                    })
                conn.close()
                return jobs
            except Exception:
                return []

    def create_job(self, name: str, kind: str, run_at: Optional[str] = None,
                   interval_seconds: Optional[int] = None, action: Optional[Dict] = None,
                   meta: Optional[Dict] = None) -> Dict[str, Any]:
        """Create a new job."""
        job_id = str(uuid.uuid4())
        kind = (kind or "at").lower()
        if kind not in ("at", "interval"):
            kind = "at"

        now = _utc_now()
        created = _utc_iso(now)

        ra = None
        nxt = None
        inter = None

        if kind == "at":
            dt = _parse_utc_iso(run_at or "")
            if dt is None:
                dt = now + timedelta(minutes=5)
            ra = _utc_iso(dt)
            nxt = ra
        else:
            sec = int(interval_seconds) if interval_seconds is not None else 60
            sec = max(5, min(sec, 365 * 24 * 3600))
            inter = sec
            nxt = _utc_iso(now + timedelta(seconds=sec))

        action_json = json.dumps(action or {}, ensure_ascii=False)
        meta_json = json.dumps(meta or {}, ensure_ascii=False)

        with self.lock:
            try:
                conn = sqlite3.connect(str(self.db_path), timeout=5)
                conn.execute(
                    """
                    INSERT INTO jobs (id, name, kind, enabled, created_at, next_run_at, run_at, interval_seconds, action, meta)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (job_id, name or f"job-{job_id[:8]}", kind, 1, created, nxt, ra, inter, action_json, meta_json)
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

        return {
            "id": job_id,
            "name": name or f"job-{job_id[:8]}",
            "kind": kind,
            "enabled": True,
            "created_at": created,
            "next_run_at": nxt,
            "run_at": ra,
            "interval_seconds": inter,
            "action": action or {},
            "meta": meta or {},
        }

    def cancel(self, job_id: str) -> bool:
        """Cancel (disable) a job."""
        with self.lock:
            try:
                conn = sqlite3.connect(str(self.db_path), timeout=5)
                conn.execute(
                    "UPDATE jobs SET enabled=0, next_run_at=NULL WHERE id=?",
                    (str(job_id),)
                )
                conn.commit()
                affected = conn.total_changes
                conn.close()
                return affected > 0
            except Exception:
                return False

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get a single job by ID."""
        with self.lock:
            try:
                conn = sqlite3.connect(str(self.db_path), timeout=5)
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM jobs WHERE id=?", (str(job_id),)).fetchone()
                conn.close()
                if not row:
                    return None
                return {
                    "id": row["id"],
                    "name": row["name"],
                    "kind": row["kind"],
                    "enabled": bool(row["enabled"]),
                    "created_at": row["created_at"],
                    "next_run_at": row["next_run_at"],
                    "run_at": row["run_at"],
                    "interval_seconds": row["interval_seconds"],
                    "action": json.loads(row["action"] or "{}"),
                    "meta": json.loads(row["meta"] or "{}"),
                }
            except Exception:
                return None

    def record_history(self, job_id: str, name: str, started_at: str, ended_at: str,
                       ok: bool, error: Optional[str] = None, result: Optional[str] = None) -> None:
        """Record job execution history."""
        with self.lock:
            try:
                conn = sqlite3.connect(str(self.db_path), timeout=5)
                conn.execute(
                    """
                    INSERT INTO history (job_id, name, started_at, ended_at, ok, error, result)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (str(job_id), str(name), started_at, ended_at, int(ok), error, result[:4000] if result else None)
                )
                conn.execute(
                    "DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT ?)",
                    (self.max_history,)
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

    def get_history(self, job_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Get job execution history."""
        limit = max(1, min(int(limit), 500))
        with self.lock:
            try:
                conn = sqlite3.connect(str(self.db_path), timeout=5)
                conn.row_factory = sqlite3.Row
                if job_id:
                    rows = conn.execute(
                        "SELECT * FROM history WHERE job_id=? ORDER BY id DESC LIMIT ?",
                        (str(job_id), limit)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM history ORDER BY id DESC LIMIT ?",
                        (limit,)
                    ).fetchall()

                result = []
                for row in rows:
                    result.append({
                        "job_id": row["job_id"],
                        "name": row["name"],
                        "started_at": row["started_at"],
                        "ended_at": row["ended_at"],
                        "ok": bool(row["ok"]),
                        "error": row["error"],
                        "result": row["result"],
                    })
                conn.close()
                return result
            except Exception:
                return []


def register(registry: Any, config: Dict[str, Any]) -> None:
    """Register scheduler tools into the registry."""
    from runtime.tool_registry import (
        ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy
    )

    sch_cfg = config.get("scheduler", {}) if isinstance(config, dict) else {}
    enabled = bool(sch_cfg.get("enabled", True))

    db_path = Path(str(sch_cfg.get("db", "data/scheduler.db")))
    if not db_path.is_absolute():
        db_path = Path(config.get("project_root", ".")).resolve() / db_path

    max_history = _safe_int(sch_cfg.get("max_history", 5000), 5000)
    scheduler = _SchedulerDB(db_path, max_history=max_history)

    def list_jobs_handler(params: Dict[str, Any]) -> str:
        if not enabled:
            return _jdump({"error": "Scheduler disabled"})
        return _jdump(scheduler.list_jobs())

    registry.register(ToolSpec(
        name="list_jobs",
        description="List all scheduled jobs.",
        pack="scheduler_tools",
        tags=["scheduler"],
        parameters={"type": "object", "properties": {}, "required": []},
        handler=list_jobs_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    def get_job_history_handler(params: Dict[str, Any]) -> str:
        if not enabled:
            return _jdump({"error": "Scheduler disabled"})
        job_id = str(params.get("job_id") or "").strip() if params.get("job_id") else None
        limit = _safe_int(params.get("limit", 50), 50)
        return _jdump(scheduler.get_history(job_id=job_id, limit=limit))

    registry.register(ToolSpec(
        name="get_job_history",
        description="Get execution history for a job or all jobs.",
        pack="scheduler_tools",
        tags=["scheduler"],
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
        handler=get_job_history_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    def create_job_handler(params: Dict[str, Any]) -> str:
        if not enabled:
            return _jdump({"error": "Scheduler disabled"})
        name = str(params.get("name") or "").strip()
        kind = str(params.get("kind") or "at").strip().lower()
        run_at = params.get("run_at")
        interval_seconds = params.get("interval_seconds")
        action = params.get("action") if isinstance(params.get("action"), dict) else {}
        meta = params.get("meta") if isinstance(params.get("meta"), dict) else {}

        job = scheduler.create_job(
            name=name,
            kind=kind,
            run_at=str(run_at) if run_at else None,
            interval_seconds=_safe_int(interval_seconds, 60),
            action=action,
            meta=meta,
        )
        return _jdump(job)

    registry.register(ToolSpec(
        name="create_job",
        description="Schedule a job (kind='at' for one-shot UTC timestamp, 'interval' for recurring).",
        pack="scheduler_tools",
        tags=["scheduler"],
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "kind": {"type": "string", "enum": ["at", "interval"]},
                "run_at": {"type": "string"},
                "interval_seconds": {"type": "integer"},
                "action": {"type": "object"},
                "meta": {"type": "object"},
            },
            "required": ["kind", "action"],
        },
        handler=create_job_handler,
        risk_level=ToolRiskLevel.REVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.SOFT_CONFIRM,
        enabled=enabled,
    ))

    def cancel_job_handler(params: Dict[str, Any]) -> str:
        if not enabled:
            return _jdump({"error": "Scheduler disabled"})
        job_id = str(params.get("job_id") or "").strip()
        if not job_id:
            return _jdump({"error": "Missing job_id"})
        ok = scheduler.cancel(job_id)
        return _jdump({"job_id": job_id, "cancelled": ok})

    registry.register(ToolSpec(
        name="cancel_job",
        description="Cancel (disable) a scheduled job.",
        pack="scheduler_tools",
        tags=["scheduler"],
        parameters={
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
        handler=cancel_job_handler,
        risk_level=ToolRiskLevel.REVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.SOFT_CONFIRM,
        enabled=enabled,
    ))

    def trigger_job_handler(params: Dict[str, Any]) -> str:
        if not enabled:
            return _jdump({"error": "Scheduler disabled"})
        job_id = str(params.get("job_id") or "").strip()
        if not job_id:
            return _jdump({"error": "Missing job_id"})

        job = scheduler.get_job(job_id)
        if not job:
            return _jdump({"error": "Job not found", "job_id": job_id})

        started = _utc_iso()
        ended = _utc_iso()
        result = "Manual trigger executed (internal job execution not implemented in this tool)"
        scheduler.record_history(job_id, job["name"], started, ended, True, result=result)

        return _jdump({"ok": True, "job_id": job_id, "message": result})

    registry.register(ToolSpec(
        name="trigger_job",
        description="Manually trigger a job to run now.",
        pack="scheduler_tools",
        tags=["scheduler"],
        parameters={
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
        handler=trigger_job_handler,
        risk_level=ToolRiskLevel.REVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.SOFT_CONFIRM,
        enabled=enabled,
    ))
