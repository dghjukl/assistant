"""Ingestion Tools — Document ingestion and search"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict


def _jdump(x: Any) -> str:
    try:
        return json.dumps(x, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


def register(registry: Any, config: Dict[str, Any]) -> None:
    from runtime.tool_registry import ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy

    icfg = config.get("ingestion", {}) if isinstance(config, dict) else {}
    enabled = bool(icfg.get("enabled", True))

    db_path = Path(str(icfg.get("db", "data/documents.db")))
    if not db_path.is_absolute():
        db_path = Path(config.get("project_root", ".")).resolve() / db_path

    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Initialize DB
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata TEXT,
                ingested_at TEXT
            )
        """)
        conn.commit()
        conn.close()
    except Exception:
        pass

    def ingest_text_handler(params: Dict[str, Any]) -> str:
        if not enabled:
            return "Ingestion disabled"
        text = str(params.get("text") or "").strip()
        path = str(params.get("path") or "").strip()
        if not text:
            return _jdump({"error": "Missing text"})
        try:
            from datetime import datetime
            conn = sqlite3.connect(str(db_path), timeout=5)
            conn.execute(
                "INSERT INTO documents (path, content, ingested_at) VALUES (?, ?, ?)",
                (path or "<text>", text, datetime.now().isoformat())
            )
            conn.commit()
            doc_id = conn.lastrowid
            conn.close()
            return _jdump({"ok": True, "id": doc_id, "chars": len(text)})
        except Exception as e:
            return _jdump({"ok": False, "error": str(e)})

    registry.register(ToolSpec(
        name="ingest_text",
        description="Ingest text into document store.",
        pack="ingestion_tools",
        tags=["ingestion", "documents"],
        parameters={"type": "object", "properties": {"text": {"type": "string"}, "path": {"type": "string"}}, "required": ["text"]},
        handler=ingest_text_handler,
        risk_level=ToolRiskLevel.DRAFT,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.SOFT_CONFIRM,
        enabled=enabled,
    ))

    def search_documents_handler(params: Dict[str, Any]) -> str:
        if not enabled:
            return "Ingestion disabled"
        query = str(params.get("query") or "").strip()
        if not query:
            return _jdump({"error": "Missing query"})
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, path, substr(content, 1, 200) as preview FROM documents WHERE content LIKE ? LIMIT 20",
                (f"%{query}%",)
            ).fetchall()
            results = [{"id": row["id"], "path": row["path"], "preview": row["preview"]} for row in rows]
            conn.close()
            return _jdump({"ok": True, "count": len(results), "results": results})
        except Exception as e:
            return _jdump({"ok": False, "error": str(e)})

    registry.register(ToolSpec(
        name="search_documents",
        description="Search ingested documents.",
        pack="ingestion_tools",
        tags=["ingestion", "documents"],
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        handler=search_documents_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))
