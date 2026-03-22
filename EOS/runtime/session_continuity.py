"""
EOS — Session Continuity Service
Preserves a compact excerpt of the previous conversation so the entity
can be primed with "where we left off" at the start of each new session.

Design
------
At shutdown, the last N conversation turns are read from the interaction_log
and written to disk as a structured JSON file.  At boot these turns are read
back and exposed as ``session_primer()`` for injection into the system prompt.

No LLM is involved.  The primer is built directly from raw turn records —
cheap, deterministic, independent of any model being available at shutdown.

Injected block format
---------------------
    ## Previous Session  (2026-01-15 · 12 turns)
    You: [user message, truncated to 150 chars]
    Me:  [entity reply, truncated to 220 chars]
    ...
    (8 of 12 turns shown)

This gives the entity enough context to resume naturally without flooding
the prompt with full history (long-term memory retrieval handles depth).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("eos.session_continuity")
UTC = timezone.utc


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


# ── Persisted record ──────────────────────────────────────────────────────────

@dataclass
class SessionRecord:
    """On-disk representation of a completed session's excerpt."""
    session_ended_at: str
    turn_count: int       # total interactions in the session (not just excerpt)
    excerpt: list[dict]   # [{"role": "user"|"assistant", "content": str}]
    boot_count: int = 0   # lifecycle boot_count at end (for display context)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SessionRecord":
        return cls(
            session_ended_at = d.get("session_ended_at", "unknown"),
            turn_count       = int(d.get("turn_count", 0)),
            excerpt          = d.get("excerpt", []),
            boot_count       = int(d.get("boot_count", 0)),
        )


# ── Service ───────────────────────────────────────────────────────────────────

class SessionContinuityService:
    """
    Saves and restores a compact conversation excerpt across sessions.

    Parameters
    ----------
    cfg : dict
        Runtime config.  Reads ``session_continuity_path``
        (default: ``data/session_continuity.json``).
    excerpt_turns : int
        Number of most-recent exchange pairs to preserve (default 4).
    max_chars_user : int
        Max characters per user message in the excerpt (default 150).
    max_chars_entity : int
        Max characters per entity reply in the excerpt (default 220).
    """

    def __init__(
        self,
        cfg: dict,
        excerpt_turns: int = 4,
        max_chars_user: int = 150,
        max_chars_entity: int = 220,
    ) -> None:
        self._path = Path(
            cfg.get("session_continuity_path", "data/session_continuity.json")
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._excerpt_turns = excerpt_turns
        self._max_u = max_chars_user
        self._max_e = max_chars_entity
        self._prior: Optional[SessionRecord] = self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def save_session_end(
        self,
        recent_turns: list[dict],
        total_turn_count: int | None = None,
        boot_count: int = 0,
    ) -> None:
        """
        Write a session continuity record at clean shutdown.

        Parameters
        ----------
        recent_turns : list[dict]
            Most-recent turns from ``get_recent_interactions()``.
            Each dict must have ``role`` and ``content`` keys.
        total_turn_count : int | None
            Total turn count for this session.  Falls back to len(recent_turns).
        boot_count : int
            Lifecycle boot_count at session end.
        """
        if not recent_turns:
            return

        # Take only the last 2*excerpt_turns messages (N exchange pairs)
        tail = recent_turns[-(self._excerpt_turns * 2):]
        excerpt: list[dict] = []
        for msg in tail:
            role    = msg.get("role", "")
            content = msg.get("content", "").strip()
            if not content or role not in ("user", "assistant"):
                continue
            max_c = self._max_e if role == "assistant" else self._max_u
            excerpt.append({"role": role, "content": _truncate(content, max_c)})

        if not excerpt:
            return

        record = SessionRecord(
            session_ended_at = _now_iso(),
            turn_count       = total_turn_count if total_turn_count is not None
                               else len(recent_turns),
            excerpt          = excerpt,
            boot_count       = boot_count,
        )
        self._write(record)
        logger.info(
            "[session_continuity] Session saved (%d turns, %d excerpt messages).",
            record.turn_count, len(record.excerpt),
        )

    def session_primer(self) -> str:
        """
        Return a formatted block for injection into the system prompt.

        Returns an empty string when no prior session record exists.
        The block is deliberately compact: it primes recollection without
        duplicating what long-term memory retrieval will surface.
        """
        if not self.has_prior_session():
            return ""

        assert self._prior is not None  # guaranteed by has_prior_session()
        try:
            ended = self._prior.session_ended_at[:10]   # YYYY-MM-DD
        except Exception:
            ended = "unknown"

        lines = [
            f"## Previous Session  ({ended} · {self._prior.turn_count} turns)",
        ]
        for msg in self._prior.excerpt:
            role    = msg.get("role", "")
            content = msg.get("content", "")
            prefix  = "You:" if role == "user" else "Me: "
            lines.append(f"{prefix} {content}")

        shown = len(self._prior.excerpt)
        total = self._prior.turn_count * 2   # rough message count
        if shown < total:
            lines.append(f"(showing last {shown} messages of this session)")

        return "\n".join(lines)

    def has_prior_session(self) -> bool:
        return self._prior is not None and bool(self._prior.excerpt)

    def to_dict(self) -> dict:
        """Return the prior session record as a plain dict (for admin API)."""
        if self._prior is None:
            return {"has_prior_session": False}
        return {"has_prior_session": True, **self._prior.to_dict()}

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> Optional[SessionRecord]:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return SessionRecord.from_dict(data)
        except Exception as exc:
            logger.warning("[session_continuity] Failed to load %s: %s", self._path, exc)
            return None

    def _write(self, record: SessionRecord) -> None:
        tmp = self._path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(record.to_dict(), indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception as exc:
            logger.error("[session_continuity] Failed to write %s: %s", self._path, exc)
