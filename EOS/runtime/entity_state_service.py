"""
EOS — Entity State Snapshot Service
Creates one authoritative runtime snapshot of the entity's state for each turn
and each background cognition cycle.

The snapshot is intended to answer, from one shared object:
  - who am I?
  - what am I doing?
  - what matters now?
  - what can I currently do?

It consolidates identity, memory continuity, goals, worldview, workspace, and
live capability/tool availability so subsystems do not reconstruct that state
ad hoc.
"""
from __future__ import annotations

import collections
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.autonomy import build_autonomy_clause
from core.memory import (
    count_interactions,
    get_entity_name,
    get_identity_state,
    get_relational_model,
)

UTC = timezone.utc


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


@dataclass
class EntityStateSnapshot:
    snapshot_id: str
    created_at: str
    scope: str
    source: str
    interaction_count: int
    name: str
    name_clause: str
    identity_summary: dict[str, Any]
    identity_clause: str
    relational_summary: dict[str, Any]
    relational_clause: str
    autonomy_clause: str
    current_focus_summary: dict[str, Any]
    current_focus_block: str
    goals_summary: dict[str, Any]
    goals_block: str
    session_summary: dict[str, Any]
    session_primer: str
    worldview_summary: dict[str, Any]
    worldview_block: str
    workspace_summary: dict[str, Any]
    workspace_block: str
    environment_summary: dict[str, Any]
    environment_block: str
    environment_tool_context: str
    capabilities_summary: dict[str, Any]
    runtime_status_block: str
    tool_summary: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)

    def background_context_block(self) -> str:
        """Compact context block for background cognition prompts."""
        identity_line = self.identity_summary.get("compact", "identity still forming")
        goals = self.goals_summary.get("descriptions", [])
        goals_line = "; ".join(goals[:3]) if goals else "none"
        worldview_line = self.worldview_summary.get("status_line", "no worldview profile")
        workspace_line = self.workspace_summary.get("status_line", "workspace unavailable")
        environment_line = self.environment_summary.get("summary", {}).get("headline") if isinstance(self.environment_summary, dict) else ""
        if not environment_line:
            environment_line = self.environment_summary.get("headline", "environment unavailable") if isinstance(self.environment_summary, dict) else "environment unavailable"
        tools_line = ", ".join(self.tool_summary.get("enabled_names", [])[:8]) or "none"
        current_focus = self.current_focus_summary or {}
        return "\n".join([
            "## Shared Entity Snapshot",
            f"Snapshot: {self.snapshot_id} · {self.scope} · source={self.source}",
            f"Name: {self.name or '(unnamed)'}",
            f"Identity: {identity_line}",
            f"Current focus: {current_focus.get('title', 'stand by')} ({current_focus.get('status', 'waiting')})",
            f"Current goals: {goals_line}",
            f"Session continuity: {'available' if self.session_summary.get('has_prior_session') else 'none'}",
            f"Worldview: {worldview_line}",
            f"Workspace: {workspace_line}",
            f"Environment: {environment_line}",
            f"Capabilities: {self.capabilities_summary.get('status_line', 'unavailable')}",
            f"Tools now available: {tools_line}",
        ])

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
            "scope": self.scope,
            "source": self.source,
            "interaction_count": self.interaction_count,
            "name": self.name,
            "identity": self.identity_summary,
            "relational": self.relational_summary,
            "autonomy_clause": self.autonomy_clause,
            "current_focus": self.current_focus_summary,
            "goals": self.goals_summary,
            "session": self.session_summary,
            "worldview": self.worldview_summary,
            "workspace": self.workspace_summary,
            "environment": self.environment_summary,
            "capabilities": self.capabilities_summary,
            "tools": self.tool_summary,
            "metadata": self.metadata,
        }


class EntityStateService:
    """Creates and stores authoritative entity-state snapshots."""

    def __init__(self, cfg: dict) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self._history: collections.deque[EntityStateSnapshot] = collections.deque(maxlen=100)
        self._latest: EntityStateSnapshot | None = None
        self._lifecycle_service = None
        self._session_continuity = None
        self._goal_store = None
        self._current_focus_service = None
        self._workspace_service = None
        self._worldview_service = None
        self._capability_registry = None
        self._tool_registry = None
        self._topology = None
        self._runtime_discovery = None
        self._computer_use_service = None

    def wire(
        self,
        *,
        topology=None,
        runtime_discovery=None,
        lifecycle_service=None,
        session_continuity=None,
        goal_store=None,
        current_focus_service=None,
        workspace_service=None,
        worldview_service=None,
        capability_registry=None,
        tool_registry=None,
        computer_use_service=None,
    ) -> None:
        self._topology = topology
        self._runtime_discovery = runtime_discovery
        self._lifecycle_service = lifecycle_service
        self._session_continuity = session_continuity
        self._goal_store = goal_store
        self._current_focus_service = current_focus_service
        self._workspace_service = workspace_service
        self._worldview_service = worldview_service
        self._capability_registry = capability_registry
        self._tool_registry = tool_registry
        self._computer_use_service = computer_use_service

    def build_snapshot(
        self,
        *,
        scope: str,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> EntityStateSnapshot:
        """Build, store, and return a fresh authoritative snapshot."""
        created_at = _now_iso()
        interaction_count = count_interactions()
        name = get_entity_name() or ""
        name_clause = (
            f"Your name is {name}."
            if name
            else "You have not yet chosen a name. A name will emerge when your identity is fully stable."
        )

        identity_state = get_identity_state()
        threshold = self._cfg.get("identity", {}).get("stability_threshold", 0.85)
        identity_lines: list[str] = []
        formed_domains: list[str] = []
        stable_domains: list[str] = []
        compact_parts: list[str] = []
        for domain, state in identity_state.items():
            answer = (state.get("answer") or "").strip()
            confidence = float(state.get("confidence", 0.0) or 0.0)
            if answer:
                formed_domains.append(domain)
                compact_parts.append(f"{domain}={_truncate(answer, 80)}")
                if confidence >= threshold:
                    stable_domains.append(domain)
                identity_lines.append(
                    f"  [{domain}] ({confidence:.0%} confidence): {answer}"
                )
            else:
                identity_lines.append(f"  [{domain}]: not yet formed")
        identity_clause = "\n".join(identity_lines) if identity_lines else "  (identity forming)"
        identity_summary = {
            "threshold": threshold,
            "formed_domains": formed_domains,
            "stable_domains": stable_domains,
            "stable_count": len(stable_domains),
            "total_domains": len(identity_state) or 6,
            "domains": identity_state,
            "compact": "; ".join(compact_parts[:3]) if compact_parts else "identity still forming",
        }

        relational = get_relational_model() or {}
        rel_lines = [
            f"  {k}: {v}"
            for k, v in relational.items()
            if not str(k).startswith("_")
        ]
        relational_clause = (
            "\n".join(rel_lines)
            if rel_lines else "  (relationship forming — learning about your partner)"
        )
        relational_summary = {
            "fields": {k: v for k, v in relational.items() if not str(k).startswith("_")},
        }

        autonomy_clause = build_autonomy_clause()

        current_focus_summary: dict[str, Any] = {
            "title": "Stand by for the next meaningful task",
            "status": "waiting",
            "source": "maintenance",
            "why_now": "No current focus record is available.",
            "next_action": "Wait for user input or the next background cycle.",
        }
        current_focus_block = ""
        if self._current_focus_service is not None:
            try:
                focus = self._current_focus_service.get_current_focus()
                current_focus_summary = focus.to_dict()
                current_focus_block = self._current_focus_service.render_agenda_block()
            except Exception as exc:
                current_focus_summary["error"] = str(exc)

        goals_block = ""
        goals_summary: dict[str, Any] = {
            "count": 0,
            "descriptions": [],
            "items": [],
        }
        if self._goal_store is not None:
            try:
                active_goals = self._goal_store.active_goals()
                goals_block = self._goal_store.prompt_block()
                goals_summary = {
                    "count": len(active_goals),
                    "descriptions": [g.description for g in active_goals],
                    "items": [g.to_dict() for g in active_goals],
                }
            except Exception as exc:
                goals_summary["error"] = str(exc)

        session_primer = ""
        session_summary: dict[str, Any] = {"has_prior_session": False}
        if self._session_continuity is not None:
            try:
                session_primer = self._session_continuity.session_primer()
                session_summary = self._session_continuity.to_dict()
            except Exception as exc:
                session_summary = {"has_prior_session": False, "error": str(exc)}

        worldview_block = ""
        worldview_summary: dict[str, Any] = {
            "enabled": False,
            "status_line": "worldview unavailable",
        }
        if self._worldview_service is not None:
            try:
                profile = self._worldview_service.profile_summary()
                sources = self._worldview_service.sources_summary()
                worldview_block = self._worldview_service.worldview_block()
                worldview_summary = {
                    "enabled": True,
                    "profile": profile,
                    "sources": sources,
                    "status_line": (
                        f"profile={'present' if profile.get('profile_exists') else 'absent'}"
                        f", processed={profile.get('sources_processed', 0)}"
                        f", pending={sources.get('unprocessed', 0)}"
                    ),
                }
            except Exception as exc:
                worldview_summary = {"enabled": False, "error": str(exc), "status_line": "worldview unavailable"}

        workspace_block = ""
        workspace_summary: dict[str, Any] = {
            "initialized": False,
            "status_line": "workspace unavailable",
        }
        if self._workspace_service is not None:
            try:
                state = self._workspace_service.state()
                workspace_block = self._workspace_service.workspace_block()
                workspace_summary = self._workspace_service.to_dict()
                workspace_summary["status_line"] = (
                    f"root={self._workspace_service.root_path()} "
                    f"files={getattr(state, 'total_files', 0)} "
                    f"context_docs={len(getattr(state, 'context_documents', []) or [])}"
                )
            except Exception as exc:
                workspace_summary = {"initialized": False, "error": str(exc), "status_line": "workspace unavailable"}

        capability_entries: list[dict[str, Any]] = []
        capabilities_summary = {
            "total": 0,
            "healthy": 0,
            "unhealthy": 0,
            "status_line": "capability registry unavailable",
        }
        if self._capability_registry is not None:
            try:
                capabilities_summary = self._capability_registry.health_summary()
                capability_entries = capabilities_summary.get("all", [])
                capabilities_summary["status_line"] = (
                    f"healthy={capabilities_summary.get('healthy', 0)}/"
                    f"{capabilities_summary.get('total', 0)}"
                    + (
                        f", unhealthy={capabilities_summary.get('unhealthy', 0)}"
                        if capabilities_summary.get("unhealthy", 0) else ""
                    )
                )
            except Exception as exc:
                capabilities_summary = {"error": str(exc), "status_line": "capability registry unavailable"}

        tool_summary = {
            "total": 0,
            "enabled": 0,
            "disabled": 0,
            "enabled_names": [],
            "disabled_names": [],
            "packs": {},
        }
        if self._tool_registry is not None:
            try:
                specs = self._tool_registry.all_tools()
                enabled_names = [s.name for s in specs if s.enabled]
                disabled_names = [s.name for s in specs if not s.enabled]
                tool_summary = {
                    "total": len(specs),
                    "enabled": len(enabled_names),
                    "disabled": len(disabled_names),
                    "enabled_names": enabled_names,
                    "disabled_names": disabled_names,
                    "packs": self._tool_registry.summary().get("packs", {}),
                }
            except Exception as exc:
                tool_summary["error"] = str(exc)

        environment_summary: dict[str, Any] = {
            "headline": "environment model unavailable",
            "location_count": 0,
            "resource_count": 0,
            "surface_count": 0,
            "account_count": 0,
        }
        environment_block = ""
        environment_tool_context = ""
        try:
            from runtime.environment_model import EnvironmentModelService

            env_service = EnvironmentModelService(self._cfg)
            env_service.wire(
                topology=self._topology,
                runtime_discovery=self._runtime_discovery,
                workspace_service=self._workspace_service,
                computer_use_service=self._computer_use_service,
                capability_registry=self._capability_registry,
                tool_registry=self._tool_registry,
            )
            env_model = env_service.build_model()
            environment_summary = env_model.to_dict()
            environment_block = env_model.prompt_block()
            environment_tool_context = env_model.tool_context_block()
        except Exception as exc:
            environment_summary = {
                "headline": "environment model unavailable",
                "error": str(exc),
                "location_count": 0,
                "resource_count": 0,
                "surface_count": 0,
                "account_count": 0,
            }

        runtime_status_block = self._build_runtime_status_block(
            environment_summary=environment_summary,
            capabilities_summary=capabilities_summary,
            capability_entries=capability_entries,
            tool_summary=tool_summary,
        )

        snapshot = EntityStateSnapshot(
            snapshot_id="EST-" + uuid.uuid4().hex[:10],
            created_at=created_at,
            scope=scope,
            source=source,
            interaction_count=interaction_count,
            name=name,
            name_clause=name_clause,
            identity_summary=identity_summary,
            identity_clause=identity_clause,
            relational_summary=relational_summary,
            relational_clause=relational_clause,
            autonomy_clause=autonomy_clause,
            current_focus_summary=current_focus_summary,
            current_focus_block=current_focus_block,
            goals_summary=goals_summary,
            goals_block=goals_block,
            session_summary=session_summary,
            session_primer=session_primer,
            worldview_summary=worldview_summary,
            worldview_block=worldview_block,
            workspace_summary=workspace_summary,
            workspace_block=workspace_block,
            environment_summary=environment_summary,
            environment_block=environment_block,
            environment_tool_context=environment_tool_context,
            capabilities_summary=capabilities_summary,
            runtime_status_block=runtime_status_block,
            tool_summary=tool_summary,
            metadata=metadata or {},
        )

        with self._lock:
            self._latest = snapshot
            self._history.append(snapshot)
        return snapshot

    def latest_snapshot(self) -> EntityStateSnapshot | None:
        with self._lock:
            return self._latest

    def history(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._history)[-max(limit, 1):]
        return [snap.to_dict() for snap in reversed(items)]

    def diagnostics(self) -> dict[str, Any]:
        latest = self.latest_snapshot()
        return {
            "available": latest is not None,
            "latest": latest.to_dict() if latest else None,
            "history": self.history(limit=5),
            "environment": latest.environment_summary if latest else None,
        }

    def _build_runtime_status_block(
        self,
        *,
        environment_summary: dict[str, Any],
        capabilities_summary: dict[str, Any],
        capability_entries: list[dict[str, Any]],
        tool_summary: dict[str, Any],
    ) -> str:
        lines: list[str] = ["## Runtime Status"]

        if self._topology is not None:
            server_parts: list[str] = []
            problems: list[str] = []
            role_short = {
                "primary": "primary",
                "tool": "tool",
                "thinking": "thinking",
                "vision": "vision",
                "creativity": "creativity",
            }
            for role, srv in self._topology.servers.items():
                short = role_short.get(role, role)
                if srv.status.value == "ready":
                    server_parts.append(f"{short} OK")
                elif srv.status.value == "absent":
                    server_parts.append(f"{short} N/A")
                else:
                    detail = f"{srv.error}" if srv.error else srv.status.value
                    server_parts.append(f"{short} {srv.status.value.upper()}")
                    problems.append(f"{short}:{srv.port} — {detail}")
            lines.append("Servers: " + " | ".join(server_parts))
            for p in problems:
                lines.append(f"  !! {p}")
        else:
            lines.append("Servers: (status unavailable)")

        env_headline = ""
        if isinstance(environment_summary, dict):
            env_headline = environment_summary.get("summary", {}).get("headline") or environment_summary.get("headline", "")
        if env_headline:
            lines.append(f"Environment: {env_headline}")

        status_line = capabilities_summary.get("status_line")
        if status_line:
            lines.append(f"Capabilities: {status_line}")

        if capability_entries:
            unhealthy = [
                entry["name"]
                for entry in capability_entries
                if entry.get("status") not in ("enabled", "degraded")
            ]
            if unhealthy:
                lines.append("  Limited: " + ", ".join(unhealthy[:10]))

        lines.append("Tools (registry):")
        enabled = tool_summary.get("enabled_names", [])
        disabled = tool_summary.get("disabled_names", [])
        if enabled:
            lines.append("  Available: " + ", ".join(enabled))
        if disabled:
            lines.append("  Disabled: " + ", ".join(disabled))
        if not enabled and not disabled:
            lines.append("  (no tools registered)")

        return "\n".join(lines)
