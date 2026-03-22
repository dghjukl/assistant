"""
EOS — Goal / Intent Persistence
Durable store for the entity's current goals and working intentions.

Why this exists
---------------
FocusEngine tracks attention weights (what the entity is thinking about *right now*).
Long-term memory stores what has happened.
Goals track what the entity is *committed to doing* — explicit intentions that
span sessions and don't decay automatically.

A goal can be:
  - created by the entity during conversation (via GoalStore.add_goal())
  - created by the reflection/initiative pipeline
  - completed, paused, or abandoned as circumstances change

The active goal list is injected into the system prompt so the entity always
knows what it is working toward, without needing to search memory.

Goal statuses
-------------
  active     — currently being pursued
  paused     — temporarily set aside (can resume)
  completed  — successfully achieved
  abandoned  — decided against / no longer relevant

Priority
--------
  high    — urgent or time-sensitive
  normal  — standard working goal  (default)
  low     — aspirational or background

Storage
-------
SQLite table ``entity_goals`` in the main entity_state.db.  The table is
created by calling ``GoalStore.init_table()`` once at startup (called
automatically from ``GoalStore.__init__()``).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Any, Optional

logger = logging.getLogger("eos.intent")

_VALID_STATUSES   = frozenset({"active", "paused", "completed", "abandoned"})
_VALID_PRIORITIES = frozenset({"high", "normal", "low"})

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS entity_goals (
    goal_id        TEXT PRIMARY KEY,
    description    TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',
    priority       TEXT NOT NULL DEFAULT 'normal',
    created_at     REAL NOT NULL,
    last_updated_at REAL NOT NULL,
    context        TEXT,
    source         TEXT NOT NULL DEFAULT 'entity',
    completion_note TEXT
);
"""


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class Goal:
    goal_id:         str
    description:     str
    status:          str
    priority:        str
    created_at:      float
    last_updated_at: float
    context:         Optional[str]
    source:          str
    completion_note: Optional[str]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "Goal":
        return cls(
            goal_id         = row["goal_id"],
            description     = row["description"],
            status          = row["status"],
            priority        = row["priority"],
            created_at      = float(row["created_at"]),
            last_updated_at = float(row["last_updated_at"]),
            context         = row["context"],
            source          = row["source"],
            completion_note = row["completion_note"],
        )


# ── Store ─────────────────────────────────────────────────────────────────────

class GoalStore:
    """
    SQLite-backed goal registry.

    Parameters
    ----------
    db_path : str
        Path to the entity_state.db SQLite file.
        Obtained from cfg["db_path"] or default "data/entity_state.db".
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._init_table()

    # ── Schema ────────────────────────────────────────────────────────────────

    def _init_table(self) -> None:
        from core.memory import get_db
        with get_db() as conn:
            conn.executescript(_INIT_SQL)
            conn.commit()

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def add_goal(
        self,
        description: str,
        priority: str = "normal",
        context: str = "",
        source: str = "entity",
    ) -> str:
        """
        Create a new active goal.

        Returns the goal_id of the created goal.
        """
        if priority not in _VALID_PRIORITIES:
            priority = "normal"

        goal_id = str(uuid.uuid4())
        now = time.time()

        from core.memory import get_db
        with get_db() as conn:
            conn.execute(
                """INSERT INTO entity_goals
                   (goal_id, description, status, priority, created_at, last_updated_at,
                    context, source, completion_note)
                   VALUES (?, ?, 'active', ?, ?, ?, ?, ?, NULL)""",
                (goal_id, description.strip(), priority, now, now,
                 context.strip() or None, source),
            )
            conn.commit()

        logger.info("[intent] New goal: [%s] %r (priority=%s)", goal_id[:8], description[:60], priority)
        return goal_id

    def complete_goal(self, goal_id: str, note: str = "") -> bool:
        """Mark a goal as completed.  Returns True if the goal was found."""
        return self._transition(goal_id, "completed", note)

    def abandon_goal(self, goal_id: str, reason: str = "") -> bool:
        """Mark a goal as abandoned.  Returns True if the goal was found."""
        return self._transition(goal_id, "abandoned", reason)

    def pause_goal(self, goal_id: str) -> bool:
        """Pause an active goal.  Returns True if the goal was found."""
        return self._transition(goal_id, "paused")

    def resume_goal(self, goal_id: str) -> bool:
        """Resume a paused goal.  Returns True if the goal was found."""
        return self._transition(goal_id, "active")

    def update_description(self, goal_id: str, description: str) -> bool:
        """Update the description of an existing goal."""
        from core.memory import get_db
        with get_db() as conn:
            cur = conn.execute(
                "UPDATE entity_goals SET description=?, last_updated_at=? WHERE goal_id=?",
                (description.strip(), time.time(), goal_id),
            )
            conn.commit()
        return cur.rowcount > 0

    # ── Queries ───────────────────────────────────────────────────────────────

    def active_goals(self) -> list[Goal]:
        """Return all active goals, high priority first."""
        from core.memory import get_db
        with get_db() as conn:
            rows = conn.execute(
                """SELECT * FROM entity_goals WHERE status='active'
                   ORDER BY
                     CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                     created_at ASC"""
            ).fetchall()
        return [Goal.from_row(r) for r in rows]

    def get_goal(self, goal_id: str) -> Optional[Goal]:
        from core.memory import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM entity_goals WHERE goal_id=?", (goal_id,)
            ).fetchone()
        return Goal.from_row(row) if row else None

    def all_goals(self, limit: int = 30, include_closed: bool = True) -> list[Goal]:
        """Return recent goals across all statuses."""
        from core.memory import get_db
        where = "" if include_closed else "WHERE status IN ('active','paused')"
        with get_db() as conn:
            rows = conn.execute(
                f"SELECT * FROM entity_goals {where} ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [Goal.from_row(r) for r in rows]

    def active_count(self) -> int:
        from core.memory import get_db
        with get_db() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM entity_goals WHERE status='active'"
            ).fetchone()[0]

    # ── Prompt block ──────────────────────────────────────────────────────────

    def prompt_block(self) -> str:
        """
        Return a concise block for injection into the system prompt.

        Returns an empty string when there are no active goals.
        Only active goals are shown; completed/abandoned goals live in history.
        """
        active = self.active_goals()
        if not active:
            return ""

        lines = ["## Current Goals"]
        priority_icon = {"high": "⚑ ", "normal": "", "low": "↓ "}
        for g in active:
            icon = priority_icon.get(g.priority, "")
            lines.append(f"  [{g.goal_id[:8]}] {icon}{g.description}")
            if g.context:
                lines.append(f"    context: {g.context[:120]}")

        return "\n".join(lines)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _transition(self, goal_id: str, new_status: str, note: str = "") -> bool:
        from core.memory import get_db
        with get_db() as conn:
            cur = conn.execute(
                """UPDATE entity_goals
                   SET status=?, last_updated_at=?, completion_note=?
                   WHERE goal_id=?""",
                (new_status, time.time(), note.strip() or None, goal_id),
            )
            conn.commit()
        found = cur.rowcount > 0
        if found:
            logger.info("[intent] Goal %s → %s", goal_id[:8], new_status)
        return found
