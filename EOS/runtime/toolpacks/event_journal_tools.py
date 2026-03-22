"""Event Journal Tools — Persistent event logging

Configuration:
  event_journal:
    enabled: true
    db: data/event_journal.db
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


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


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def register(registry: Any, config: Dict[str, Any]) -> None:
    from runtime.tool_registry import ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy

    jcfg = config.get("event_journal", {}) if isinstance(config, dict) else {}
    enabled = bool(jcfg.get("enabled", True))

    db_path = Path(str(jcfg.get("db", "data/event_journal.db")))
    if not db_path.is_absolute():
        db_path = Path(config.get("project_root", ".")).resolve() / db_path

    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Initialize DB
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                kind TEXT NOT NULL,
                tags TEXT,
                payload TEXT
            )
        """)
        conn.commit()
        conn.close()
    except Exception:
        pass

    def write_event_handler(params: Dict[str, Any]) -> str:
        if not enabled:
            return "Event journal disabled"
        kind = str(params.get("kind") or "event").strip()
        payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
        tags = params.get("tags") if isinstance(params.get("tags"), list) else []
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            conn.execute(
                "INSERT INTO events (ts, kind, tags, payload) VALUES (?, ?, ?, ?)",
                (_utc_iso(), kind, json.dumps(tags), json.dumps(payload, ensure_ascii=False))
            )
            conn.commit()
            conn.close()
        except Exception:
            pass
        return _jdump({"ok": True, "ts": _utc_iso(), "kind": kind})

    registry.register(ToolSpec(
        name="write_event",
        description="Write an event to the journal.",
        pack="event_journal_tools",
        tags=["logging", "events"],
        parameters={"type": "object", "properties": {"kind": {"type": "string"}, "payload": {"type": "object"}, "tags": {"type": "array"}}, "required": ["kind"]},
        handler=write_event_handler,
        risk_level=ToolRiskLevel.DRAFT,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    def query_events_handler(params: Dict[str, Any]) -> str:
        if not enabled:
            return "Event journal disabled"
        kind = str(params.get("kind") or "").strip() if params.get("kind") else None
        limit = _safe_int(params.get("limit", 50), 50)
        limit = max(1, min(limit, 500))
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            if kind:
                rows = conn.execute("SELECT * FROM events WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            items = []
            for row in rows:
                items.append({"ts": row[1], "kind": row[2], "tags": json.loads(row[3] or "[]"), "payload": json.loads(row[4] or "{}")})
            conn.close()
            return _jdump({"count": len(items), "items": items})
        except Exception:
            return _jdump({"count": 0, "items": []})

    registry.register(ToolSpec(
        name="query_events",
        description="Query events from the journal.",
        pack="event_journal_tools",
        tags=["logging", "events"],
        parameters={"type": "object", "properties": {"kind": {"type": "string"}, "limit": {"type": "integer"}}, "required": []},
        handler=query_events_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))
