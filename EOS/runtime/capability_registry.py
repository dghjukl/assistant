"""
EOS — Capability Registry
Unified registry for all system capabilities and their health status.

Provides a single source of truth for what the system can do and whether
each capability is currently available.  Replaces the previous pattern of
checking topology for models and tool_states for tools separately.

Capability kinds
----------------
  MODEL       — llama-server backend (primary, thinking, tool, vision, utility)
  TOOL        — individual tool in the ToolRegistry
  TOOLPACK    — toolpack (group of tools)
  AUDIO       — STT / TTS subsystems
  MEMORY      — SQLite + ChromaDB stores
  INITIATIVE  — initiative engine
  REFLECTION  — reflection pipeline
  SCHEDULER   — background scheduler loop
  CONNECTOR   — external connector (Discord, Google, etc.)

Capability statuses
-------------------
  ENABLED   — configured, healthy, ready to use
  DISABLED  — configured but turned off
  DEGRADED  — available but performing poorly or with reduced capability
  OFFLINE   — unreachable / failed
  UNKNOWN   — not yet probed

Usage
-----
    from runtime.capability_registry import CapabilityRegistry, CapabilityEntry, CapabilityKind, CapabilityStatus

    reg = CapabilityRegistry()
    reg.register(CapabilityEntry(
        name="primary",
        kind=CapabilityKind.MODEL,
        status=CapabilityStatus.ENABLED,
        healthy=True,
        policy="required",
        version="qwen3-8b",
    ))

    entry = reg.get("primary")
    summary = reg.health_summary()   # dict for admin API
    all_caps = reg.all()             # list[CapabilityEntry]

    # Update status in place (e.g. after a probe)
    reg.set_status("primary", CapabilityStatus.DEGRADED, "high latency")
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

UTC = timezone.utc


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# ── Enums (string-based for easy JSON serialisation) ─────────────────────────

class CapabilityKind:
    MODEL     = "model"
    TOOL      = "tool"
    TOOLPACK  = "toolpack"
    AUDIO     = "audio"
    MEMORY    = "memory"
    INITIATIVE = "initiative"
    REFLECTION = "reflection"
    SCHEDULER  = "scheduler"
    CONNECTOR  = "connector"


class CapabilityStatus:
    ENABLED  = "enabled"
    DISABLED = "disabled"
    DEGRADED = "degraded"
    OFFLINE  = "offline"
    UNKNOWN  = "unknown"


# ── Entry dataclass ───────────────────────────────────────────────────────────

@dataclass
class CapabilityEntry:
    name: str
    kind: str           # CapabilityKind.*
    status: str         # CapabilityStatus.*
    healthy: bool
    policy: str         # "required" | "optional" | "disabled"
    version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    last_health_check: Optional[str] = None
    health_message: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name":               self.name,
            "kind":               self.kind,
            "status":             self.status,
            "healthy":            self.healthy,
            "policy":             self.policy,
            "version":            self.version,
            "metadata":           self.metadata,
            "last_health_check":  self.last_health_check,
            "health_message":     self.health_message,
        }


# ── Registry ──────────────────────────────────────────────────────────────────

class CapabilityRegistry:
    """Thread-safe registry of all system capabilities."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, CapabilityEntry] = {}

    # ── Registration ──────────────────────────────────────────────────────────

    def register(self, entry: CapabilityEntry) -> None:
        """Register or replace a capability entry."""
        with self._lock:
            self._entries[entry.name] = entry

    def register_many(self, entries: list[CapabilityEntry]) -> None:
        with self._lock:
            for e in entries:
                self._entries[e.name] = e

    # ── Retrieval ─────────────────────────────────────────────────────────────

    def get(self, name: str) -> Optional[CapabilityEntry]:
        with self._lock:
            return self._entries.get(name)

    def all(self) -> list[CapabilityEntry]:
        with self._lock:
            return list(self._entries.values())

    def by_kind(self, kind: str) -> list[CapabilityEntry]:
        with self._lock:
            return [e for e in self._entries.values() if e.kind == kind]

    # ── Status mutation ───────────────────────────────────────────────────────

    def set_status(
        self,
        name: str,
        status: str,
        health_message: str = "",
    ) -> bool:
        """Update a capability's status. Returns True if entry exists."""
        with self._lock:
            entry = self._entries.get(name)
            if entry is None:
                return False
            entry.status = status
            entry.healthy = status in (CapabilityStatus.ENABLED, CapabilityStatus.DEGRADED)
            entry.last_health_check = _now_iso()
            entry.health_message = health_message or ""
            return True

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """Enable or disable a capability."""
        status = CapabilityStatus.ENABLED if enabled else CapabilityStatus.DISABLED
        return self.set_status(name, status)

    # ── Health summary ────────────────────────────────────────────────────────

    def health_summary(self) -> dict:
        """
        Aggregate health view suitable for the admin API or system prompt.
        Returns counts per kind/status and a flat list of entries.
        """
        with self._lock:
            entries = list(self._entries.values())

        counts: dict[str, dict[str, int]] = {}
        for e in entries:
            counts.setdefault(e.kind, {})
            counts[e.kind][e.status] = counts[e.kind].get(e.status, 0) + 1

        unhealthy = [e.to_dict() for e in entries if not e.healthy]
        total = len(entries)
        healthy_count = sum(1 for e in entries if e.healthy)

        return {
            "total": total,
            "healthy": healthy_count,
            "unhealthy": len(unhealthy),
            "by_kind": counts,
            "unhealthy_entries": unhealthy,
            "all": [e.to_dict() for e in entries],
        }

    def model_summary(self) -> str:
        """Compact one-liner for system prompt injection."""
        with self._lock:
            entries = list(self._entries.values())
        counts = {s: 0 for s in (
            CapabilityStatus.ENABLED, CapabilityStatus.DEGRADED,
            CapabilityStatus.OFFLINE, CapabilityStatus.DISABLED,
        )}
        for e in entries:
            if e.status in counts:
                counts[e.status] += 1
        degraded = counts[CapabilityStatus.DEGRADED]
        offline  = counts[CapabilityStatus.OFFLINE]
        parts = [f"enabled={counts[CapabilityStatus.ENABLED]}"]
        if degraded:
            parts.append(f"degraded={degraded}")
        if offline:
            parts.append(f"offline={offline}")
        return "[capabilities: " + " ".join(parts) + "]"


# ── Factory ───────────────────────────────────────────────────────────────────

def build_default_registry(cfg: dict, topology: Any = None) -> CapabilityRegistry:
    """
    Build a CapabilityRegistry pre-populated from config and topology.

    Registers:
    - MODEL entries from topology server states
    - AUDIO entries from STT/TTS config
    - MEMORY entries from db_path / chroma_path
    - CONNECTOR entries from discord/google config
    - INITIATIVE, REFLECTION, SCHEDULER entries (always registered)
    """
    reg = CapabilityRegistry()

    # ── Models ────────────────────────────────────────────────────────────────
    if topology:
        for role, state in topology.servers.items():
            if state.is_absent():
                status = CapabilityStatus.DISABLED
                healthy = False
            elif state.is_ready():
                status = CapabilityStatus.ENABLED
                healthy = True
            else:
                status = CapabilityStatus.UNKNOWN
                healthy = False
            server_cfg = cfg.get("servers", {}).get(role, {})
            reg.register(CapabilityEntry(
                name=role,
                kind=CapabilityKind.MODEL,
                status=status,
                healthy=healthy,
                policy="required" if server_cfg.get("required") else "optional",
                version=server_cfg.get("model_path", ""),
                metadata={"port": server_cfg.get("port"), "host": server_cfg.get("host")},
                last_health_check=_now_iso(),
            ))
    else:
        # Seed from config without topology
        for role, scfg in cfg.get("servers", {}).items():
            enabled = scfg.get("enabled", False)
            reg.register(CapabilityEntry(
                name=role,
                kind=CapabilityKind.MODEL,
                status=CapabilityStatus.UNKNOWN if enabled else CapabilityStatus.DISABLED,
                healthy=False,
                policy="required" if scfg.get("required") else "optional",
                version=scfg.get("model_path", ""),
            ))

    # ── Audio ─────────────────────────────────────────────────────────────────
    stt_cfg = cfg.get("stt", {})
    if stt_cfg:
        reg.register(CapabilityEntry(
            name="stt",
            kind=CapabilityKind.AUDIO,
            status=CapabilityStatus.UNKNOWN,
            healthy=False,
            policy="optional",
            version=stt_cfg.get("fw_model", ""),
        ))

    tts_cfg = cfg.get("tts", {})
    if tts_cfg:
        reg.register(CapabilityEntry(
            name="tts",
            kind=CapabilityKind.AUDIO,
            status=CapabilityStatus.UNKNOWN,
            healthy=False,
            policy="optional",
            version=tts_cfg.get("model_path", ""),
        ))

    # ── Memory ────────────────────────────────────────────────────────────────
    db_path = cfg.get("db_path")
    if db_path:
        reg.register(CapabilityEntry(
            name="sqlite_memory",
            kind=CapabilityKind.MEMORY,
            status=CapabilityStatus.UNKNOWN,
            healthy=False,
            policy="required",
            version=db_path,
        ))

    retrieval_cfg = cfg.get("retrieval", {})
    if retrieval_cfg:
        reg.register(CapabilityEntry(
            name="vector_memory",
            kind=CapabilityKind.MEMORY,
            status=CapabilityStatus.UNKNOWN,
            healthy=False,
            policy="optional",
            version=retrieval_cfg.get("embed_model", ""),
        ))

    # ── Connectors ────────────────────────────────────────────────────────────
    discord_cfg = cfg.get("discord", {})
    if discord_cfg.get("enabled"):
        reg.register(CapabilityEntry(
            name="discord",
            kind=CapabilityKind.CONNECTOR,
            status=CapabilityStatus.UNKNOWN,
            healthy=False,
            policy="optional",
        ))

    google_cfg = cfg.get("google", {})
    if google_cfg.get("enabled"):
        for svc in ("calendar", "gmail", "drive"):
            if google_cfg.get(f"{svc}_enabled"):
                reg.register(CapabilityEntry(
                    name=f"google_{svc}",
                    kind=CapabilityKind.CONNECTOR,
                    status=CapabilityStatus.UNKNOWN,
                    healthy=False,
                    policy="optional",
                ))

    # ── Cognitive subsystems ──────────────────────────────────────────────────
    for name, kind in [
        ("initiative_engine", CapabilityKind.INITIATIVE),
        ("reflection_pipeline", CapabilityKind.REFLECTION),
        ("memory_maintenance", CapabilityKind.SCHEDULER),
        ("idle_cognition", CapabilityKind.INITIATIVE),
        ("investigation_engine", CapabilityKind.SCHEDULER),
    ]:
        reg.register(CapabilityEntry(
            name=name,
            kind=kind,
            status=CapabilityStatus.UNKNOWN,
            healthy=False,
            policy="optional",
        ))

    return reg
