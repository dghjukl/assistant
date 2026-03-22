"""
AuditStore — durable, queryable audit log for admin actions and tool executions.

Writes to data/audit.db (SQLite, separate from entity_state.db).

Tables
------
admin_actions
    Every admin API write (tool toggles, permission changes, autonomy
    updates, computer-use mode changes, etc.)

tool_executions
    Every tool call dispatched through ToolExecutor, with outcome and
    timing.  Replaces the in-memory AuditLog in ToolRegistry for
    durable history.

Usage
-----
    from core.audit import init_audit_store, get_audit_store

    # At startup:
    store = init_audit_store(Path("data/audit.db"))

    # In admin endpoints:
    store.record_admin_action("tool_toggle", target="read_file", details={"enabled": True})

    # In tool executor:
    store.record_tool_execution("read_file", success=True, duration_ms=42)

    # Query:
    rows = store.query_admin_actions(action_type="tool_toggle", limit=50)
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AuditStore:
    """Thread-safe SQLite-backed audit log."""

    def __init__(self, db_path: Path | str):
        self._db = Path(db_path)
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        logger.info("[audit] AuditStore initialised at %s", self._db)
        # Run pending schema migrations
        try:
            from core.db_migrations import apply_migrations
            with self._conn() as mconn:
                apply_migrations(mconn, "audit")
        except Exception as exc:
            logger.warning("[audit] Migration runner failed (non-fatal): %s", exc)

    # ── Internal ──────────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    id         TEXT NOT NULL,
                    db_name    TEXT NOT NULL DEFAULT '',
                    applied_at REAL NOT NULL,
                    PRIMARY KEY (id, db_name)
                );

                CREATE TABLE IF NOT EXISTS admin_actions (
                    id          TEXT PRIMARY KEY,
                    timestamp   REAL NOT NULL,
                    actor       TEXT NOT NULL DEFAULT 'admin',
                    action_type TEXT NOT NULL,
                    target      TEXT,
                    details     TEXT,
                    outcome     TEXT NOT NULL DEFAULT 'success',
                    origin_tier TEXT,
                    client_ip   TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_admin_actions_ts
                    ON admin_actions (timestamp);
                CREATE INDEX IF NOT EXISTS idx_admin_actions_type
                    ON admin_actions (action_type);

                CREATE TABLE IF NOT EXISTS tool_executions (
                    id              TEXT PRIMARY KEY,
                    timestamp       REAL NOT NULL,
                    tool_name       TEXT NOT NULL,
                    pack            TEXT,
                    risk_level      TEXT,
                    params_summary  TEXT,
                    success         INTEGER NOT NULL,
                    error           TEXT,
                    duration_ms     INTEGER,
                    origin_tier     TEXT,
                    client_ip       TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tool_exec_ts
                    ON tool_executions (timestamp);
                CREATE INDEX IF NOT EXISTS idx_tool_exec_name
                    ON tool_executions (tool_name);
            """)

    # ── Write ─────────────────────────────────────────────────────────────

    def record_admin_action(
        self,
        action_type: str,
        target: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        actor: str = "admin",
        outcome: str = "success",
        origin_tier: Optional[str] = None,
        client_ip: Optional[str] = None,
    ) -> str:
        """Record an admin panel action. Returns the generated action ID."""
        action_id = str(uuid.uuid4())
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO admin_actions VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        action_id,
                        time.time(),
                        actor,
                        action_type,
                        target,
                        json.dumps(details) if details is not None else None,
                        outcome,
                        origin_tier,
                        client_ip,
                    ),
                )
        except Exception as exc:
            logger.error("[audit] record_admin_action failed: %s", exc)
        return action_id

    def record_tool_execution(
        self,
        tool_name: str,
        success: bool,
        pack: Optional[str] = None,
        risk_level: Optional[str] = None,
        params_summary: Optional[str] = None,
        error: Optional[str] = None,
        duration_ms: Optional[int] = None,
        origin_tier: Optional[str] = None,
        client_ip: Optional[str] = None,
    ) -> str:
        """Record a tool execution outcome. Returns the generated execution ID."""
        exec_id = str(uuid.uuid4())
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO tool_executions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        exec_id,
                        time.time(),
                        tool_name,
                        pack,
                        risk_level,
                        params_summary,
                        1 if success else 0,
                        error,
                        duration_ms,
                        origin_tier,
                        client_ip,
                    ),
                )
        except Exception as exc:
            logger.error("[audit] record_tool_execution failed: %s", exc)
        return exec_id

    # ── Query ─────────────────────────────────────────────────────────────

    def query_admin_actions(
        self,
        action_type: Optional[str] = None,
        actor: Optional[str] = None,
        target: Optional[str] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
        outcome: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Query admin actions with optional filters. Newest first."""
        clauses: list[str] = []
        args: list[Any] = []
        if action_type:
            clauses.append("action_type = ?")
            args.append(action_type)
        if actor:
            clauses.append("actor = ?")
            args.append(actor)
        if target:
            clauses.append("target = ?")
            args.append(target)
        if since is not None:
            clauses.append("timestamp >= ?")
            args.append(since)
        if until is not None:
            clauses.append("timestamp <= ?")
            args.append(until)
        if outcome:
            clauses.append("outcome = ?")
            args.append(outcome)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(max(1, min(limit, 1000)))
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    f"SELECT * FROM admin_actions {where} ORDER BY timestamp DESC LIMIT ?",
                    args,
                ).fetchall()
            result = []
            for r in rows:
                row = dict(r)
                if row.get("details"):
                    try:
                        row["details"] = json.loads(row["details"])
                    except Exception:
                        pass
                result.append(row)
            return result
        except Exception as exc:
            logger.error("[audit] query_admin_actions failed: %s", exc)
            return []

    def query_tool_executions(
        self,
        tool_name: Optional[str] = None,
        pack: Optional[str] = None,
        risk_level: Optional[str] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
        success_only: Optional[bool] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Query tool executions with optional filters. Newest first."""
        clauses: list[str] = []
        args: list[Any] = []
        if tool_name:
            clauses.append("tool_name = ?")
            args.append(tool_name)
        if pack:
            clauses.append("pack = ?")
            args.append(pack)
        if risk_level:
            clauses.append("risk_level = ?")
            args.append(risk_level)
        if since is not None:
            clauses.append("timestamp >= ?")
            args.append(since)
        if until is not None:
            clauses.append("timestamp <= ?")
            args.append(until)
        if success_only is not None:
            clauses.append("success = ?")
            args.append(1 if success_only else 0)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(max(1, min(limit, 1000)))
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    f"SELECT * FROM tool_executions {where} ORDER BY timestamp DESC LIMIT ?",
                    args,
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.error("[audit] query_tool_executions failed: %s", exc)
            return []

    def summary(self) -> Dict[str, Any]:
        """Return aggregate statistics for the audit log."""
        try:
            with self._conn() as conn:
                admin_total = conn.execute("SELECT COUNT(*) FROM admin_actions").fetchone()[0]
                tool_total  = conn.execute("SELECT COUNT(*) FROM tool_executions").fetchone()[0]
                tool_ok     = conn.execute("SELECT COUNT(*) FROM tool_executions WHERE success=1").fetchone()[0]
                tool_fail   = conn.execute("SELECT COUNT(*) FROM tool_executions WHERE success=0").fetchone()[0]
                recent_admin = conn.execute(
                    "SELECT action_type, target, timestamp, outcome FROM admin_actions "
                    "ORDER BY timestamp DESC LIMIT 10"
                ).fetchall()
            return {
                "admin_actions_total": admin_total,
                "tool_executions_total": tool_total,
                "tool_executions_ok": tool_ok,
                "tool_executions_failed": tool_fail,
                "recent_admin_actions": [dict(r) for r in recent_admin],
            }
        except Exception as exc:
            logger.error("[audit] summary failed: %s", exc)
            return {}


# ── Module-level singleton ──────────────────────────────────────────────────

_audit_store: Optional[AuditStore] = None


def init_audit_store(db_path: Path | str) -> AuditStore:
    """Initialise the module-level AuditStore. Call once at startup."""
    global _audit_store
    _audit_store = AuditStore(db_path)
    return _audit_store


def get_audit_store() -> Optional[AuditStore]:
    """Return the active AuditStore, or None if not yet initialised."""
    return _audit_store
