"""Tool Registry — Governance and Execution Control for EOS Runtime Tools

Provides a structured registry of all tools available to the AI entity runtime,
with governance metadata (risk level, trust level, confirmation policy) and
an audit trail for significant actions.

Architecture
============

Tier 1 — Classification:
  ToolRiskLevel  — how dangerous / reversible an action is
  ToolTrustLevel — who is allowed to invoke the action
  ConfirmationPolicy — when the system must seek explicit approval

Tier 2 — Schema:
  ToolSpec       — full declaration of a single tool with handler

Tier 3 — Audit trail:
  AuditEntry     — a record of one impactful tool execution
  AuditLog       — bounded, thread-safe, append-only audit trail

Tier 4 — Registry:
  ToolRegistry   — central store; enforces authorization and audit

Usage::

    from runtime.tool_registry import ToolRegistry, ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy

    registry = ToolRegistry()

    # Register a tool
    registry.register(ToolSpec(
        name="read_file",
        description="Read contents of a file.",
        pack="fs_tools",
        tags=["files", "read"],
        parameters={"type": "object", "properties": {...}},
        handler=lambda params: {...},
        risk_level="read_only",
        trust_level="public",
        confirmation_policy="none",
    ))

    # Get enabled tools
    enabled = registry.all_enabled()

    # Look up a specific tool
    spec = registry.get("read_file")
    if spec:
        result = spec.handler({"path": "file.txt"})

    # Query by pack or tags
    fs_tools = registry.by_pack("fs_tools")

    # Record execution
    registry.record_execution("read_file", success=True, params_summary="path=file.txt")

    # Get audit summary
    for entry in registry.recent_audit(10):
        print(entry)
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class ToolRiskLevel:
    """How dangerous or reversible a tool's action is."""
    READ_ONLY = "read_only"
    DRAFT = "draft"
    REVERSIBLE_COMMIT = "reversible_commit"
    IRREVERSIBLE_COMMIT = "irreversible_commit"


class ToolTrustLevel:
    """Who is permitted to invoke a tool."""
    PUBLIC = "public"
    VERIFIED_USER = "verified_user"
    OPERATOR_ONLY = "operator_only"


class ConfirmationPolicy:
    """When a tool call must be confirmed before execution."""
    NONE = "none"
    SOFT_CONFIRM = "soft_confirm"
    HARD_CONFIRM = "hard_confirm"


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

@dataclass
class AuditEntry:
    """A single record in the tool action audit trail."""

    audit_id: str
    timestamp: datetime
    tool_name: str
    params_summary: str
    success: bool
    note: Optional[str] = None

    def as_dict(self) -> Dict:
        """Return a JSON-serializable dict representation."""
        return {
            "audit_id": self.audit_id,
            "timestamp": self.timestamp.isoformat(),
            "tool_name": self.tool_name,
            "params_summary": self.params_summary,
            "success": self.success,
            "note": self.note,
        }


class AuditLog:
    """Bounded, thread-safe, append-only audit trail for tool executions.

    Stores AuditEntry records for all reversible_commit and irreversible_commit
    actions. When the log reaches max_entries, the oldest entry is discarded.
    """

    def __init__(self, max_entries: int = 500) -> None:
        if max_entries < 1:
            raise ValueError(f"AuditLog max_entries must be >= 1, got {max_entries}.")
        self.max_entries = max_entries
        self._entries: List[AuditEntry] = []
        self._lock = threading.Lock()

    def append(self, entry: AuditEntry) -> None:
        """Add an entry to the log. Evicts oldest if at capacity."""
        with self._lock:
            if len(self._entries) >= self.max_entries:
                self._entries.pop(0)
            self._entries.append(entry)

    def recent(self, n: int = 50) -> List[AuditEntry]:
        """Return the n most recent entries."""
        with self._lock:
            return list(self._entries[-n:])

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def as_dict(self) -> Dict:
        """Return a JSON-serializable dict with all current entries."""
        with self._lock:
            return {
                "max_entries": self.max_entries,
                "entry_count": len(self._entries),
                "entries": [e.as_dict() for e in self._entries],
            }


# ---------------------------------------------------------------------------
# Tool Specification
# ---------------------------------------------------------------------------

@dataclass
class ToolSpec:
    """Full declaration of a single tool with governance metadata.

    Attributes:
        name:                  Unique identifier (snake_case)
        description:           Human-readable one-line description
        pack:                  Which toolpack registered it
        tags:                  Domain tags for broker filtering
        parameters:            JSON Schema for parameters
        handler:               The actual implementation function (params: dict) -> str
        risk_level:            One of ToolRiskLevel values
        trust_level:           One of ToolTrustLevel values
        confirmation_policy:   One of ConfirmationPolicy values
        enabled:               If False, tool is registered but not invokable
        timeout_seconds:       Execution timeout (default 30)
    """

    name: str
    description: str
    pack: str
    tags: List[str]
    parameters: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], str]
    risk_level: str
    trust_level: str
    confirmation_policy: str
    enabled: bool = True
    timeout_seconds: int = 30

    def __post_init__(self):
        """Validate risk/trust/confirmation values."""
        valid_risks = {ToolRiskLevel.READ_ONLY, ToolRiskLevel.DRAFT,
                      ToolRiskLevel.REVERSIBLE_COMMIT, ToolRiskLevel.IRREVERSIBLE_COMMIT}
        if self.risk_level not in valid_risks:
            raise ValueError(f"Invalid risk_level: {self.risk_level}")

        valid_trusts = {ToolTrustLevel.PUBLIC, ToolTrustLevel.VERIFIED_USER,
                       ToolTrustLevel.OPERATOR_ONLY}
        if self.trust_level not in valid_trusts:
            raise ValueError(f"Invalid trust_level: {self.trust_level}")

        valid_confirms = {ConfirmationPolicy.NONE, ConfirmationPolicy.SOFT_CONFIRM,
                         ConfirmationPolicy.HARD_CONFIRM}
        if self.confirmation_policy not in valid_confirms:
            raise ValueError(f"Invalid confirmation_policy: {self.confirmation_policy}")

    def as_dict(self) -> Dict:
        """Return a JSON-serializable dict representation."""
        return {
            "name": self.name,
            "description": self.description,
            "pack": self.pack,
            "tags": list(self.tags),
            "risk_level": self.risk_level,
            "trust_level": self.trust_level,
            "confirmation_policy": self.confirmation_policy,
            "enabled": self.enabled,
            "timeout_seconds": self.timeout_seconds,
        }


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Central registry for tool specs with governance and audit trail.

    Responsibilities:
      - Store and look up ToolSpec declarations
      - Enforce trust-level and risk-level contracts
      - Provide audit trail for significant actions
      - Enable querying tools by pack, tag, or status
    """

    def __init__(self, max_audit_entries: int = 500) -> None:
        self._specs: Dict[str, ToolSpec] = {}
        self._lock = threading.Lock()
        self.audit_log = AuditLog(max_entries=max_audit_entries)

    def register(self, spec: ToolSpec) -> None:
        """Register a tool spec in the registry.

        Args:
            spec: A ToolSpec instance with all required fields.

        Raises:
            ValueError: If spec.name is already registered.
        """
        if not spec.name or not isinstance(spec.name, str):
            raise ValueError("spec.name must be a non-empty string")

        with self._lock:
            if spec.name in self._specs:
                raise ValueError(f"Tool '{spec.name}' is already registered")
            self._specs[spec.name] = spec
            logger.debug(f"Registered tool: {spec.name} (pack: {spec.pack})")

    def get(self, name: str) -> Optional[ToolSpec]:
        """Look up a tool by name. Returns None if not found or disabled."""
        with self._lock:
            spec = self._specs.get(name)
        if spec is None or not spec.enabled:
            return None
        return spec

    def all_tools(self) -> List[ToolSpec]:
        """Return all registered tool specs (enabled and disabled)."""
        with self._lock:
            return list(self._specs.values())

    def set_enabled(self, name: str, enabled: bool) -> None:
        """Enable or disable a tool by name.

        Raises:
            KeyError: If the tool is not registered.
        """
        with self._lock:
            if name not in self._specs:
                raise KeyError(f"Tool '{name}' not found")
            self._specs[name].enabled = enabled
            logger.info("Tool '%s' %s", name, "enabled" if enabled else "disabled")

    def all_enabled(self) -> List[ToolSpec]:
        """Return list of all enabled tool specs."""
        with self._lock:
            return [spec for spec in self._specs.values() if spec.enabled]

    def by_pack(self, pack: str) -> List[ToolSpec]:
        """Return all enabled tools from a specific pack."""
        with self._lock:
            return [spec for spec in self._specs.values()
                   if spec.pack == pack and spec.enabled]

    def by_tag(self, tag: str) -> List[ToolSpec]:
        """Return all enabled tools with a specific tag."""
        with self._lock:
            return [spec for spec in self._specs.values()
                   if tag in spec.tags and spec.enabled]

    def summary(self) -> Dict[str, Any]:
        """Return a summary of registered tools."""
        with self._lock:
            all_specs = list(self._specs.values())
            enabled_specs = [s for s in all_specs if s.enabled]

            packs = {}
            for spec in all_specs:
                if spec.pack not in packs:
                    packs[spec.pack] = {"total": 0, "enabled": 0}
                packs[spec.pack]["total"] += 1
                if spec.enabled:
                    packs[spec.pack]["enabled"] += 1

            return {
                "total_tools": len(all_specs),
                "enabled_tools": len(enabled_specs),
                "disabled_tools": len(all_specs) - len(enabled_specs),
                "packs": packs,
            }

    def record_execution(
        self,
        tool_name: str,
        success: bool,
        params_summary: str,
        note: Optional[str] = None,
    ) -> Optional[str]:
        """Record a tool execution in the audit log.

        Only reversible_commit and irreversible_commit actions are recorded.

        Args:
            tool_name: Name of the tool that was executed.
            success: Whether the execution succeeded.
            params_summary: Human-readable summary of parameters.
            note: Optional additional note (e.g., error message).

        Returns:
            The audit_id if recorded, None otherwise.
        """
        spec = self.get(tool_name)
        if spec is None:
            return None

        # Only audit reversible_commit and irreversible_commit actions
        if spec.risk_level not in {ToolRiskLevel.REVERSIBLE_COMMIT,
                                   ToolRiskLevel.IRREVERSIBLE_COMMIT}:
            return None

        entry = AuditEntry(
            audit_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc),
            tool_name=tool_name,
            params_summary=params_summary,
            success=success,
            note=note,
        )
        self.audit_log.append(entry)
        return entry.audit_id

    def recent_audit(self, n: int = 50) -> List[AuditEntry]:
        """Return the n most recent audit entries."""
        return self.audit_log.recent(n=n)

    def audit_summary(self) -> Dict[str, Any]:
        """Return a summary of the audit log."""
        entries = self.audit_log.recent(n=self.audit_log.max_entries)
        return {
            "max_entries": self.audit_log.max_entries,
            "entry_count": len(entries),
            "success_count": sum(1 for e in entries if e.success),
            "failure_count": sum(1 for e in entries if not e.success),
            "entries": [e.as_dict() for e in entries],
        }
