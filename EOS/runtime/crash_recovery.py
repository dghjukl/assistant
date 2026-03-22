"""
EOS — Crash Recovery Service
Tracks shutdown and startup events to detect unclean shutdowns (crashes).

Problem this solves
-------------------
Without shutdown tracking, EOS cannot distinguish between a clean restart
and a crash recovery.  After a crash the system may have stale locks,
partially written memory, or inconsistent state.  This module gives EOS
awareness of its own shutdown history and reports the situation at boot.

How it works
------------
1. At every startup ``CrashRecoveryService.record_boot()`` is called.
   It reads the ledger; if the previous boot has no matching shutdown record,
   the prior session is flagged as UNCLEAN.

2. At every clean shutdown ``record_shutdown()`` writes a ShutdownRecord
   before servers stop.  This is called from the FastAPI ``shutdown`` event.

3. ``get_recovery_report()`` returns a structured ``RecoveryReport`` that
   callers (admin API, entity system prompt) can inspect.

Ledger file format
------------------
A single JSON file (default: ``data/shutdown_ledger.json``) containing a
``records`` list.  Each entry is a BootRecord or ShutdownRecord:

    {"record_type": "boot",     "boot_id": "...", "boot_at": "...", ...}
    {"record_type": "shutdown", "boot_id": "...", "shutdown_at": "...", ...}

The ledger is append-only.  Old records beyond ``max_records`` are trimmed.

Usage
-----
    from runtime.crash_recovery import CrashRecoveryService

    recovery = CrashRecoveryService(cfg)
    report = recovery.record_boot()          # call at startup
    recovery.record_shutdown()               # call at clean shutdown

    report = recovery.get_recovery_report()
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("eos.crash_recovery")
UTC = timezone.utc

_MAX_RECORDS = 200   # trim ledger beyond this


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# ── Record types ──────────────────────────────────────────────────────────────

@dataclass
class BootRecord:
    boot_id: str
    boot_at: str
    config_mode: str = ""
    record_type: str = "boot"


@dataclass
class ShutdownRecord:
    boot_id: str         # references the BootRecord this closes
    shutdown_at: str
    clean: bool = True
    record_type: str = "shutdown"


# ── Recovery report ───────────────────────────────────────────────────────────

class ShutdownKind:
    CLEAN   = "clean"
    UNCLEAN = "unclean"
    UNKNOWN = "unknown"   # no previous boot record found


@dataclass
class RecoveryReport:
    boot_id: str
    boot_at: str
    previous_shutdown_kind: str      # ShutdownKind.*
    previous_boot_id: Optional[str]
    previous_boot_at: Optional[str]
    previous_shutdown_at: Optional[str]
    notes: list[str]

    def is_crash_recovery(self) -> bool:
        return self.previous_shutdown_kind == ShutdownKind.UNCLEAN

    def to_dict(self) -> dict:
        return {
            "boot_id": self.boot_id,
            "boot_at": self.boot_at,
            "previous_shutdown_kind": self.previous_shutdown_kind,
            "previous_boot_id": self.previous_boot_id,
            "previous_boot_at": self.previous_boot_at,
            "previous_shutdown_at": self.previous_shutdown_at,
            "is_crash_recovery": self.is_crash_recovery(),
            "notes": self.notes,
        }

    def admin_summary(self) -> str:
        if self.previous_shutdown_kind == ShutdownKind.UNKNOWN:
            return "Boot: first start (no prior ledger entry)"
        if self.is_crash_recovery():
            return (
                f"Boot: CRASH RECOVERY — previous session "
                f"(boot_id={self.previous_boot_id}) did not shut down cleanly"
            )
        return (
            f"Boot: clean start — previous session shut down at "
            f"{self.previous_shutdown_at}"
        )

    def model_summary(self) -> str:
        """One-line note for injection into system prompt."""
        if self.is_crash_recovery():
            return "[NOTE: previous session ended in an unclean shutdown — state may be partially stale]"
        return ""


# ── Ledger ────────────────────────────────────────────────────────────────────

class ShutdownLedger:
    """Reads and writes the shutdown/boot ledger JSON file."""

    def __init__(self, ledger_path: Path) -> None:
        self._path = ledger_path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("records", []) if isinstance(data, dict) else []
        except Exception as e:
            logger.warning("Could not read ledger (%s): %s", self._path, e)
            return []

    def _save(self, records: list[dict]) -> None:
        # Trim to max
        if len(records) > _MAX_RECORDS:
            records = records[-_MAX_RECORDS:]
        try:
            with self._path.open("w", encoding="utf-8") as f:
                json.dump({"records": records}, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning("Could not write ledger (%s): %s", self._path, e)

    def append_boot(self, rec: BootRecord) -> None:
        records = self._load()
        records.append(asdict(rec))
        self._save(records)

    def append_shutdown(self, rec: ShutdownRecord) -> None:
        records = self._load()
        records.append(asdict(rec))
        self._save(records)

    def get_last_boot_and_shutdown(self) -> tuple[Optional[dict], Optional[dict]]:
        """
        Return (last_boot_record, last_shutdown_record) where last_shutdown
        is the one matching the last boot (i.e. closing the same boot_id).
        """
        records = self._load()
        last_boot: Optional[dict] = None
        for r in reversed(records):
            if r.get("record_type") == "boot":
                last_boot = r
                break
        if last_boot is None:
            return None, None

        boot_id = last_boot.get("boot_id")
        matching_shutdown: Optional[dict] = None
        for r in reversed(records):
            if r.get("record_type") == "shutdown" and r.get("boot_id") == boot_id:
                matching_shutdown = r
                break
        return last_boot, matching_shutdown

    def get_previous_boot_and_shutdown(self, current_boot_id: str) -> tuple[Optional[dict], Optional[dict]]:
        """
        Return the boot record BEFORE current_boot_id and its matching shutdown.
        """
        records = self._load()
        # Find all boot records except current
        boots = [r for r in records if r.get("record_type") == "boot"
                 and r.get("boot_id") != current_boot_id]
        if not boots:
            return None, None
        prev_boot = boots[-1]
        prev_boot_id = prev_boot.get("boot_id")

        # Find shutdown matching the previous boot
        shutdown = None
        for r in records:
            if r.get("record_type") == "shutdown" and r.get("boot_id") == prev_boot_id:
                shutdown = r
        return prev_boot, shutdown


# ── CrashRecoveryService ──────────────────────────────────────────────────────

class CrashRecoveryService:
    """
    Top-level crash recovery coordinator.

    Manages boot/shutdown records and produces RecoveryReport at startup.

    Parameters
    ----------
    cfg : dict
        Runtime config.  Reads ``db_path`` to derive the ledger path, or
        ``crash_recovery.ledger_path`` for an explicit override.
    """

    def __init__(self, cfg: dict) -> None:
        self._cfg = cfg
        # Derive ledger path from db_path or explicit override
        recovery_cfg = cfg.get("crash_recovery", {})
        explicit = recovery_cfg.get("ledger_path")
        if explicit:
            ledger_path = Path(explicit)
        else:
            db = Path(cfg.get("db_path", "data/entity_state.db"))
            ledger_path = db.parent / "shutdown_ledger.json"

        self._ledger = ShutdownLedger(ledger_path)
        self._current_boot_id: Optional[str] = None
        self._report: Optional[RecoveryReport] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def record_boot(self, config_mode: str = "") -> RecoveryReport:
        """
        Record this boot in the ledger and return a RecoveryReport.
        Call once at startup before serving requests.
        """
        boot_id = str(uuid.uuid4())
        self._current_boot_id = boot_id

        # Read previous state before writing this boot
        prev_boot, prev_shutdown = self._ledger.get_previous_boot_and_shutdown(boot_id)

        # Write current boot record
        self._ledger.append_boot(BootRecord(
            boot_id=boot_id,
            boot_at=_now_iso(),
            config_mode=config_mode,
        ))

        # Determine shutdown kind of previous session
        notes = []
        if prev_boot is None:
            kind = ShutdownKind.UNKNOWN
            notes.append("No previous boot record found — likely first run.")
        elif prev_shutdown is None:
            kind = ShutdownKind.UNCLEAN
            notes.append(
                f"Previous boot (id={prev_boot.get('boot_id', '?')}, "
                f"at={prev_boot.get('boot_at', '?')}) has no matching shutdown record."
            )
            notes.append("State may be partially stale — recommend memory health check.")
        else:
            kind = ShutdownKind.CLEAN

        self._report = RecoveryReport(
            boot_id=boot_id,
            boot_at=_now_iso(),
            previous_shutdown_kind=kind,
            previous_boot_id=prev_boot.get("boot_id") if prev_boot else None,
            previous_boot_at=prev_boot.get("boot_at") if prev_boot else None,
            previous_shutdown_at=prev_shutdown.get("shutdown_at") if prev_shutdown else None,
            notes=notes,
        )

        if kind == ShutdownKind.UNCLEAN:
            logger.warning("[crash_recovery] UNCLEAN SHUTDOWN DETECTED: %s", notes[0])
        else:
            logger.info("[crash_recovery] Boot recorded (kind=%s)", kind)

        return self._report

    def record_shutdown(self) -> None:
        """
        Record a clean shutdown in the ledger.
        Call from the FastAPI ``shutdown`` lifespan event.
        """
        if not self._current_boot_id:
            logger.warning("[crash_recovery] record_shutdown() called before record_boot()")
            return
        self._ledger.append_shutdown(ShutdownRecord(
            boot_id=self._current_boot_id,
            shutdown_at=_now_iso(),
            clean=True,
        ))
        logger.info("[crash_recovery] Clean shutdown recorded (boot_id=%s)", self._current_boot_id)

    def get_recovery_report(self) -> Optional[RecoveryReport]:
        """Return the RecoveryReport from the most recent record_boot() call."""
        return self._report

    def is_crash_recovery(self) -> bool:
        """True if the current session started after an unclean shutdown."""
        return self._report.is_crash_recovery() if self._report else False
