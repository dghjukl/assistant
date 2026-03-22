"""
EOS — Entity Lifecycle Service
Durable, machine-readable self-lifecycle awareness for the entity.

Why this exists
---------------
Without this, every boot is structurally an amnesiac present moment.  The
entity may accumulate memories through the memory system, but it has no
factual, deterministic knowledge of:
  - how many times it has started
  - how long it has been running in total
  - whether its last shutdown was clean or a crash
  - what version it was when it first initialised

This module provides exactly that — not inferred from memory text, but
written and read as structured operational record.

Design principle
----------------
Do NOT make the model infer its own lifecycle from memory text.
The runtime determines lifecycle state deterministically, then exposes it
to the model as trusted context via the system prompt.

Fields
------
  entity_id              — UUID4, assigned once at first_run, stable forever
  first_initialized_at   — ISO timestamp of very first boot
  last_start_at          — ISO timestamp of most recent boot
  last_shutdown_at       — ISO timestamp of last CLEAN shutdown (None if unknown)
  boot_count             — total number of times record_boot() has been called
  restart_count          — boots after the first (boot_count - 1)
  unclean_shutdown_count — boots whose prior session had no shutdown record
  total_runtime_seconds  — cumulative seconds across all completed sessions
  version_at_first_init  — EOS version string captured on first_run
  current_version        — EOS version string on this boot

Derived on each boot
--------------------
  boot_reason: "first_run" | "clean_restart" | "unclean_restart" | "version_upgrade"

  version_upgrade is set when current_version != version_at_first_init AND it
  is not first_run.  It stacks with clean/unclean: a version upgrade after a
  clean shutdown is "version_upgrade" (not "clean_restart").

Storage
-------
  Single JSON file: data/entity_lifecycle.json  (path from cfg["lifecycle_path"]
  or default).  Written atomically on every boot and clean shutdown.

Usage
-----
    from runtime.entity_lifecycle import EntityLifecycleService

    lifecycle = EntityLifecycleService(cfg, crash_report=recovery.record_boot())
    # system prompt assembly:
    prompt_block = lifecycle.lifecycle_summary()

    # at clean shutdown:
    lifecycle.record_shutdown()
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("eos.entity_lifecycle")

UTC = timezone.utc

# ── Version resolution ────────────────────────────────────────────────────────

def _current_version() -> str:
    """Read EOS version from VERSION file, pyproject.toml, or fallback string."""
    for candidate in [
        Path(__file__).parent.parent / "VERSION",
        Path(__file__).parent.parent / "version.txt",
    ]:
        if candidate.exists():
            try:
                return candidate.read_text().strip()
            except OSError:
                pass
    # Try pyproject.toml
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    if pyproject.exists():
        try:
            for line in pyproject.read_text().splitlines():
                if line.strip().startswith("version"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        return parts[1].strip().strip('"\'')
        except OSError:
            pass
    return "unknown"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class LifecycleRecord:
    """Persisted lifecycle state — written to disk on each boot and shutdown."""
    entity_id:               str
    first_initialized_at:    str
    version_at_first_init:   str

    # Updated on every boot
    last_start_at:           str
    current_version:         str
    boot_count:              int
    restart_count:           int
    unclean_shutdown_count:  int

    # Updated on clean shutdown
    last_shutdown_at:        Optional[str]
    total_runtime_seconds:   float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LifecycleRecord":
        return cls(
            entity_id              = d["entity_id"],
            first_initialized_at   = d["first_initialized_at"],
            version_at_first_init  = d["version_at_first_init"],
            last_start_at          = d["last_start_at"],
            current_version        = d.get("current_version", "unknown"),
            boot_count             = int(d.get("boot_count", 1)),
            restart_count          = int(d.get("restart_count", 0)),
            unclean_shutdown_count = int(d.get("unclean_shutdown_count", 0)),
            last_shutdown_at       = d.get("last_shutdown_at"),
            total_runtime_seconds  = float(d.get("total_runtime_seconds", 0.0)),
        )


@dataclass
class LifecycleSummary:
    """
    Derived view of lifecycle state, computed fresh on each boot.
    Passed to build_system_prompt() for context injection.
    """
    entity_id:              str
    first_initialized_at:   str
    last_start_at:          str
    last_shutdown_at:       Optional[str]
    boot_count:             int
    restart_count:          int
    unclean_shutdown_count: int
    total_runtime_seconds:  float
    version_at_first_init:  str
    current_version:        str
    boot_reason:            str   # first_run | clean_restart | unclean_restart | version_upgrade

    def to_dict(self) -> dict:
        return asdict(self)

    def compact(self) -> str:
        """
        One-liner suitable for injection into the system prompt.

        Example:
          Boot #4 (clean_restart) | Total runtime: 3h 42m | First init: 2025-11-02
        """
        h = int(self.total_runtime_seconds // 3600)
        m = int((self.total_runtime_seconds % 3600) // 60)
        runtime_str = f"{h}h {m}m" if h else f"{m}m"

        try:
            first_date = self.first_initialized_at[:10]   # YYYY-MM-DD
        except Exception:
            first_date = "unknown"

        parts = [
            f"Boot #{self.boot_count} ({self.boot_reason})",
            f"Total runtime: {runtime_str}",
            f"First init: {first_date}",
        ]
        if self.unclean_shutdown_count:
            parts.append(f"Unclean shutdowns: {self.unclean_shutdown_count}")
        if self.current_version != "unknown":
            parts.append(f"Version: {self.current_version}")
        return " | ".join(parts)

    def prompt_block(self) -> str:
        """
        Multi-line block for injection into the system prompt.
        Gives the model the factual operational context it needs without
        encouraging mystical interpretation.
        """
        lines = [
            "## Operational History",
            f"Boot number:      {self.boot_count}  (restarts: {self.restart_count})",
            f"Boot reason:      {self.boot_reason}",
            f"This session:     started {self.last_start_at}",
            f"First ever init:  {self.first_initialized_at}",
        ]
        if self.last_shutdown_at:
            lines.append(f"Last shutdown:    {self.last_shutdown_at} (clean)")
        else:
            lines.append("Last shutdown:    unknown (first run or prior crash)")
        h = int(self.total_runtime_seconds // 3600)
        m = int((self.total_runtime_seconds % 3600) // 60)
        runtime_str = f"{h}h {m}m" if h else (f"{m}m" if m else "<1m")
        lines.append(f"Total runtime:    {runtime_str} across all sessions")
        if self.unclean_shutdown_count:
            lines.append(
                f"Unclean shutdowns: {self.unclean_shutdown_count} "
                "(sessions that ended without a clean shutdown record)"
            )
        if self.current_version != "unknown":
            v_note = ""
            if self.current_version != self.version_at_first_init:
                v_note = f"  (was {self.version_at_first_init} at first init)"
            lines.append(f"Version:          {self.current_version}{v_note}")
        return "\n".join(lines)


# ── Service ───────────────────────────────────────────────────────────────────

class EntityLifecycleService:
    """
    Manages the entity's persistent operational history.

    Parameters
    ----------
    cfg : dict
        Runtime config dict.  Reads ``lifecycle_path`` (default:
        ``data/entity_lifecycle.json``).
    crash_report : Any | None
        RecoveryReport from CrashRecoveryService.record_boot().  Used to
        determine whether the previous session ended cleanly.
        If None, prior shutdown state is treated as unknown.
    """

    def __init__(
        self,
        cfg: dict,
        crash_report: Any = None,
    ) -> None:
        self._path = Path(
            cfg.get("lifecycle_path", "data/entity_lifecycle.json")
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._boot_start_monotonic: float = time.monotonic()
        self._summary: Optional[LifecycleSummary] = None

        version = _current_version()
        self._record = self._boot(version, crash_report)

    # ── Public API ────────────────────────────────────────────────────────────

    def lifecycle_summary(self) -> LifecycleSummary:
        """Return the LifecycleSummary computed at boot. Immutable for this session."""
        assert self._summary is not None, "EntityLifecycleService not yet initialised"
        return self._summary

    def record_shutdown(self) -> None:
        """
        Write a clean shutdown timestamp and update total_runtime_seconds.
        Call this from the FastAPI shutdown event *before* servers stop.
        """
        elapsed = time.monotonic() - self._boot_start_monotonic
        self._record.total_runtime_seconds += elapsed
        self._record.last_shutdown_at = _now_iso()
        self._write(self._record)
        logger.info(
            "[entity_lifecycle] Clean shutdown recorded. "
            "Session runtime: %.0fs. Total: %.0fs.",
            elapsed, self._record.total_runtime_seconds,
        )

    def to_dict(self) -> dict:
        """Full record as dict, for admin API."""
        d = self._record.to_dict()
        if self._summary:
            d["boot_reason"] = self._summary.boot_reason
            # Append in-progress session estimate
            d["current_session_seconds"] = round(
                time.monotonic() - self._boot_start_monotonic, 1
            )
        return d

    # ── Boot logic ────────────────────────────────────────────────────────────

    def _boot(self, version: str, crash_report: Any) -> LifecycleRecord:
        existing = self._load()

        if existing is None:
            # ── First run ──────────────────────────────────────────────────
            now = _now_iso()
            record = LifecycleRecord(
                entity_id              = str(uuid.uuid4()),
                first_initialized_at   = now,
                version_at_first_init  = version,
                last_start_at          = now,
                current_version        = version,
                boot_count             = 1,
                restart_count          = 0,
                unclean_shutdown_count = 0,
                last_shutdown_at       = None,
                total_runtime_seconds  = 0.0,
            )
            boot_reason = "first_run"
            logger.info(
                "[entity_lifecycle] First run. entity_id=%s version=%s",
                record.entity_id, version,
            )
        else:
            # ── Subsequent boot ───────────────────────────────────────────
            prior_clean = self._was_prior_shutdown_clean(existing, crash_report)

            # Determine unclean count increment
            unclean_inc = 0 if prior_clean else 1

            # Determine boot reason
            if version != existing.version_at_first_init:
                boot_reason = "version_upgrade"
            elif prior_clean:
                boot_reason = "clean_restart"
            else:
                boot_reason = "unclean_restart"

            record = LifecycleRecord(
                entity_id              = existing.entity_id,
                first_initialized_at   = existing.first_initialized_at,
                version_at_first_init  = existing.version_at_first_init,
                last_start_at          = _now_iso(),
                current_version        = version,
                boot_count             = existing.boot_count + 1,
                restart_count          = existing.restart_count + 1,
                unclean_shutdown_count = existing.unclean_shutdown_count + unclean_inc,
                last_shutdown_at       = existing.last_shutdown_at if prior_clean else None,
                total_runtime_seconds  = existing.total_runtime_seconds,
            )
            logger.info(
                "[entity_lifecycle] Boot #%d (%s). entity_id=%s version=%s "
                "total_runtime=%.0fs unclean=%d",
                record.boot_count, boot_reason, record.entity_id, version,
                record.total_runtime_seconds, record.unclean_shutdown_count,
            )

        self._write(record)

        self._summary = LifecycleSummary(
            entity_id              = record.entity_id,
            first_initialized_at   = record.first_initialized_at,
            last_start_at          = record.last_start_at,
            last_shutdown_at       = record.last_shutdown_at,
            boot_count             = record.boot_count,
            restart_count          = record.restart_count,
            unclean_shutdown_count = record.unclean_shutdown_count,
            total_runtime_seconds  = record.total_runtime_seconds,
            version_at_first_init  = record.version_at_first_init,
            current_version        = record.current_version,
            boot_reason            = boot_reason,
        )
        return record

    @staticmethod
    def _was_prior_shutdown_clean(record: LifecycleRecord, crash_report: Any) -> bool:
        """
        Determine whether the prior session ended cleanly.

        Priority:
          1. CrashRecoveryService report (most authoritative — checks the
             shutdown ledger for a matching shutdown record).
          2. Fall back to whether last_shutdown_at is set in the lifecycle
             record (less precise — does not detect ledger mismatches).
        """
        if crash_report is not None:
            try:
                # RecoveryReport.prior_shutdown_kind:
                #   ShutdownKind.CLEAN / UNCLEAN / UNKNOWN
                kind = getattr(crash_report, "prior_shutdown_kind", None)
                if kind is not None:
                    kind_str = str(kind).upper()
                    if "CLEAN" in kind_str and "UNCLEAN" not in kind_str:
                        return True
                    if "UNCLEAN" in kind_str:
                        return False
                    # UNKNOWN — fall through to lifecycle record check
            except Exception as exc:
                logger.debug("[entity_lifecycle] crash_report check failed: %s", exc)

        # Fallback: if last_shutdown_at is present in the lifecycle record,
        # treat it as clean (it was written by record_shutdown()).
        return record.last_shutdown_at is not None

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> Optional[LifecycleRecord]:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return LifecycleRecord.from_dict(data)
        except Exception as exc:
            logger.warning("[entity_lifecycle] Failed to load %s: %s", self._path, exc)
            return None

    def _write(self, record: LifecycleRecord) -> None:
        tmp = self._path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(record.to_dict(), indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception as exc:
            logger.error("[entity_lifecycle] Failed to write %s: %s", self._path, exc)
