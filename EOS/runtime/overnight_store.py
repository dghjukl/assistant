from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc

ACTIVE_STATUSES = ("scheduled", "active", "prewake")
FINAL_STATUSES = ("ended", "cancelled", "superseded")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None = None) -> str:
    value = dt or _utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class OvernightCycleRecord:
    id: str
    status: str
    declared_at: str
    away_start_time: str
    expected_return_time: str
    actual_return_time: str | None
    confidence: float
    source: str
    source_text: str
    is_one_off: bool
    parser_details: dict[str, Any]
    cancelled_at: str | None
    superseded_by_id: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "declared_at": self.declared_at,
            "away_start_time": self.away_start_time,
            "expected_return_time": self.expected_return_time,
            "actual_return_time": self.actual_return_time,
            "confidence": self.confidence,
            "source": self.source,
            "source_text": self.source_text,
            "is_one_off": self.is_one_off,
            "parser_details": dict(self.parser_details),
            "cancelled_at": self.cancelled_at,
            "superseded_by_id": self.superseded_by_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row | None) -> "OvernightCycleRecord | None":
        if row is None:
            return None
        details_raw = row["parser_details"]
        try:
            parser_details = json.loads(details_raw) if details_raw else {}
        except Exception:
            parser_details = {"raw": details_raw}
        return cls(
            id=row["id"],
            status=row["status"],
            declared_at=row["declared_at"],
            away_start_time=row["away_start_time"],
            expected_return_time=row["expected_return_time"],
            actual_return_time=row["actual_return_time"],
            confidence=float(row["confidence"] or 0.0),
            source=row["source"],
            source_text=row["source_text"],
            is_one_off=bool(row["is_one_off"]),
            parser_details=parser_details,
            cancelled_at=row["cancelled_at"],
            superseded_by_id=row["superseded_by_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class OvernightCycleStore:
    """Dedicated durable persistence for conversational overnight availability."""

    DDL = """
    CREATE TABLE IF NOT EXISTS overnight_cycles (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        declared_at TEXT NOT NULL,
        away_start_time TEXT NOT NULL,
        expected_return_time TEXT NOT NULL,
        actual_return_time TEXT,
        confidence REAL NOT NULL DEFAULT 0.0,
        source TEXT NOT NULL DEFAULT 'conversation',
        source_text TEXT NOT NULL DEFAULT '',
        is_one_off INTEGER NOT NULL DEFAULT 1,
        parser_details TEXT NOT NULL DEFAULT '{}',
        cancelled_at TEXT,
        superseded_by_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_overnight_cycles_status_updated
        ON overnight_cycles (status, updated_at DESC);

    CREATE INDEX IF NOT EXISTS idx_overnight_cycles_declared_at
        ON overnight_cycles (declared_at DESC);
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_schema(self) -> None:
        with self._lock:
            with self._conn() as conn:
                conn.executescript(self.DDL)
                conn.commit()

    def create_declaration(
        self,
        *,
        away_start_time: str,
        expected_return_time: str,
        confidence: float,
        source: str,
        source_text: str,
        declared_at: str | None = None,
        is_one_off: bool = True,
        parser_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = declared_at or _iso()
        record_id = "ONC-" + uuid.uuid4().hex[:10]
        parser_payload = json.dumps(parser_details or {}, ensure_ascii=False)

        with self._lock:
            with self._conn() as conn:
                current = self._fetch_current_conn(conn)
                if current is not None:
                    conn.execute(
                        """
                        UPDATE overnight_cycles
                        SET status='superseded', superseded_by_id=?, updated_at=?
                        WHERE id=?
                        """,
                        (record_id, now, current["id"]),
                    )
                conn.execute(
                    """
                    INSERT INTO overnight_cycles (
                        id, status, declared_at, away_start_time, expected_return_time,
                        actual_return_time, confidence, source, source_text, is_one_off,
                        parser_details, cancelled_at, superseded_by_id, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        record_id,
                        "scheduled",
                        now,
                        away_start_time,
                        expected_return_time,
                        None,
                        float(confidence),
                        source,
                        source_text,
                        int(is_one_off),
                        parser_payload,
                        None,
                        None,
                        now,
                        now,
                    ),
                )
                conn.commit()
        return self.get(record_id) or {}

    def get(self, record_id: str) -> dict[str, Any] | None:
        with self._lock:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM overnight_cycles WHERE id=?",
                    (record_id,),
                ).fetchone()
        record = OvernightCycleRecord.from_row(row)
        return record.to_dict() if record is not None else None

    def fetch_current(self) -> dict[str, Any] | None:
        with self._lock:
            with self._conn() as conn:
                row = self._fetch_current_conn(conn)
        record = OvernightCycleRecord.from_row(row)
        return record.to_dict() if record is not None else None

    def _fetch_current_conn(self, conn: sqlite3.Connection) -> sqlite3.Row | None:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        return conn.execute(
            f"SELECT * FROM overnight_cycles WHERE status IN ({placeholders}) ORDER BY updated_at DESC LIMIT 1",
            ACTIVE_STATUSES,
        ).fetchone()

    def update_status(
        self,
        record_id: str,
        status: str,
        *,
        updated_at: str | None = None,
    ) -> dict[str, Any] | None:
        now = updated_at or _iso()
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE overnight_cycles SET status=?, updated_at=? WHERE id=?",
                    (status, now, record_id),
                )
                conn.commit()
        return self.get(record_id)

    def mark_return(
        self,
        record_id: str,
        *,
        actual_return_time: str | None = None,
        status: str = "ended",
    ) -> dict[str, Any] | None:
        now = actual_return_time or _iso()
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    """
                    UPDATE overnight_cycles
                    SET actual_return_time=?, status=?, updated_at=?
                    WHERE id=?
                    """,
                    (now, status, now, record_id),
                )
                conn.commit()
        return self.get(record_id)

    def cancel_current(self, *, cancelled_at: str | None = None) -> dict[str, Any] | None:
        now = cancelled_at or _iso()
        with self._lock:
            with self._conn() as conn:
                row = self._fetch_current_conn(conn)
                if row is None:
                    return None
                conn.execute(
                    """
                    UPDATE overnight_cycles
                    SET status='cancelled', cancelled_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (now, now, row["id"]),
                )
                conn.commit()
                record_id = row["id"]
        return self.get(record_id)

    def update_expected_return_time(
        self,
        record_id: str,
        expected_return_time: str,
        *,
        updated_at: str | None = None,
    ) -> dict[str, Any] | None:
        now = updated_at or _iso()
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    """
                    UPDATE overnight_cycles
                    SET expected_return_time=?, updated_at=?
                    WHERE id=?
                    """,
                    (expected_return_time, now, record_id),
                )
                conn.commit()
        return self.get(record_id)

    def recent_history(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM overnight_cycles ORDER BY declared_at DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
        return [record.to_dict() for record in (OvernightCycleRecord.from_row(row) for row in rows) if record is not None]
