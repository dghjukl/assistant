"""
EOS WebUI Server
FastAPI server for user chat interface and admin panel.

Startup sequence:
  1. Load config from an explicitly passed config path (fallback: EOS_CONFIG env var, then config.json)
  2. Call orchestrator.startup(cfg) — init memory + db
  3. Discover already-running backends and build RuntimeTopology
  4. Init CognitionTracer
  5. Seed app_state.tool_states, app_state.perm_allowlist, etc.
  6. Start background tasks

Global state:
  app_state.topology: RuntimeTopology | None
  app_state.cfg: dict
  app_state.tracer: CognitionTracer | None
  app_state.log_ring: deque[LogEntry]
  app_state.admin_ws_clients: list[WebSocket]
  app_state.tool_states: dict[tool_name, bool]
  app_state.perm_allowlist: set[permission_class_name]
  app_state.toolpack_states: dict[pack_name, bool]
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from core.autonomy import get_full_profile, set_dimension, can
from core.auth import AdminAuthMiddleware, load_or_create_token, get_admin_token, get_token_file_path
from core.access_control import (
    AccessControlMiddleware,
    init_access_controller, get_access_controller,
    classify_origin, extract_client_ip,
    TIER_LOCALHOST, TIER_LAN, TIER_EXTERNAL,
)
from webui.schemas import (
    ChatRequest, UploadRequest, TtsRequest, VisionSettingsRequest, AutonomyRequest,
    CapabilityRequest, ComputerUseModeRequest, ComputerUseHaltRequest,
    InitiativeTriggerRequest, InitiativeFeedbackRequest,
    InvestigationCreateRequest, InvestigationRunPassRequest, InvestigationResolveRequest,
    SecretSetRequest, ForceToolRequest, ForceRetrievalRequest,
    GoalCreateRequest, GoalNoteRequest, GoalAbandonRequest,
    AccessTierUpdateRequest, LanPairRequest, LanSessionRevokeRequest,
    OvernightReturnTimeRequest, OvernightCancelRequest,
    ExternalInferenceConfigUpdate, ExternalInferenceApiKeyRequest,
)
from core.audit import init_audit_store, get_audit_store
from core.secrets import init_secrets, secrets_manager as _secrets_manager_ref
from runtime.service_discovery import discover_runtime
from runtime.startup_health import detect_startup_guidance, issue_record
from runtime.topology import RuntimeTopology
from webui.app_state import app_state





async def run_evaluation_cycle(*args, **kwargs):
    from core.identity import run_evaluation_cycle as _run_evaluation_cycle
    return await _run_evaluation_cycle(*args, **kwargs)

def get_status(cfg):
    from core.entity import get_status as _get_status
    return _get_status(cfg)


def memory_configure(cfg):
    from core.memory import configure as _memory_configure
    return _memory_configure(cfg)


def get_identity_state():
    from core.memory import get_identity_state as _get_identity_state
    return _get_identity_state()


def get_recent_interactions(limit):
    from core.memory import get_recent_interactions as _get_recent_interactions
    return _get_recent_interactions(limit)


def get_relational_model():
    from core.memory import get_relational_model as _get_relational_model
    return _get_relational_model()


def search_memory(query, top_k):
    from core.memory import search_memory as _search_memory
    return _search_memory(query, top_k=top_k)


def orchestrator_startup(cfg):
    from runtime.orchestrator import startup as _startup
    return _startup(cfg)


async def process_turn(topology, user_input, cfg, **kwargs):
    from runtime.orchestrator import process_turn as _process_turn
    return await _process_turn(topology, user_input, cfg, **kwargs)


async def execute_worldview_extraction(topology, user_input, cfg, **kwargs):
    from runtime.orchestrator import execute_worldview_extraction as _execute_worldview_extraction
    return await _execute_worldview_extraction(topology, user_input, cfg, **kwargs)

# Try to import CognitionTracer and SignalBus; fail gracefully
try:
    from runtime.cognition_tracer import CognitionTracer
except ImportError:
    CognitionTracer = None

try:
    from runtime.signal_bus import SignalBus
except ImportError:
    SignalBus = None


# ── Setup logging ──────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("eos.webui")


# ── Shared app state ───────────────────────────────────────────────────────

# Mutable runtime state now lives in webui.app_app_state.state so routers can depend on
# an explicit container instead of scattered module globals.


# ── Helpers ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _emit_log(level: str, source: str, message: str, detail: Any = None) -> None:
    """Emit a log entry to the ring and broadcast to admin WS clients."""
    entry = {
        "timestamp": time.time(),
        "level": level,
        "source": source,
        "message": message,
        "detail": detail,
    }
    app_state.log_ring.append(entry)
    # Schedule async broadcast
    asyncio.create_task(_broadcast_log_to_admins(entry))


def _record_startup_issue(category: str, component: str, reason: str, detail: Any = None) -> dict[str, Any]:
    issue = issue_record(category, component, reason, detail=detail)
    app_state.startup_issues.append(issue)
    return issue


def _track_background_task(name: str, task: asyncio.Task) -> asyncio.Task:
    def _on_done(done: asyncio.Task) -> None:
        app_state.background_tasks.discard(done)
        if done.cancelled():
            return
        try:
            exc = done.exception()
        except asyncio.CancelledError:
            return
        except Exception as callback_exc:
            exc = callback_exc
        if exc is None:
            return
        issue = _record_startup_issue(
            "background_task_failure",
            name,
            "task_exited",
            {"error": str(exc)},
        )
        logger.error("Background task %s exited unexpectedly: %s", name, exc)
        _emit_log("error", "startup", f"Background task '{name}' failed", issue)

    app_state.background_tasks.add(task)
    task.add_done_callback(_on_done)
    return task


async def _broadcast_log_to_admins(entry: dict) -> None:
    """Send log entry to all connected admin WebSocket clients."""
    disconnected = []
    for ws in app_state.admin_ws_clients:
        try:
            await ws.send_json({"type": "log", "data": entry})
        except Exception as exc:
            logger.debug("Admin WS send failed: %s", exc)
            disconnected.append(ws)
    # Remove disconnected clients
    for ws in disconnected:
        if ws in app_state.admin_ws_clients:
            app_state.admin_ws_clients.remove(ws)


def _get_request_origin(request: Request) -> tuple[str, str]:
    """Return (origin_tier, client_ip) from request state (set by AccessControlMiddleware).
    Falls back to live classification if state is not yet set (early startup)."""
    tier = getattr(request.state, "origin_tier", None)
    ip   = getattr(request.state, "client_ip",   None)
    if tier is None or ip is None:
        ip   = ip   or extract_client_ip(request)
        tier = tier or classify_origin(ip)
    return tier, ip


def _sanitize_config(cfg: dict) -> dict:
    """Remove sensitive fields from config for admin API."""
    safe = {}
    for key, val in cfg.items():
        if key in ("discord", "google") or "token" in key.lower() or "secret" in key.lower():
            safe[key] = "[REDACTED]"
        elif isinstance(val, dict):
            safe[key] = _sanitize_config(val)
        else:
            safe[key] = val
    return safe


def _notify_interaction() -> float:
    """Update the shared interaction timestamp for all user-facing turn entry points."""
    interaction_ts = time.monotonic()
    app_state.last_interaction_monotonic = interaction_ts
    if app_state.overnight_cycle_service is not None and hasattr(app_state.overnight_cycle_service, "note_interaction"):
        try:
            app_state.overnight_cycle_service.note_interaction(now=datetime.now(timezone.utc))
        except Exception as exc:
            logger.debug("Overnight interaction update failed: %s", exc)
    if app_state.idle_cognition is not None and hasattr(app_state.idle_cognition, "notify_interaction"):
        try:
            app_state.idle_cognition.notify_interaction(at_monotonic=interaction_ts)
        except TypeError:
            app_state.idle_cognition.notify_interaction()
    return interaction_ts


def _notify_turn_completed() -> None:
    """Propagate a completed turn to the turn-aware runtime engines."""
    if app_state.reflection_pipeline:
        app_state.reflection_pipeline.notify_turn()
    if app_state.initiative_engine:
        app_state.initiative_engine.notify_turn()


def _build_discord_turn_notifier():
    """Build a Discord turn notifier that updates shared interaction and turn state."""

    def _discord_turn_notifier():
        _notify_interaction()
        _notify_turn_completed()

    return _discord_turn_notifier


def _get_overnight_status(*, include_history: bool = False) -> dict[str, Any]:
    if app_state.overnight_cycle_service is None:
        return {
            "enabled": False,
            "phase": "DAY_ACTIVE",
            "status": "none",
            "current_window": None,
            "posture": {
                "overnight_active": False,
                "allow_investigations": True,
                "allow_memory_maintenance": True,
                "allow_initiative": True,
                "idle_cognition_style": "day",
                "prefer_synthesis": False,
                "suppress_heavy_new_work": False,
            },
            "config": {},
            "recent_history": [] if include_history else None,
        }
    try:
        return app_state.overnight_cycle_service.get_status(include_history=include_history)
    except Exception as exc:
        logger.debug("OvernightCycleService status failed: %s", exc)
        return {
            "enabled": False,
            "phase": "DAY_ACTIVE",
            "status": "none",
            "current_window": None,
            "error": str(exc),
            "posture": {},
            "config": {},
            "recent_history": [] if include_history else None,
        }


def _build_entity_snapshot(
    *,
    scope: str,
    source: str,
    metadata: dict[str, Any] | None = None,
):
    """Build a fresh shared entity-state snapshot when the service is available."""
    if app_state.entity_state_service is None:
        return None
    try:
        enriched_metadata = dict(metadata or {})
        enriched_metadata.setdefault("overnight", _get_overnight_status())
        return app_state.entity_state_service.build_snapshot(
            scope=scope,
            source=source,
            metadata=enriched_metadata,
        )
    except Exception as exc:
        logger.debug("EntityStateService snapshot failed (%s): %s", source, exc)
        return None


def _presence_recent_events(limit: int = 6) -> list[dict[str, Any]]:
    """Return recent notable runtime events for the presence layer."""
    notable = []
    interesting_sources = (
        "chat", "initiative", "investigation", "backup", "health",
        "health_monitor", "idle_cognition", "reflection", "maintenance",
    )
    for entry in reversed(app_state.log_ring):
        source = str(entry.get("source") or "")
        if source and not any(anchor in source for anchor in interesting_sources):
            continue
        notable.append({
            "timestamp": entry.get("timestamp"),
            "level": entry.get("level"),
            "source": source or "runtime",
            "message": str(entry.get("message") or "").strip(),
            "detail": entry.get("detail"),
        })
        if len(notable) >= limit:
            break
    return notable


def _build_presence_state(
    *,
    entity_snapshot=None,
    include_recent_interactions: bool = True,
):
    """Build a presence-layer state from live runtime services."""
    try:
        from runtime.presence_layer import build_presence_state
    except Exception:
        return None

    recent_interactions: list[dict[str, Any]] = []
    if include_recent_interactions:
        try:
            recent_interactions = get_recent_interactions(8)
        except Exception:
            recent_interactions = []

    current_focus = (
        getattr(entity_snapshot, "current_focus_summary", None)
        if entity_snapshot is not None else None
    ) or _get_current_focus_dict()
    attention_summary = (
        getattr(entity_snapshot, "attention_summary", None)
        if entity_snapshot is not None else None
    ) or {}
    continuity = (
        getattr(entity_snapshot, "session_summary", None)
        if entity_snapshot is not None else None
    ) or (app_state.session_continuity.to_dict() if app_state.session_continuity is not None else {"has_prior_session": False})
    environment = (
        getattr(entity_snapshot, "environment_summary", None)
        if entity_snapshot is not None else None
    ) or {}
    capabilities = (
        getattr(entity_snapshot, "capabilities_summary", None)
        if entity_snapshot is not None else None
    ) or (
        app_state.capability_registry.health_summary()
        if app_state.capability_registry is not None and hasattr(app_state.capability_registry, "health_summary")
        else {}
    )

    initiative = {
        "enabled": can("initiative"),
        "queue_depth": len(app_state.initiative_engine.get_queue()) if app_state.initiative_engine is not None else 0,
    }
    if app_state.initiative_engine is not None and hasattr(app_state.initiative_engine, "get_status"):
        try:
            initiative.update(app_state.initiative_engine.get_status())
        except Exception:
            pass

    idle = {"tier": "active", "seconds_since_interaction": 0.0}
    try:
        if app_state.idle_cognition is not None and hasattr(app_state.idle_cognition, "status"):
            idle.update(app_state.idle_cognition.status())
        else:
            idle_secs = max(time.monotonic() - float(app_state.last_interaction_monotonic or time.monotonic()), 0.0)
            idle = {"tier": "active", "seconds_since_interaction": round(idle_secs, 2)}
            ic_cfg = app_state.cfg.get("idle_cognition", {})
            resting_h = float(ic_cfg.get("resting_threshold_hours", 2.0))
            drifting_h = float(ic_cfg.get("drifting_threshold_hours", 6.0))
            deep_h = float(ic_cfg.get("deep_threshold_hours", 24.0))
            idle_hours = idle_secs / 3600.0
            if idle_hours >= deep_h:
                idle["tier"] = "deep"
            elif idle_hours >= drifting_h:
                idle["tier"] = "drifting"
            elif idle_hours >= resting_h:
                idle["tier"] = "resting"
            idle["hours_since_last_interaction"] = round(idle_hours, 2)
    except Exception:
        pass

    try:
        return build_presence_state(
            current_focus=current_focus,
            attention_summary=attention_summary,
            continuity=continuity,
            environment=environment,
            capabilities=capabilities,
            recent_events=_presence_recent_events(),
            recent_interactions=recent_interactions,
            initiative=initiative,
            idle=idle,
            overnight=_get_overnight_status(),
        )
    except Exception as exc:
        logger.debug("Presence layer build failed: %s", exc)
        return None


def _build_presence_payload(*, entity_snapshot=None) -> dict[str, Any] | None:
    state = _build_presence_state(entity_snapshot=entity_snapshot)
    return state.to_dict() if state is not None else None


def _signal_bus_health_summary() -> dict[str, Any]:
    """Return compact signal-bus health for admin APIs."""
    if app_state.bus is None:
        return {"available": False, "healthy": None}
    if hasattr(app_state.bus, "health_summary"):
        try:
            summary = app_state.bus.health_summary()
            return {"available": True, **summary}
        except Exception as exc:
            logger.warning("Signal bus health summary failed: %s", exc)
            return {"available": True, "healthy": False, "error": str(exc)}
    return {"available": True, "healthy": None}

def _backup_health_summary() -> dict[str, Any]:
    """Return compact backup health for admin APIs from BackupService diagnostics."""
    if app_state.backup_service is None:
        return {"available": False}
    try:
        diagnostics = app_state.backup_service.diagnostics()
        return {"available": True, **diagnostics}
    except Exception as exc:
        logger.warning("Backup diagnostics failed: %s", exc)
        return {"available": True, "healthy": False, "error": str(exc)}


def _iso_utc_after_seconds(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() + max(seconds, 0.0), timezone.utc).isoformat().replace("+00:00", "Z")


async def _run_auto_backup_cycle() -> None:
    """Run one auto-backup scheduler cycle and update diagnostics/logging."""
    if app_state.backup_service is None:
        return

    backup_cfg = app_state.cfg.get("backup", {})
    interval_hours = float(backup_cfg.get("auto_backup_interval_hours", 24))
    next_run_at = _iso_utc_after_seconds(interval_hours * 3600)

    try:
        should_backup = app_state.backup_service.needs_auto_backup()
        if should_backup:
            manifest = app_state.backup_service.create_backup(
                label="auto",
                trigger="auto_interval",
                notes="Background auto-backup scheduler",
            )
            _emit_log(
                "info",
                "backup",
                "Background auto-backup complete",
                {"backup_id": manifest.backup_id, "size_bytes": manifest.total_size_bytes},
            )
        else:
            _emit_log("info", "backup", "Background auto-backup check skipped; no backup needed")
        app_state.backup_service.mark_auto_backup_run(next_run_at=next_run_at)
    except Exception as exc:
        app_state.backup_service.mark_auto_backup_failure(exc, next_run_at=next_run_at)
        _emit_log("error", "backup", f"Background auto-backup failed: {exc}")
        logger.error("Background auto-backup failed: %s", exc)


async def _backup_loop() -> None:
    """Periodic backup scheduler driven by backup.auto_backup_interval_hours."""
    while True:
        try:
            backup_cfg = app_state.cfg.get("backup", {})
            interval_hours = float(backup_cfg.get("auto_backup_interval_hours", 24))
            await asyncio.sleep(interval_hours * 3600)
            await _run_auto_backup_cycle()
        except Exception as exc:
            logger.error("Backup loop error: %s", exc)


def _get_current_focus_dict() -> dict[str, Any]:
    """Return the authoritative current-focus record as a plain dict."""
    if app_state.current_focus_service is None:
        return {
            "focus_id": "focus-unavailable",
            "title": "Stand by for the next meaningful task",
            "why_now": "Current focus service is not initialized.",
            "next_action": "Wait for user input or startup completion.",
            "status": "waiting",
            "source": "maintenance",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "metadata": {"available": False},
        }
    try:
        return app_state.current_focus_service.get_current_focus().to_dict()
    except Exception as exc:
        logger.debug("CurrentFocusService lookup failed: %s", exc)
        return {
            "focus_id": "focus-error",
            "title": "Stand by for the next meaningful task",
            "why_now": f"Current focus lookup failed: {exc}",
            "next_action": "Retry current focus resolution.",
            "status": "blocked",
            "source": "maintenance",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "metadata": {"available": False, "error": str(exc)},
        }


def _group_tools_by_category() -> dict[str, list[str]]:
    """Group registered tools by their pack name.

    Uses the live ToolRegistry when available (authoritative source).
    Falls back to the legacy TOOL_SCHEMA dispatcher only when the registry
    has not yet been initialised (e.g. very early startup).
    """
    # Prefer the live registry — it is the single source of truth
    if app_state.tool_registry is not None:
        groups: dict[str, list[str]] = {}
        for spec in app_state.tool_registry.all_tools():
            groups.setdefault(spec.pack, []).append(spec.name)
        return groups

    # Legacy fallback: derive from old TOOL_SCHEMA dispatcher
    try:
        from tools.dispatcher import TOOL_SCHEMA
        legacy_groups = {
            "perception": ["screen_capture", "webcam_capture"],
            "memory": ["query_memory", "save_memory"],
            "calendar": ["list_events", "create_event"],
            "communication": ["send_discord"],
            "files": ["read_file", "write_file", "list_dir"],
            "web": ["web_search"],
        }
        return {pack: [t for t in tools if t in TOOL_SCHEMA]
                for pack, tools in legacy_groups.items()}
    except Exception:
        return {}


def _resolve_service_probe(role: str):
    """Return the live runtime discovery probe for a service role when available."""
    if app_state.runtime_discovery is None:
        return None
    return app_state.runtime_discovery.services.get(role)


def _resolve_service_endpoint(role: str, state) -> str:
    """Resolve a service health endpoint from runtime discovery with state fallback."""
    probe = _resolve_service_probe(role)
    if probe is not None and probe.endpoint:
        return probe.endpoint
    return state.endpoint


async def _server_health_loop() -> None:
    """Every 30s: check /health on each server; track primary degradation state."""
    while True:
        try:
            await asyncio.sleep(30)
            if not app_state.topology:
                continue

            for role, state in app_state.topology.servers.items():
                # Only probe servers that were previously ready or are in error
                if state.is_absent():
                    continue

                endpoint = _resolve_service_endpoint(role, state)

                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        resp = await client.get(f"{endpoint}/health")
                        if resp.status_code == 200:
                            # Recovery: server is healthy again
                            was_error = state.status.value in ("error", "starting")
                            app_state.topology.mark_ready(role, state.pid)
                            if was_error:
                                _emit_log(
                                    "info", "health_monitor",
                                    f"Server '{role}' recovered",
                                    {"role": role, "port": state.port, "endpoint": endpoint},
                                )
                            # Clear primary degradation flag
                            if role == "primary" and app_state.primary_degraded:
                                app_state.primary_degraded = False
                                _emit_log("info", "health_monitor",
                                          "Primary server recovered — chat re-enabled")
                        else:
                            app_state.topology.mark_error(role, f"HTTP {resp.status_code}")
                            _emit_log(
                                "warn", "health_monitor",
                                f"Server '{role}' unhealthy",
                                {"status": resp.status_code, "role": role, "endpoint": endpoint},
                            )
                            if role == "primary" and not app_state.primary_degraded:
                                app_state.primary_degraded = True
                                _emit_log(
                                    "error", "health_monitor",
                                    "PRIMARY SERVER DOWN — chat responses gated",
                                    {"role": role, "port": state.port, "endpoint": endpoint},
                                )
                except Exception as exc:
                    if state.is_ready():
                        app_state.topology.mark_error(role, str(exc))
                        _emit_log(
                            "error", "health_monitor",
                            f"Server '{role}' unreachable",
                            {"role": role, "error": str(exc), "endpoint": endpoint},
                        )
                        if role == "primary" and not app_state.primary_degraded:
                            app_state.primary_degraded = True
                            _emit_log(
                                "error", "health_monitor",
                                "PRIMARY SERVER UNREACHABLE — chat responses gated",
                                {"role": role, "port": state.port, "endpoint": endpoint},
                            )
        except Exception as exc:
            logger.error("Health loop error: %s", exc)


async def _memory_maintenance_loop() -> None:
    """Periodic memory maintenance: prune, consolidate, health check."""
    while True:
        try:
            maint_cfg = app_state.cfg.get("memory_maintenance", {})
            interval_hours = float(maint_cfg.get("maintenance_interval_hours", 6))
            await asyncio.sleep(interval_hours * 3600)

            if not app_state.topology:
                continue

            overnight_status = _get_overnight_status()
            if overnight_status.get("current_window") and not overnight_status.get("posture", {}).get("allow_memory_maintenance", True):
                _emit_log(
                    "info",
                    "memory_maintenance",
                    "Maintenance deferred by overnight posture",
                    {"phase": overnight_status.get("phase"), "status": overnight_status.get("status")},
                )
                continue

            _emit_log("info", "memory_maintenance", "Starting maintenance run")
            focus_id = None
            try:
                if app_state.current_focus_service is not None:
                    focus = app_state.current_focus_service.set_background_focus(
                        focus_id="maintenance-loop",
                        title="Run memory maintenance",
                        why_now="The scheduled memory-maintenance interval elapsed.",
                        next_action="Prune, consolidate, and health-check memory stores.",
                        source="maintenance",
                    )
                    focus_id = focus.focus_id
                _build_entity_snapshot(
                    scope="background",
                    source="memory_maintenance.loop",
                    metadata={"loop": "memory_maintenance"},
                )
                from runtime.memory_maintenance import run_maintenance
                result = await run_maintenance(
                    app_state.topology, app_state.cfg, tracer=app_state.tracer, bus=app_state.bus
                )
                app_state.last_maintenance_result = result
                _emit_log(
                    "info", "memory_maintenance",
                    f"Maintenance complete ({result.get('elapsed_ms', 0)}ms)",
                    {
                        "interactions_pruned": result.get("interaction_prune", {}).get("pruned", 0),
                        "vectors_pruned":      result.get("vector_prune", {}).get("pruned", 0),
                        "consolidated":        result.get("consolidation", {}).get("entries_created", 0),
                    },
                )
                if app_state.current_focus_service is not None:
                    app_state.current_focus_service.set_background_focus(
                        focus_id=focus_id or "maintenance-loop",
                        title="Memory maintenance finished",
                        why_now="The scheduled maintenance run completed successfully.",
                        next_action="Wait for the next maintenance interval or user task.",
                        status="done",
                        source="maintenance",
                        metadata={"result": result},
                    )
            except Exception as exc:
                _emit_log("error", "memory_maintenance", f"Maintenance failed: {exc}")
                logger.error("Memory maintenance error: %s", exc)
                if app_state.current_focus_service is not None:
                    app_state.current_focus_service.set_background_focus(
                        focus_id=focus_id or "maintenance-loop",
                        title="Memory maintenance failed",
                        why_now="A scheduled maintenance cycle raised an error.",
                        next_action="Inspect the failure and retry when safe.",
                        status="blocked",
                        source="maintenance",
                        metadata={"error": str(exc)},
                    )

        except Exception as exc:
            logger.error("Memory maintenance loop error: %s", exc)


async def _initiative_loop() -> None:
    """Every 60s: evaluate initiative candidates and execute queued items."""
    while True:
        try:
            await asyncio.sleep(60)
            if app_state.initiative_engine is None or app_state.topology is None:
                continue
            from core.autonomy import can
            if not can("initiative"):
                continue
            try:
                if app_state.current_focus_service is not None:
                    app_state.current_focus_service.set_background_focus(
                        focus_id="initiative-eval",
                        title="Evaluate initiative opportunities",
                        why_now="The initiative scheduler fired and autonomy permits proactive work.",
                        next_action="Inspect current signals and queue the strongest initiative candidate.",
                        source="maintenance",
                    )
                snapshot = _build_entity_snapshot(
                    scope="background",
                    source="initiative.loop",
                    metadata={"loop": "initiative"},
                )
                try:
                    from runtime.orchestrator import get_initiative_attention_bias
                    attention_bias = get_initiative_attention_bias(snapshot)
                except Exception:
                    attention_bias = dict(getattr(snapshot, "attention_summary", {}).get("initiative_bias") or {})
                result = app_state.initiative_engine.evaluate(
                    attention_summary={
                        **(getattr(snapshot, "attention_summary", None) or {}),
                        "initiative_bias": attention_bias,
                    },
                    overnight_status=_get_overnight_status(),
                )
                if result.get("selected"):
                    _emit_log(
                        "info", "initiative",
                        f"Queued: {result['selected']['initiative_type']}",
                        {"selected": result["selected"], "queue_depth": result["queue_depth"]},
                    )
                dispatched = await app_state.initiative_engine.execute_queued(
                    app_state.topology,
                    app_state.cfg,
                    tracer=app_state.tracer,
                    bus=app_state.bus,
                    entity_snapshot=snapshot,
                    overnight_status=_get_overnight_status(),
                )
                if dispatched:
                    _emit_log(
                        "info", "initiative",
                        f"Dispatched {len(dispatched)} initiative(s)",
                        {"dispatched": [d.get("initiative_type") for d in dispatched]},
                    )
                elif app_state.current_focus_service is not None and not result.get("selected"):
                    app_state.current_focus_service.set_background_focus(
                        focus_id="initiative-eval",
                        title="Initiative evaluation complete",
                        why_now="The initiative scheduler ran but did not find a stronger proactive task.",
                        next_action="Wait for the next initiative cycle or user turn.",
                        status="done",
                        source="maintenance",
                    )
            except Exception as exc:
                logger.debug("Initiative eval error: %s", exc)
                if app_state.current_focus_service is not None:
                    app_state.current_focus_service.set_background_focus(
                        focus_id="initiative-eval",
                        title="Initiative evaluation failed",
                        why_now="The initiative background loop encountered an error.",
                        next_action="Inspect the initiative error and retry on a future cycle.",
                        status="blocked",
                        source="maintenance",
                        metadata={"error": str(exc)},
                    )
        except Exception as exc:
            logger.error("Initiative loop error: %s", exc)


async def _bus_poll_loop() -> None:
    """Every 10s: drain new salient signals from the SignalBus into the admin log."""
    while True:
        try:
            await asyncio.sleep(10)
            if app_state.bus is None:
                continue
            try:
                signals = app_state.bus.get_salient_signals(min_salience=0.1, limit=50)
                for sig in signals:
                    sid = getattr(sig, "signal_id", None) or str(sig)
                    if sid in app_state.bus_seen_signals:
                        continue
                    app_state.bus_seen_signals.add(sid)
                    # Evict old IDs if set grows large
                    if len(app_state.bus_seen_signals) > 2000:
                        app_state.bus_seen_signals = set(list(app_state.bus_seen_signals)[-1000:])

                    payload = getattr(sig, "payload", {})
                    _emit_log(
                        "info",
                        f"bus:{getattr(sig, 'source', 'unknown')}",
                        f"[{getattr(sig, 'signal_type', 'signal')}] "
                        f"salience={getattr(sig, 'salience_score', 0):.2f}",
                        {
                            "signal_id":   sid,
                            "severity":    getattr(sig, "severity", "info"),
                            "payload":     payload,
                        },
                    )
            except Exception as exc:
                logger.debug("Bus poll error: %s", exc)
        except Exception as exc:
            logger.error("Bus poll loop error: %s", exc)


# ── FastAPI app ───────────────────────────────────────────────────────────

# ── Signal bus subscriber wiring ─────────────────────────────────────────────

def _wire_signal_subscribers() -> None:
    """
    Register cross-subsystem signal callbacks after all services are initialised.

    Wired routes
    ------------
    backend_health_probe → CapabilityRegistry
        Backend status transitions update the MODEL capability entry so the
        capability registry stays in sync with actual backend health.

    identity_continuity  → FocusEngine
        When IdentityContinuityMonitor signals that a name review is warranted
        (significant cross-session identity drift), the FocusEngine receives a
        high-weight introspection signal to prime the entity for self-reflection.

    relational_update    → (logging only for now)
        Relational evaluation cycle completions are acknowledged.
    """
    if app_state.bus is None:
        return

    # ── Backend health → CapabilityRegistry ──────────────────────────────────
    if app_state.capability_registry is not None:
        def _on_backend_health(env):
            try:
                payload    = env.payload or {}
                role       = payload.get("role", "")
                new_status = payload.get("new_status", "")
                if not role or not new_status:
                    return
                # Map backend status to capability status
                if new_status in ("offline",):
                    cap_status = "unavailable"
                    msg = f"backend {role} is OFFLINE"
                elif new_status in ("degraded",):
                    cap_status = "degraded"
                    msg = f"backend {role} is DEGRADED"
                else:   # healthy / recovered
                    cap_status = "active"
                    msg = f"backend {role} is HEALTHY"
                # Cap name convention: "model:{role}"
                cap_name = f"model:{role}"
                app_state.capability_registry.set_status(cap_name, cap_status, msg)
                logger.debug("[signal_wire] capability %s → %s", cap_name, cap_status)
            except Exception as exc:
                logger.debug("[signal_wire] backend→capability failed: %s", exc)

        app_state.bus.subscribe(
            _on_backend_health,
            sources=frozenset({"backend_health_probe"}),
        )
        logger.info("[signal_wire] backend_health_probe → capability_registry wired.")

    # ── Identity continuity → FocusEngine ────────────────────────────────────
    def _on_identity_continuity(env):
        try:
            payload = env.payload or {}
            if not payload.get("name_review_warranted", False):
                return
            # Import lazily — FocusEngine lives in orchestrator's module scope
            from runtime.orchestrator import _focus
            if _focus is None:
                return
            from runtime.focus_engine import FocusEngine as _FE
            sig = _FE.signal_from_initiative(
                "identity review: reflect on who I've become — does my current name still fit?"
            )
            _focus.update([sig])
            logger.info("[signal_wire] identity_continuity name_review → FocusEngine injected.")
        except Exception as exc:
            logger.debug("[signal_wire] identity_continuity→focus failed: %s", exc)

    app_state.bus.subscribe(
        _on_identity_continuity,
        signal_types=frozenset({"identity_continuity", "IDENTITY_CONTINUITY"}),
    )

    # ── Tool failure → FocusEngine ────────────────────────────────────────────
    def _on_tool_failure(env):
        try:
            from runtime.orchestrator import _focus
            if _focus is None:
                return
            from runtime.focus_engine import FocusEngine as _FE
            tool_name = (env.payload or {}).get("tool_name", env.related_entity or "unknown")
            sig = _FE.signal_from_tool_failure(tool_name)
            _focus.update([sig])
        except Exception as exc:
            logger.debug("[signal_wire] tool_failure→focus failed: %s", exc)

    from runtime.signal_bus import STYPE_TOOL_FAILURE, SEVERITY_MEDIUM
    app_state.bus.subscribe(
        _on_tool_failure,
        signal_types=frozenset({STYPE_TOOL_FAILURE}),
        min_severity=SEVERITY_MEDIUM,
    )

    logger.info("[signal_wire] Signal subscriber wiring complete.")


# ── Runtime scaffolding ───────────────────────────────────────────────────

_DEFAULT_APP_POLICY = {
    "_schema": "eos_app_policy_v1",
    "_description": "Per-application action policy for the EOS Computer Use subsystem.",
    "apps": {},
}


def _ensure_runtime_dirs(root: Path) -> None:
    """Ensure all required runtime directories and seed files exist.

    Safe to call on every boot — uses exist_ok=True throughout and only
    writes seed files when they are genuinely absent.
    """
    required_dirs = [
        root / "data",
        root / "data" / "computer_use",
        root / "data" / "computer_use" / "approved_shortcuts",
        root / "data" / "backups",
        root / "data" / "workspace",
        root / "logs",
        root / "config" / "google",
    ]
    for d in required_dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.debug("[scaffold] Could not create dir %s: %s", d, exc)

    # Seed app_policy.json if missing (Computer Use will warn without it)
    policy_path = root / "data" / "computer_use" / "app_policy.json"
    if not policy_path.is_file():
        try:
            policy_path.write_text(
                json.dumps(_DEFAULT_APP_POLICY, indent=2),
                encoding="utf-8",
            )
            logger.info("[scaffold] Seeded default app_policy.json at %s", policy_path)
        except Exception as exc:
            logger.warning("[scaffold] Could not seed app_policy.json: %s", exc)


# ── Startup & Shutdown ────────────────────────────────────────────────────

def _resolve_startup_config_path(app=None, config_path: str | Path | None = None) -> Path:
    """Resolve the canonical config path, preferring explicit parameter passing."""
    if config_path is not None:
        candidate = Path(config_path)
    elif app is not None and getattr(app.state, "config_path", None) is not None:
        candidate = Path(app.state.config_path)
    else:
        candidate = Path(os.environ.get("EOS_CONFIG", "config.json"))

    if not candidate.is_absolute():
        candidate = Path(__file__).parent.parent / candidate
    return candidate


async def startup_event(app=None, config_path: str | Path | None = None):
    """Initialize the server: load config, discover topology, init tracer."""

    try:
        app_state.startup_issues.clear()
        app_state.startup_guidance = None

        # 1. Load config
        config_path = _resolve_startup_config_path(app=app, config_path=config_path)
        config_file = str(config_path)

        if not config_path.is_file():
            _emit_log("error", "startup", f"Config not found: {config_path}")
            logger.error("Config not found: %s", config_path)
            return

        with config_path.open(encoding="utf-8") as f:
            app_state.cfg = json.load(f)
        app_state.config_path = str(config_path)   # stored for config persistence by subsystems
        _emit_log("info", "startup", f"Config loaded: {config_file}")

        # 1b. Ensure all required runtime directories and seed files exist
        _ensure_runtime_dirs(config_path.parent)

        # 2a. Security: load or generate admin token
        try:
            db_dir = Path(app_state.cfg.get("db_path", "data/entity_app_state.db")).parent
            if not db_dir.is_absolute():
                db_dir = config_path.parent / db_dir
            token = load_or_create_token(data_dir=db_dir)
            _emit_log(
                "info", "startup",
                f"Admin token ready — stored at: {get_token_file_path()}",
            )
            logger.info("[auth] Admin token file: %s", get_token_file_path())
        except Exception as exc:
            _emit_log("error", "startup", "Admin token init failed", {"error": str(exc)})
            logger.error("Admin token init failed: %s", exc)

        # 2b. Audit store
        try:
            audit_db = db_dir / "audit.db"
            init_audit_store(audit_db)
            _emit_log("info", "startup", f"Audit store ready: {audit_db}")
        except Exception as exc:
            _emit_log("error", "startup", "Audit store init failed", {"error": str(exc)})
            logger.error("Audit store init failed: %s", exc)

        # 2b2. Access controller (origin classification, tier policies, rate limiting, LAN sessions)
        try:
            init_access_controller(data_dir=db_dir, cfg=app_state.cfg)
            _emit_log("info", "startup", "Access controller ready")
        except Exception as exc:
            _emit_log("error", "startup", "Access controller init failed", {"error": str(exc)})
            logger.error("Access controller init failed: %s", exc)

        # 2c. Secrets manager (re-init with resolved data dir)
        try:
            init_secrets(data_dir=db_dir)
            _emit_log("info", "startup", "Secrets manager ready")
        except Exception as exc:
            logger.warning("Secrets manager init failed: %s", exc)

        # 2c2. External Inference — ledger + policy engine (optional; never required)
        try:
            from runtime.external_inference_ledger import init_ledger
            from runtime.external_inference_policy import init_policy
            from core.secrets import secrets_manager as _sm
            ei_ledger = init_ledger(db_path=db_dir / "entity_state.db")
            ei_policy = init_policy(cfg=app_state.cfg, secrets_manager=_sm)
            app_state.ei_policy = ei_policy
            _emit_log("info", "startup", "External inference subsystem ready (disabled by default)")
        except Exception as exc:
            logger.warning("External inference init failed (non-fatal): %s", exc)
            _emit_log("warn", "startup", "External inference subsystem init failed (non-fatal)", {"error": str(exc)})

        # 2d. Google OAuth manager (configure token/secret paths from loaded config)
        try:
            from core.google_oauth import configure as google_oauth_configure
            google_oauth_configure(app_state.cfg)
            _emit_log("info", "startup", "Google OAuth manager configured")
        except ImportError as exc:
            issue = _record_startup_issue("import_failure", "google_oauth", "import_failed", {"error": str(exc)})
            _emit_log("error", "startup", "Google OAuth import failed", issue)
            logger.error("Google OAuth import failed: %s", exc)
        except Exception as exc:
            issue = _record_startup_issue("startup_failure", "google_oauth", "configure_failed", {"error": str(exc)})
            _emit_log("error", "startup", "Google OAuth configure failed", issue)
            logger.error("Google OAuth configure failed: %s", exc)

        # 2. Init memory/db
        try:
            memory_configure(app_state.cfg)
            orchestrator_startup(app_state.cfg)
            _emit_log("info", "startup", "Orchestrator initialized")
        except Exception as exc:
            _emit_log("error", "startup", "Orchestrator init failed", {"error": str(exc)})
            logger.error("Orchestrator init failed: %s", exc)

        # 3. Discover topology from already-running services
        app_state.runtime_discovery = discover_runtime(config_path, root=config_path.parent)
        app_state.topology = app_state.runtime_discovery.topology
        app_state.primary_degraded = app_state.topology.server("primary") is None or not app_state.topology.server("primary").is_ready()
        app_state.startup_guidance = detect_startup_guidance(app_state.runtime_discovery)
        _emit_log("info", "startup", "Runtime discovery complete", app_state.runtime_discovery.to_dict())
        logger.info("Runtime discovery complete: %s", app_state.topology)
        if app_state.startup_guidance:
            issue = _record_startup_issue("runtime_guidance", "backends", "no_backends_running", {"message": app_state.startup_guidance})
            _emit_log("warn", "startup", app_state.startup_guidance, issue)

        # 3a. On-demand server manager (tool / thinking / creativity start only when needed)
        try:
            from runtime.on_demand import init_on_demand_manager

            def _interaction_age_provider():
                if not app_state.last_interaction_monotonic:
                    return None
                return max(0.0, time.monotonic() - app_state.last_interaction_monotonic)

            app_state.server_activation_manager = init_on_demand_manager(
                app_state.cfg,
                config_path.parent,
                app_state.topology,
                interaction_age_provider=_interaction_age_provider,
            )
            idle_task = app_state.server_activation_manager.start_idle_loop()
            _track_background_task("on_demand_idle_loop", idle_task)
            _emit_log("info", "startup", "Elastic auxiliary server manager initialised")
        except Exception as exc:
            logger.warning("OnDemandServerManager init failed: %s", exc)

        # 3b. Crash recovery — record this boot and detect unclean shutdowns
        _crash_report = None
        try:
            from runtime.crash_recovery import CrashRecoveryService
            app_state.crash_recovery = CrashRecoveryService(app_state.cfg)
            mode = app_state.cfg.get("deployment_mode", "standard")
            _crash_report = app_state.crash_recovery.record_boot(config_mode=mode)
            if _crash_report.is_crash_recovery():
                _emit_log("warn", "startup", "Crash recovery detected", _crash_report.to_dict())
                logger.warning("[crash_recovery] %s", _crash_report.admin_summary())
            else:
                _emit_log("info", "startup", "Boot ledger updated", {"kind": _crash_report.previous_shutdown_kind})
        except Exception as exc:
            logger.warning("CrashRecoveryService init failed: %s", exc)

        # 3c. Entity lifecycle — deterministic operational history (uses crash report)
        try:
            from runtime.entity_lifecycle import EntityLifecycleService
            app_state.entity_lifecycle = EntityLifecycleService(app_state.cfg, crash_report=_crash_report)
            summary = app_state.entity_lifecycle.lifecycle_summary()
            _emit_log("info", "startup", "Entity lifecycle loaded",
                      {"boot": summary.boot_count, "reason": summary.boot_reason,
                       "entity_id": summary.entity_id})
            logger.info("[entity_lifecycle] %s", summary.compact())
        except Exception as exc:
            logger.warning("EntityLifecycleService init failed: %s", exc)

        # 3c2. Entity state service — shared snapshot authority for turns/background loops
        try:
            from runtime.entity_state_service import EntityStateService
            app_state.entity_state_service = EntityStateService(app_state.cfg)
            app_state.entity_state_service.wire(
                topology=app_state.topology,
                runtime_discovery=app_state.runtime_discovery,
                lifecycle_service=app_state.entity_lifecycle,
            )
            _emit_log("info", "startup", "EntityStateService initialized")
        except Exception as exc:
            logger.warning("EntityStateService init failed: %s", exc)

        # 3c3. Current focus service — one shared "what am I doing now?" record
        try:
            from runtime.current_focus import CurrentFocusService
            app_state.current_focus_service = CurrentFocusService()
            _emit_log("info", "startup", "CurrentFocusService initialized")
        except Exception as exc:
            logger.warning("CurrentFocusService init failed: %s", exc)

        # 3d. Session continuity — load prior session excerpt for system prompt primer
        try:
            from runtime.session_continuity import SessionContinuityService
            _lc_boot = 0
            if app_state.entity_lifecycle is not None:
                try:
                    _lc_boot = app_state.entity_lifecycle.lifecycle_summary().boot_count
                except Exception:
                    pass
            app_state.session_continuity = SessionContinuityService(app_state.cfg)
            has_prior = app_state.session_continuity.has_prior_session()
            _emit_log("info", "startup", "SessionContinuity loaded",
                      {"has_prior_session": has_prior})
            if has_prior:
                logger.info("[session_continuity] Prior session primer available.")
        except Exception as exc:
            logger.warning("SessionContinuityService init failed: %s", exc)
        finally:
            if app_state.entity_state_service is not None:
                app_state.entity_state_service.wire(
                    topology=app_state.topology,
                    runtime_discovery=app_state.runtime_discovery,
                    lifecycle_service=app_state.entity_lifecycle,
                    session_continuity=app_state.session_continuity,
                    current_focus_service=app_state.current_focus_service,
                )

        # 3e. Goal store — load durable goals
        try:
            from core.intent import GoalStore
            app_state.goal_store = GoalStore(app_state.cfg.get("db_path", "data/entity_app_state.db"))
            active_n = app_state.goal_store.active_count()
            _emit_log("info", "startup", "GoalStore loaded", {"active_goals": active_n})
            if active_n:
                logger.info("[goal_store] %d active goal(s) loaded.", active_n)
        except Exception as exc:
            logger.warning("GoalStore init failed: %s", exc)
        finally:
            if app_state.entity_state_service is not None:
                app_state.entity_state_service.wire(
                    topology=app_state.topology,
                    runtime_discovery=app_state.runtime_discovery,
                    lifecycle_service=app_state.entity_lifecycle,
                    session_continuity=app_state.session_continuity,
                    goal_store=app_state.goal_store,
                    current_focus_service=app_state.current_focus_service,
                )
            if app_state.current_focus_service is not None:
                app_state.current_focus_service.wire(goal_store=app_state.goal_store)

        # 3f. Workspace service — first-class persistent environment for the entity
        try:
            from runtime.workspace_service import WorkspaceService
            app_state.workspace_service = WorkspaceService(app_state.cfg)
            ws_state = app_state.workspace_service.state()
            ctx_n = len(ws_app_state.context_documents) if ws_state else 0
            file_n = ws_app_state.total_files if ws_state else 0
            _emit_log("info", "startup", "WorkspaceService initialized",
                      {"files": file_n, "context_docs": ctx_n,
                       "root": app_state.workspace_service.root_path()})
            logger.info("[workspace] Ready. %d files, %d context doc(s).", file_n, ctx_n)
        except Exception as exc:
            logger.warning("WorkspaceService init failed: %s", exc)
        finally:
            if app_state.entity_state_service is not None:
                app_state.entity_state_service.wire(
                    topology=app_state.topology,
                    runtime_discovery=app_state.runtime_discovery,
                    lifecycle_service=app_state.entity_lifecycle,
                    session_continuity=app_state.session_continuity,
                    goal_store=app_state.goal_store,
                    current_focus_service=app_state.current_focus_service,
                    workspace_service=app_state.workspace_service,
                )

        # 3g. Worldview service — passive partner orientation subsystem
        try:
            from core.worldview import WorldviewService
            app_state.worldview_service = WorldviewService(app_state.cfg)
            wv_summary = app_state.worldview_service.profile_summary()
            _emit_log("info", "startup", "WorldviewService initialized",
                      {"profile_exists": wv_summary["profile_exists"],
                       "sources_processed": wv_summary["sources_processed"]})
            logger.info(
                "[worldview] Ready. Profile exists: %s, Sources processed: %d.",
                wv_summary["profile_exists"], wv_summary["sources_processed"],
            )
        except Exception as exc:
            logger.warning("WorldviewService init failed: %s", exc)
        finally:
            if app_state.entity_state_service is not None:
                app_state.entity_state_service.wire(
                    topology=app_state.topology,
                    runtime_discovery=app_state.runtime_discovery,
                    lifecycle_service=app_state.entity_lifecycle,
                    session_continuity=app_state.session_continuity,
                    goal_store=app_state.goal_store,
                    workspace_service=app_state.workspace_service,
                    worldview_service=app_state.worldview_service,
                    overnight_cycle_service=app_state.overnight_cycle_service,
                )

        # 3h. Backup service — state snapshots and restore path
        try:
            from runtime.backup_service import BackupService
            app_state.backup_service = BackupService(app_state.cfg)
            integrity = app_state.backup_service.integrity_check()
            if not integrity.ok:
                _emit_log("warn", "startup", "Integrity issues detected",
                          {"issues": integrity.findings})
                logger.warning("[backup] Integrity issues: %s", integrity.findings)
            backups = app_state.backup_service.list_backups()
            _emit_log("info", "startup", "BackupService initialized",
                      {"total_backups": len(backups)})
        except Exception as exc:
            logger.warning("BackupService init failed: %s", exc)

        # 4. Init CognitionTracer if available
        if CognitionTracer is not None:
            try:
                trace_cfg = app_state.cfg.get("cognition", {})
                if trace_cfg.get("enable_cognition_trace", True):
                    app_state.tracer = CognitionTracer(
                        turn_ring_size=trace_cfg.get("turn_ring_size", 200),
                        reflection_ring_size=trace_cfg.get("reflection_ring_size", 100),
                        state_ring_size=trace_cfg.get("state_ring_size", 100),
                    )
                    _emit_log("info", "startup", "CognitionTracer initialized")
            except Exception as exc:
                logger.warning("CognitionTracer init failed: %s", exc)

        # 4b. Init SignalBus if available
        if SignalBus is not None:
            try:
                app_state.bus = SignalBus()
                _emit_log("info", "startup", "SignalBus initialized")
            except Exception as exc:
                logger.warning("SignalBus init failed: %s", exc)

        # 4b2. Computer Use subsystem — must init after SignalBus so bus= is live
        try:
            cu_cfg = app_state.cfg.get("computer_use", {})
            if cu_cfg.get("enabled", False):
                from runtime.computer_use_service import ComputerUseService
                # Inject root path so service can resolve data/ paths relative to project root
                cfg_with_root = dict(app_state.cfg)
                cfg_with_root["_root"] = str(config_path.parent)
                app_state.computer_use_service = ComputerUseService(cfg_with_root, bus=app_state.bus)
                default_mode = cu_cfg.get("default_mode", "off")
                if default_mode != "off":
                    app_state.computer_use_service.set_mode(default_mode, reason="config_default")
                state = app_state.computer_use_service.get_state()
                state_mode = state.get("mode") if isinstance(state, dict) else getattr(state, "mode", None)
                state_shortcuts = (
                    state.get("approved_shortcuts", [])
                    if isinstance(state, dict)
                    else getattr(state, "approved_shortcuts", [])
                )
                _emit_log("info", "startup", "ComputerUseService initialized", {
                    "mode":      state_mode,
                    "shortcuts": len(state_shortcuts),
                })
                logger.info(
                    "[computer_use] Initialized. mode=%s shortcuts=%d",
                    state_mode, len(state_shortcuts),
                )
            else:
                logger.info("[computer_use] Disabled in config (computer_use.enabled=false). Subsystem not loaded.")
        except Exception as exc:
            logger.warning("ComputerUseService init failed: %s", exc)
        finally:
            if app_state.entity_state_service is not None:
                app_state.entity_state_service.wire(
                    topology=app_state.topology,
                    runtime_discovery=app_state.runtime_discovery,
                    lifecycle_service=app_state.entity_lifecycle,
                    session_continuity=app_state.session_continuity,
                    goal_store=app_state.goal_store,
                    current_focus_service=app_state.current_focus_service,
                    workspace_service=app_state.workspace_service,
                    worldview_service=app_state.worldview_service,
                    computer_use_service=app_state.computer_use_service,
                    overnight_cycle_service=app_state.overnight_cycle_service,
                )

        # 4c. Init CapabilityRegistry
        try:
            from runtime.capability_registry import build_default_registry
            app_state.capability_registry = build_default_registry(app_state.cfg, app_state.topology)
            _emit_log("info", "startup", "CapabilityRegistry initialized",
                      {"total": len(app_state.capability_registry.all())})
        except Exception as exc:
            logger.warning("CapabilityRegistry init failed: %s", exc)
        finally:
            if app_state.entity_state_service is not None:
                app_state.entity_state_service.wire(
                    topology=app_state.topology,
                    runtime_discovery=app_state.runtime_discovery,
                    lifecycle_service=app_state.entity_lifecycle,
                    session_continuity=app_state.session_continuity,
                    goal_store=app_state.goal_store,
                    workspace_service=app_state.workspace_service,
                    worldview_service=app_state.worldview_service,
                    capability_registry=app_state.capability_registry,
                    computer_use_service=app_state.computer_use_service,
                    overnight_cycle_service=app_state.overnight_cycle_service,
                )

        # 4d. Init SensorPoller (hardware self-observation)
        try:
            from runtime.system_sensors import SensorPoller
            app_state.sensor_poller = SensorPoller(cfg=app_state.cfg, topology=app_state.topology)
            app_state.sensor_poller.start()
            _emit_log("info", "startup", "SensorPoller started")
        except ImportError as exc:
            issue = _record_startup_issue("import_failure", "system_sensors", "import_failed", {"error": str(exc)})
            _emit_log("error", "startup", "SensorPoller import failed", issue)
            logger.warning("SensorPoller import failed: %s", exc)
        except Exception as exc:
            logger.warning("SensorPoller init failed: %s", exc)

        # 4e. Init BackendHealthProbe (replaces/augments _server_health_loop)
        try:
            from runtime.backend_health_probe import BackendHealthProbe
            app_state.backend_probe = BackendHealthProbe(
                topology=app_state.topology,
                signal_bus=app_state.bus,
                interval_seconds=float(app_state.cfg.get("health_probe", {}).get("interval_seconds", 60)),
                failure_threshold=int(app_state.cfg.get("health_probe", {}).get("failure_threshold", 3)),
                degraded_latency_ms=float(app_state.cfg.get("health_probe", {}).get("degraded_latency_ms", 2000)),
            )
            app_state.backend_probe.start()
            _emit_log("info", "startup", "BackendHealthProbe started")
        except ImportError as exc:
            issue = _record_startup_issue("import_failure", "backend_health_probe", "import_failed", {"error": str(exc)})
            _emit_log("error", "startup", "BackendHealthProbe import failed", issue)
            logger.warning("BackendHealthProbe import failed: %s", exc)
        except Exception as exc:
            logger.warning("BackendHealthProbe init failed: %s", exc)

        # 4f. Init IdleCognitionEngine
        try:
            from runtime.idle_cognition import IdleCognitionEngine
            app_state.idle_cognition = IdleCognitionEngine(app_state.cfg)
            _emit_log("info", "startup", "IdleCognitionEngine initialized")
        except Exception as exc:
            logger.warning("IdleCognitionEngine init failed: %s", exc)

        # 4f2. Init IdentityContinuityMonitor
        try:
            from runtime.identity_continuity import IdentityContinuityMonitor
            db_path = app_state.cfg.get("db_path", "data/entity_app_state.db")
            app_state.identity_continuity = IdentityContinuityMonitor(db_path)
            snap_count = app_state.identity_continuity.snapshot_count()
            score = app_state.identity_continuity.stability_score()
            _emit_log("info", "startup", "IdentityContinuityMonitor initialized",
                      {"snapshots": snap_count, "stability": score})
        except Exception as exc:
            logger.warning("IdentityContinuityMonitor init failed: %s", exc)

        # 4f3. Init OvernightCycleService
        try:
            from runtime.overnight_store import OvernightCycleStore
            from runtime.overnight_cycle import OvernightCycleService

            overnight_store = OvernightCycleStore(app_state.cfg.get("db_path", "data/entity_state.db"))
            app_state.overnight_cycle_service = OvernightCycleService(app_state.cfg, overnight_store)
            _emit_log("info", "startup", "OvernightCycleService initialized")
            if app_state.entity_state_service is not None:
                app_state.entity_state_service.wire(
                    topology=app_state.topology,
                    runtime_discovery=app_state.runtime_discovery,
                    lifecycle_service=app_state.entity_lifecycle,
                    session_continuity=app_state.session_continuity,
                    goal_store=app_state.goal_store,
                    workspace_service=app_state.workspace_service,
                    worldview_service=app_state.worldview_service,
                    capability_registry=app_state.capability_registry,
                    initiative_engine=app_state.initiative_engine,
                    tool_registry=app_state.tool_registry,
                    computer_use_service=app_state.computer_use_service,
                    overnight_cycle_service=app_state.overnight_cycle_service,
                )
        except Exception as exc:
            logger.warning("OvernightCycleService init failed: %s", exc)

        if app_state.server_activation_manager is not None:
            try:
                app_state.server_activation_manager.bind_runtime_providers(
                    sensor_provider=(lambda: app_state.sensor_poller.snapshot() if app_state.sensor_poller is not None else None),
                    posture_provider=(lambda: _get_overnight_status(include_history=False)),
                )
                _emit_log("info", "startup", "Elastic auxiliary policy providers bound")
            except Exception as exc:
                logger.warning("Elastic auxiliary provider binding failed: %s", exc)

        # 4g. Init InitiativeEngine
        try:
            from runtime.initiative_engine import InitiativeEngine
            app_state.initiative_engine = InitiativeEngine(app_state.cfg)
            _emit_log("info", "startup", "InitiativeEngine initialized")
            if app_state.current_focus_service is not None:
                app_state.current_focus_service.wire(initiative_engine=app_state.initiative_engine)
            if app_state.entity_state_service is not None:
                app_state.entity_state_service.wire(initiative_engine=app_state.initiative_engine, overnight_cycle_service=app_state.overnight_cycle_service)
        except Exception as exc:
            logger.warning("InitiativeEngine init failed: %s", exc)

        # 4h. Init InvestigationEngine
        try:
            from runtime.investigation_engine import InvestigationEngine
            app_state.investigation_engine = InvestigationEngine(app_state.cfg)
            _emit_log("info", "startup", "InvestigationEngine initialized")
            if app_state.current_focus_service is not None:
                app_state.current_focus_service.wire(investigation_engine=app_state.investigation_engine)
        except Exception as exc:
            logger.warning("InvestigationEngine init failed: %s", exc)

        # 5. Init ToolRegistry and load toolpacks
        try:
            from runtime.tool_registry import ToolRegistry
            from runtime.toolpack_loader import ToolpackLoader
            app_state.tool_registry = ToolRegistry()
            loader = ToolpackLoader(registry=app_state.tool_registry, config=app_state.cfg)
            manifest = loader.load_all()
            # Seed app_state.tool_states and app_state.toolpack_states from the live registry
            for spec in app_state.tool_registry.all_tools():
                app_state.tool_states[spec.name] = spec.enabled
            for pack_entry in manifest.get("packs", []):
                app_state.toolpack_states[pack_entry["pack"]] = pack_entry.get("loaded", False)
            loaded  = manifest["summary"]["loaded"]
            total   = manifest["summary"]["total"]
            failed  = manifest["summary"]["failed"]
            _emit_log("info", "startup", f"Toolpacks: {loaded}/{total} loaded, {failed} failed",
                      manifest["summary"])

            # Wire the ToolExecutor into the orchestrator now that the registry is ready
            try:
                from runtime.orchestrator import wire_executor
                wire_executor(app_state.tool_registry, get_audit_store())
                _emit_log("info", "startup", "ToolExecutor wired to orchestrator")
            except Exception as _wexc:
                logger.warning("ToolExecutor wiring failed: %s", _wexc)

            # Wire the live ToolRegistry into entity.py so system prompts show
            # actual registered tools instead of the legacy dispatcher list.
            try:
                from core.entity import wire_tool_registry as _wire_entity_registry
                _wire_entity_registry(app_state.tool_registry)
                _emit_log("info", "startup", "ToolRegistry wired to entity system prompt")
            except Exception as _wexc:
                logger.warning("Entity tool registry wiring failed: %s", _wexc)
            if app_state.entity_state_service is not None:
                app_state.entity_state_service.wire(
                    topology=app_state.topology,
                    runtime_discovery=app_state.runtime_discovery,
                    lifecycle_service=app_state.entity_lifecycle,
                    session_continuity=app_state.session_continuity,
                    goal_store=app_state.goal_store,
                    current_focus_service=app_state.current_focus_service,
                    workspace_service=app_state.workspace_service,
                    worldview_service=app_state.worldview_service,
                    capability_registry=app_state.capability_registry,
                    initiative_engine=app_state.initiative_engine,
                    tool_registry=app_state.tool_registry,
                    computer_use_service=app_state.computer_use_service,
                    overnight_cycle_service=app_state.overnight_cycle_service,
                )
        except Exception as exc:
            logger.warning("Toolpack init failed — falling back to legacy tool states: %s", exc)
            # Fallback: seed app_state.tool_states from old dispatcher
            try:
                from tools.dispatcher import TOOL_SCHEMA
                for tool_name in TOOL_SCHEMA:
                    app_state.tool_states[tool_name] = True
            except Exception:
                pass

        # 6. Seed permission allowlist
        try:
            default_allowlist = app_state.cfg.get("tools", {}).get("permission_class_allowlist", [
                "system.core",
                "tools.perception",
                "tools.memory",
            ])
            app_state.perm_allowlist.update(default_allowlist)
        except Exception as exc:
            logger.warning("Failed to seed permission allowlist: %s", exc)

        # 7. Seed toolpack states (fallback if registry didn't load)
        if not app_state.toolpack_states:
            toolpacks = _group_tools_by_category()
            for pack_name in toolpacks:
                app_state.toolpack_states[pack_name] = True

        # 7b. Reconcile capability statuses — mark each subsystem ENABLED/OFFLINE/DISABLED
        #     based on actual init outcome.  Replaces the UNKNOWN seeds from build_default_registry.
        try:
            if app_state.capability_registry is not None:
                from runtime.capability_registry import CapabilityStatus, CapabilityEntry, CapabilityKind

                # Memory stores: if orchestrator init succeeded they are available
                app_state.capability_registry.set_status("sqlite_memory", CapabilityStatus.ENABLED, "init complete")
                app_state.capability_registry.set_status("vector_memory", CapabilityStatus.ENABLED, "init complete")

                # TTS: probe Piper binary
                tts_cfg = app_state.cfg.get("tts", {})
                if tts_cfg:
                    _piper = Path(tts_cfg.get("binary", "Piper/piper/piper.exe"))
                    if not _piper.is_absolute():
                        _piper = config_path.parent / _piper
                    _tts_ok = _piper.is_file()
                    app_state.capability_registry.set_status(
                        "tts",
                        CapabilityStatus.ENABLED if _tts_ok else CapabilityStatus.OFFLINE,
                        "binary found" if _tts_ok else f"binary not found: {_piper}",
                    )
                    if not _tts_ok:
                        _emit_log("warn", "startup", "TTS offline — Piper binary not found", {"path": str(_piper)})

                # Cognitive subsystems: mark ENABLED if object was created
                _subsystem_caps = [
                    (app_state.initiative_engine,    "initiative_engine"),
                    (app_state.idle_cognition,       "idle_cognition"),
                    (app_state.investigation_engine, "investigation_engine"),
                    (app_state.identity_continuity,  "identity_continuity"),
                    (app_state.overnight_cycle_service, "overnight_cycle"),
                ]
                for _obj, _cap_name in _subsystem_caps:
                    if _obj is not None:
                        app_state.capability_registry.set_status(_cap_name, CapabilityStatus.ENABLED)

                # Tool catalog: register each tool as a TOOL capability (enabled or disabled)
                if app_state.tool_registry is not None:
                    for _spec in app_state.tool_registry.all_tools():
                        _cap_name = f"tool:{_spec.name}"
                        if app_state.capability_registry.get(_cap_name) is None:
                            app_state.capability_registry.register(CapabilityEntry(
                                name=_cap_name,
                                kind=CapabilityKind.TOOL,
                                status=CapabilityStatus.ENABLED if _spec.enabled else CapabilityStatus.DISABLED,
                                healthy=_spec.enabled,
                                policy="optional",
                                version=getattr(_spec, "pack", ""),
                                metadata={
                                    "pack":  getattr(_spec, "pack", ""),
                                    "risk":  str(getattr(_spec, "risk_level", "")),
                                    "trust": str(getattr(_spec, "trust_level", "")),
                                },
                            ))

                _emit_log("info", "startup", "Capability statuses reconciled",
                          {"total": len(app_state.capability_registry.all())})
        except Exception as exc:
            logger.warning("Capability reconciliation failed: %s", exc)

        # 8. Start background tasks
        task1 = _track_background_task("server_health_loop", asyncio.create_task(_server_health_loop()))

        task2 = _track_background_task("bus_poll_loop", asyncio.create_task(_bus_poll_loop()))

        task4 = _track_background_task("initiative_loop", asyncio.create_task(_initiative_loop()))

        task5 = _track_background_task("memory_maintenance_loop", asyncio.create_task(_memory_maintenance_loop()))

        task6 = _track_background_task("backup_loop", asyncio.create_task(_backup_loop()))

        # Reflection pipeline (identity eval on schedule)
        try:
            from runtime.reflection_pipeline import ReflectionPipeline
            app_state.reflection_pipeline = ReflectionPipeline(app_state.cfg)
            if app_state.topology:
                task3 = asyncio.create_task(
                    app_state.reflection_pipeline.run_loop(
                        app_state.topology,
                        tracer=app_state.tracer,
                        bus=app_state.bus,
                        entity_state_service=app_state.entity_state_service,
                    )
                )
                _track_background_task("reflection_pipeline", task3)
                _emit_log("info", "startup", "ReflectionPipeline started")
                if app_state.current_focus_service is not None:
                    app_state.current_focus_service.set_background_focus(
                        focus_id="reflection-scheduler",
                        title="Monitor reflection schedule",
                        why_now="Reflection and relational evaluation loops are running in the background.",
                        next_action="Wait for the next reflection trigger and refresh identity app_state.",
                        status="waiting",
                        source="maintenance",
                    )
        except Exception as exc:
            logger.warning("ReflectionPipeline start failed: %s", exc)

        # Idle cognition scheduler (fires probabilistically when no user interaction)
        async def _idle_cognition_loop():
            import time as _time
            while True:
                await asyncio.sleep(900)  # check every 15 min
                if app_state.idle_cognition is None or app_state.topology is None:
                    continue
                try:
                    if app_state.current_focus_service is not None:
                        app_state.current_focus_service.set_background_focus(
                            focus_id="idle-cognition",
                            title="Check whether idle cognition should fire",
                            why_now="The idle cognition scheduler woke up after a quiet period.",
                            next_action="Assess inactivity and optionally fire a background thought.",
                            status="active",
                            source="maintenance",
                        )
                    snapshot = _build_entity_snapshot(
                        scope="background",
                        source="idle_cognition.loop",
                        metadata={"loop": "idle_cognition"},
                    )
                    await app_state.idle_cognition.maybe_fire(
                        app_state.topology, app_state.tracer, app_state.bus,
                        entity_snapshot=snapshot,
                        overnight_status=_get_overnight_status(),
                    )
                    if app_state.current_focus_service is not None:
                        app_state.current_focus_service.set_background_focus(
                            focus_id="idle-cognition",
                            title="Idle cognition check complete",
                            why_now="The idle cognition scheduler finished its latest review.",
                            next_action="Wait for the next idle-cognition window.",
                            status="done",
                            source="maintenance",
                        )
                except Exception as _exc:
                    logger.debug("idle_cognition loop error: %s", _exc)

        task_idle = _track_background_task("idle_cognition_loop", asyncio.create_task(_idle_cognition_loop()))

        # Discord bot (optional, config-gated)
        if app_state.cfg.get("discord", {}).get("enabled", False) and app_state.topology:
            try:
                from interfaces.discord_bot import start as discord_start
                task_discord = asyncio.create_task(
                    discord_start(
                        app_state.topology, app_state.cfg,
                        tracer=app_state.tracer,
                        bus=app_state.bus,
                        turn_notifiers=[_build_discord_turn_notifier()],
                    )
                )
                _track_background_task("discord_bot", task_discord)
                _emit_log("info", "startup", "Discord bot started")
            except Exception as exc:
                logger.warning("Discord bot start failed: %s", exc)
                _emit_log("warn", "startup", "Discord bot failed to start", {"error": str(exc)})

        # Wire signal bus subscribers (subsystem coordination)
        if app_state.bus is not None:
            _wire_signal_subscribers()

        _emit_log("info", "startup", "EOS WebUI server started")
        logger.info("EOS WebUI server started")

    except Exception as exc:
        logger.error("Startup error: %s", exc)
        _emit_log("error", "startup", "Unexpected startup error", {"error": str(exc)})


async def shutdown_event():
    """Graceful shutdown: stop runtime tasks without owning backend processes."""

    try:
        from runtime.on_demand import get_on_demand_manager
        _odm = get_on_demand_manager()
        if _odm is not None:
            await _odm.shutdown_all()
    except Exception as exc:
        logger.warning("OnDemandServerManager shutdown failed: %s", exc)

    try:
        # Record clean shutdown before anything else (lifecycle first, then crash ledger)

        # Save session continuity excerpt for next boot's primer
        if app_state.session_continuity is not None:
            try:
                from core.memory import get_recent_interactions, count_interactions
                recent = get_recent_interactions(n=10)
                total  = count_interactions()
                boot_n = 0
                if app_state.entity_lifecycle is not None:
                    try:
                        boot_n = app_state.entity_lifecycle.lifecycle_summary().boot_count
                    except Exception:
                        pass
                app_state.session_continuity.save_session_end(
                    recent_turns=recent,
                    total_turn_count=total,
                    boot_count=boot_n,
                )
            except Exception as exc:
                logger.warning("SessionContinuity save_session_end failed: %s", exc)

        if app_state.entity_lifecycle is not None:
            try:
                app_state.entity_lifecycle.record_shutdown()
            except Exception as exc:
                logger.warning("EntityLifecycle record_shutdown failed: %s", exc)
        if app_state.crash_recovery is not None:
            try:
                app_state.crash_recovery.record_shutdown()
            except Exception as exc:
                logger.warning("CrashRecovery record_shutdown failed: %s", exc)

        # Stop background probes
        if app_state.sensor_poller is not None:
            try:
                app_state.sensor_poller.stop()
            except Exception:
                pass
        if app_state.backend_probe is not None:
            try:
                app_state.backend_probe.stop()
            except Exception:
                pass

        # Cancel background tasks
        for task in app_state.background_tasks:
            if not task.done():
                task.cancel()

        _emit_log("info", "shutdown", "Runtime shutdown complete")
        logger.info("Runtime shutdown complete")

    except Exception as exc:
        logger.error("Shutdown error: %s", exc)


# ── User-facing endpoints ────────────────────────────────────────────────

async def get_index():
    """Serve user chat UI."""
    html_path = Path(__file__).parent / "index.html"
    if html_path.is_file():
        return FileResponse(html_path, media_type="text/html")
    return JSONResponse(
        {"ok": False, "error": "UI not found"},
        status_code=404
    )


async def get_workspace():
    """Serve workspace chat UI (original index)."""
    html_path = Path(__file__).parent / "index_workspace.html"
    if html_path.is_file():
        return FileResponse(html_path, media_type="text/html")
    return JSONResponse(
        {"ok": False, "error": "Workspace UI not found"},
        status_code=404
    )


async def get_admin():
    """Serve admin panel."""
    html_path = Path(__file__).parent / "admin.html"
    if html_path.is_file():
        return FileResponse(html_path, media_type="text/html")
    return JSONResponse(
        {"ok": False, "error": "Admin UI not found"},
        status_code=404
    )


async def get_docs():
    """Serve documentation page."""
    html_path = Path(__file__).parent / "docs.html"
    if html_path.is_file():
        return FileResponse(html_path, media_type="text/html")
    return JSONResponse(
        {"ok": False, "error": "Docs not found"},
        status_code=404
    )


async def get_docs_content(page: str):
    """Serve a documentation content fragment."""
    import re
    if not re.match(r'^[a-z0-9-]+$', page):
        return JSONResponse({"ok": False, "error": "Invalid page"}, status_code=400)
    html_path = Path(__file__).parent / "docs" / f"{page}.html"
    if html_path.is_file():
        return FileResponse(html_path, media_type="text/html")
    return JSONResponse({"ok": False, "error": "Page not found"}, status_code=404)


async def favicon():
    """Favicon endpoint — return empty icon to suppress browser 404s."""
    from starlette.responses import Response as _Resp
    return _Resp(content=b"", status_code=200, media_type="image/x-icon")


async def get_status_endpoint():
    """Get entity status: name, identity domains, interaction count."""
    if not app_state.topology:
        return JSONResponse(
            {"ok": False, "error": "Topology not ready"},
            status_code=503
        )
    try:
        status = get_status(app_state.cfg)
        topology_summary = app_state.topology.status_summary()
        snapshot = _build_entity_snapshot(
            scope="status",
            source="/api/status",
            metadata={"endpoint": "/api/status"},
        )

        # Build model_status from topology so the frontend vision-capability check works
        model_status: dict = {}
        for role, srv in app_state.topology.servers.items():
            model_status[role] = {
                "model_name": role,
                "enabled": srv.is_ready(),
                "status": srv.status.value if hasattr(srv.status, "value") else str(srv.status),
            }

        return JSONResponse({
            "ok": True,
            "session": {
                "session_id": app_state.session_id,
                "turn_count": status.get("interaction_count", 0),
            },
            "boot_time_s": int(time.time() - topology_summary["boot_time"]) if topology_summary.get("boot_time") else None,
            "identity": {
                "name": snapshot.name if snapshot is not None else status.get("name"),
                "stable_domains": (
                    snapshot.identity_summary.get("stable_count", 0)
                    if snapshot is not None else status.get("identity_stable_domains", 0)
                ),
                "total_domains": (
                    snapshot.identity_summary.get("total_domains", 6)
                    if snapshot is not None else status.get("total_domains", 6)
                ),
            },
            "model_status": model_status,
            "topology": topology_summary,
            "capabilities": app_state.runtime_discovery.capabilities if app_state.runtime_discovery else {},
            "services": app_state.runtime_discovery.to_dict().get("services", {}) if app_state.runtime_discovery else {},
            "startup_guidance": app_state.startup_guidance,
            "startup_issues": list(app_state.startup_issues),
            "activation_policy": app_state.server_activation_manager.status() if app_state.server_activation_manager else None,
            "current_focus": _get_current_focus_dict(),
            "overnight": _get_overnight_status(),
            "entity_state": snapshot.to_dict() if snapshot is not None else None,
        })
    except Exception as exc:
        logger.error("Status endpoint error: %s", exc)
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=500
        )


async def get_tools_list():
    """Get list of tools with enabled state and description."""
    try:
        if app_state.tool_registry is not None:
            tools_list = [
                {
                    "name":    spec.name,
                    "enabled": spec.enabled,
                    "description": spec.description,
                    "pack":    spec.pack,
                }
                for spec in app_state.tool_registry.all_tools()
            ]
        else:
            tools_list = [
                {"name": name, "enabled": app_state.tool_states.get(name, True), "description": ""}
                for name in app_state.tool_states.keys()
            ]
        return JSONResponse({"ok": True, "tools": tools_list})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def get_memory_recent(limit: int = 20):
    """Get recent interactions."""
    try:
        interactions = get_recent_interactions(limit)
        return JSONResponse({"ok": True, "memories": interactions})
    except Exception as exc:
        logger.error("Memory recent error: %s", exc)
        return JSONResponse({"ok": True, "memories": [], "error": str(exc)})


async def get_presence():
    """Return the current presence-layer cues and supporting context."""
    try:
        snapshot = _build_entity_snapshot(
            scope="turn",
            source="api.presence",
            metadata={"endpoint": "/api/presence"},
        )
        payload = _build_presence_payload(entity_snapshot=snapshot)
        return JSONResponse({
            "ok": True,
            "presence": payload,
            "current_focus": _get_current_focus_dict(),
        })
    except Exception as exc:
        logger.error("Presence endpoint error: %s", exc)
        return JSONResponse({"ok": True, "presence": None, "current_focus": {}, "error": str(exc)})


async def get_overnight_status():
    """Return the lightweight user-facing overnight status payload."""
    try:
        return JSONResponse({"ok": True, "overnight": _get_overnight_status(include_history=False)})
    except Exception as exc:
        logger.error("Overnight status endpoint error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def post_overnight_cancel(body: OvernightCancelRequest):
    """Cancel the active overnight cycle."""
    try:
        if app_state.overnight_cycle_service is None:
            return JSONResponse({"ok": False, "error": "OvernightCycleService not initialized"}, status_code=503)
        cancelled = app_state.overnight_cycle_service.cancel_current()
        _emit_log("info", "overnight", "Overnight cycle cancelled", {"reason": body.reason, "cancelled": cancelled})
        return JSONResponse({"ok": True, "overnight": _get_overnight_status(include_history=False), "cancelled": cancelled})
    except Exception as exc:
        logger.error("Overnight cancel endpoint error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def post_overnight_return_time(body: OvernightReturnTimeRequest):
    """Update the active overnight cycle's expected return timestamp."""
    try:
        if app_state.overnight_cycle_service is None:
            return JSONResponse({"ok": False, "error": "OvernightCycleService not initialized"}, status_code=503)
        updated = app_state.overnight_cycle_service.update_expected_return_time(
            expected_return_time=body.expected_return_time
        )
        if updated is None:
            return JSONResponse({"ok": False, "error": "No active overnight cycle"}, status_code=404)
        _emit_log("info", "overnight", "Overnight return time updated", {"updated": updated})
        return JSONResponse({"ok": True, "overnight": _get_overnight_status(include_history=False), "updated": updated})
    except Exception as exc:
        logger.error("Overnight update endpoint error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def get_initiative():
    """Return current initiative queue state and engine status."""
    try:
        enabled = can("initiative")
        data: dict = {"enabled": enabled, "queue": [], "engine_available": False}

        if app_state.initiative_engine is not None:
            data["engine_available"] = True
            try:
                queue = app_state.initiative_engine.get_queue()
                data["queue"] = queue if isinstance(queue, list) else []
                data["queue_depth"] = len(data["queue"])
            except Exception:
                data["queue"] = []
                data["queue_depth"] = 0

            if hasattr(app_state.initiative_engine, "get_status"):
                try:
                    data.update(app_state.initiative_engine.get_status())
                except Exception:
                    pass

        return JSONResponse({
            "ok": True,
            "data": data,
            "current_focus": _get_current_focus_dict(),
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def post_chat(body: ChatRequest, request: Request):
    """Process chat turn synchronously."""
    if not app_state.topology:
        return JSONResponse(
            {"ok": False, "error": "Topology not ready — EOS is starting up"},
            status_code=503
        )
    if app_state.primary_degraded:
        return JSONResponse(
            {
                "ok": False,
                "error": "Primary server is currently unavailable. "
                         "Check the admin panel for server status.",
                "degraded": True,
            },
            status_code=503,
        )
    try:
        _notify_interaction()

        user_input = body.user_message
        if not user_input:
            return JSONResponse(
                {"ok": False, "error": "Empty message"},
                status_code=400
            )

        # Resolve text attachment — prepend file content to message if present
        if body.text_attachment:
            attach = body.text_attachment
            file_id = attach.get("file_id", "")
            filename = attach.get("filename", "file")
            upload_dir = Path(app_state.cfg.get("upload_dir", "data/uploads"))
            if not upload_dir.is_absolute():
                upload_dir = Path(__file__).parent.parent / upload_dir
            file_path = upload_dir / file_id
            if file_path.is_file():
                try:
                    file_content = file_path.read_text(encoding="utf-8", errors="replace")
                    user_input = f"[ATTACHMENT: {filename}]\n{file_content}\n\n{user_input}" if user_input else f"[ATTACHMENT: {filename}]\n{file_content}"
                except Exception as _exc:
                    logger.warning("Could not read attachment %s: %s", file_id, _exc)

        _ot, _ip = _get_request_origin(request)
        response = await process_turn(
            app_state.topology,
            user_input,
            app_state.cfg,
            tracer=app_state.tracer,
            bus=app_state.bus,
            origin_tier=_ot,
            origin_ip=_ip,
        )
        _emit_log("info", "chat", "Turn processed", {"user": user_input[:50]})

        # Notify reflection pipeline + initiative engine of completed turn
        _notify_turn_completed()

        from core.memory import count_interactions
        presence_snapshot = _build_entity_snapshot(
            scope="turn",
            source="api.chat.response",
            metadata={"endpoint": "/api/chat", "user_input_preview": user_input[:120]},
        )
        return JSONResponse({
            "ok": True,
            "response": response,
            "turn_count": count_interactions(),
            "current_focus": _get_current_focus_dict(),
            "presence": _build_presence_payload(entity_snapshot=presence_snapshot),
            "overnight": _get_overnight_status(),
        })
    except Exception as exc:
        logger.error("Chat endpoint error: %s", exc)
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=500
        )


async def websocket_chat(websocket: WebSocket):
    """WebSocket streaming chat endpoint."""
    if not app_state.topology:
        await websocket.close(code=1008, reason="Topology not ready — EOS is starting")
        return
    if app_state.primary_degraded:
        await websocket.close(code=1008, reason="Primary server unavailable")
        return

    await websocket.accept()
    # Resolve origin once for the whole WS session
    _ws_ip   = extract_client_ip(websocket)
    _ws_tier = classify_origin(_ws_ip)
    try:
        while True:
            data = await websocket.receive_json()
            message = (data.get("text") or data.get("message") or "").strip()

            if not message:
                await websocket.send_json({"ok": False, "error": "Empty message"})
                continue

            try:
                _notify_interaction()

                # Resolve text attachment — prepend file content to message if present
                attach = data.get("text_attachment")
                if attach and isinstance(attach, dict):
                    file_id = attach.get("file_id", "")
                    filename = attach.get("filename", "file")
                    upload_dir = Path(app_state.cfg.get("upload_dir", "data/uploads"))
                    if not upload_dir.is_absolute():
                        upload_dir = Path(__file__).parent.parent / upload_dir
                    file_path = upload_dir / file_id
                    if file_path.is_file():
                        try:
                            file_content = file_path.read_text(encoding="utf-8", errors="replace")
                            message = f"[ATTACHMENT: {filename}]\n{file_content}\n\n{message}"
                        except Exception as _exc:
                            logger.warning("Could not read attachment %s: %s", file_id, _exc)

                # Signal the client that processing has started
                await websocket.send_json({"type": "thinking"})

                response = await process_turn(
                    app_state.topology,
                    message,
                    app_state.cfg,
                    tracer=app_state.tracer,
                    bus=app_state.bus,
                    origin_tier=_ws_tier,
                    origin_ip=_ws_ip,
                )
                _notify_turn_completed()
                from core.memory import count_interactions
                presence_snapshot = _build_entity_snapshot(
                    scope="turn",
                    source="ws.chat.response",
                    metadata={"endpoint": "/ws", "user_input_preview": message[:120]},
                )
                await websocket.send_json({
                    "type": "response",
                    "response": response,
                    "turn_count": count_interactions(),
                    "current_focus": _get_current_focus_dict(),
                    "presence": _build_presence_payload(entity_snapshot=presence_snapshot),
                    "overnight": _get_overnight_status(),
                    "ok": True,
                })
            except Exception as exc:
                logger.error("WS chat error: %s", exc)
                await websocket.send_json({
                    "type": "error",
                    "message": str(exc),
                    "ok": False,
                })

    except WebSocketDisconnect:
        logger.debug("Client disconnected from chat WS")
    except Exception as exc:
        logger.error("WS chat exception: %s", exc)


async def post_tts(body: TtsRequest):
    """Text-to-speech via Piper."""
    try:
        text = body.text.strip()
        if not text:
            return JSONResponse(
                {"ok": False, "error": "Empty text"},
                status_code=400
            )

        from services.tts import synthesize_to_wav
        wav_bytes = synthesize_to_wav(text, app_state.cfg)
        if wav_bytes is None:
            return JSONResponse(
                {"ok": False, "error": "TTS unavailable — Piper binary not found or synthesis failed"},
                status_code=503
            )
        return StreamingResponse(
            BytesIO(wav_bytes),
            media_type="audio/wav",
            headers={"Content-Length": str(len(wav_bytes))},
        )
    except Exception as exc:
        logger.error("TTS endpoint error: %s", exc)
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=500
        )


async def post_upload(body: UploadRequest):
    """Accept file upload as JSON base64.

    Persists the file to data/uploads/<file_id> on disk so that subsequent
    chat turns can read it via the text_attachment mechanism.
    Returns flat fields (not nested under 'data') to match frontend expectations.
    """
    import base64 as _b64

    try:
        raw = _b64.b64decode(body.data)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"Invalid base64 data: {exc}"},
            status_code=400
        )

    file_id = str(uuid.uuid4())

    # Determine kind from content_type
    ct = (body.content_type or "").lower()
    if ct.startswith("image/"):
        kind = "image"
    elif ct.startswith("text/") or ct in ("application/csv", "application/x-csv"):
        kind = "text"
    elif ct == "application/pdf":
        kind = "document"
    else:
        # Infer from extension as fallback
        ext = Path(body.filename).suffix.lower()
        if ext in (".txt", ".csv", ".md", ".log"):
            kind = "text"
        elif ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            kind = "image"
        else:
            kind = "document"

    # Persist to uploads directory
    try:
        upload_dir = Path(app_state.cfg.get("upload_dir", "data/uploads"))
        if not upload_dir.is_absolute():
            upload_dir = Path(__file__).parent.parent / upload_dir
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / file_id).write_bytes(raw)
    except Exception as exc:
        logger.error("Upload storage error: %s", exc)
        return JSONResponse(
            {"ok": False, "error": f"Failed to store upload: {exc}"},
            status_code=500
        )

    return JSONResponse({
        "ok": True,
        "file_id":      file_id,
        "filename":     body.filename,
        "content_type": body.content_type,
        "kind":         kind,
        "size":         len(raw),
    })


async def get_vision_settings(session_id: str | None = None):
    """Get vision settings."""
    if not app_state.topology:
        return JSONResponse(
            {"ok": False, "error": "Topology not ready"},
            status_code=503
        )
    try:
        session_enabled = app_state.vision_sessions.get(session_id, app_state.topology.vision_enabled)
        return JSONResponse({
            "ok": True,
            "enabled":   session_enabled,
            "provider":  app_state.topology.vision_provider.value,
            "available": app_state.topology.vision_available,
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def post_vision_settings(body: VisionSettingsRequest, session_id: str | None = None):
    """Update vision toggle for session."""
    try:
        enabled = body.enabled
        if session_id:
            app_state.vision_sessions[session_id] = enabled
        return JSONResponse({"ok": True, "enabled": enabled})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Identity & Autonomy ────────────────────────────────────────────────────

async def get_identity():
    """Get identity app_state."""
    try:
        state = get_identity_state()
        return JSONResponse({"ok": True, "data": state})
    except Exception as exc:
        logger.error("Identity endpoint error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def get_autonomy():
    """Get full autonomy profile."""
    try:
        profile = get_full_profile()
        return JSONResponse({"ok": True, "data": profile})
    except Exception as exc:
        logger.error("Autonomy endpoint error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def post_autonomy(body: AutonomyRequest):
    """Update autonomy dimension."""
    try:
        set_dimension(body.dimension, body.enabled)
        profile = get_full_profile()
        return JSONResponse({"ok": True, "data": profile})
    except Exception as exc:
        logger.error("Autonomy update error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def post_identity_eval():
    """Dispatch identity evaluation as background task."""
    if not app_state.topology:
        return JSONResponse(
            {"ok": False, "error": "Topology not ready"},
            status_code=503
        )
    try:
        _track_background_task(
            "identity_evaluation",
            asyncio.create_task(
                run_evaluation_cycle(app_state.topology, app_state.cfg, tracer=app_state.tracer)
            ),
        )

        return JSONResponse({
            "ok": True,
            "data": {"queued": True}
        })
    except Exception as exc:
        logger.error("Identity eval error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def get_relational():
    """Get relational model."""
    try:
        model = get_relational_model()
        return JSONResponse({"ok": True, "data": model})
    except Exception as exc:
        logger.error("Relational endpoint error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Admin: Servers ─────────────────────────────────────────────────────────

async def admin_get_status():
    """Get comprehensive admin status."""
    if not app_state.topology:
        return JSONResponse(
            {"ok": False, "error": "Topology not ready"},
            status_code=503
        )
    try:
        entity_status = get_status(app_state.cfg)
        topology_status = app_state.topology.status_summary()
        tracer_summary = app_state.tracer.summary() if app_state.tracer else None

        turn_count = entity_status.get("interaction_count", 0)
        _bt = topology_status.get("boot_time")
        boot_elapsed = int(time.time() - _bt) if _bt else None
        return JSONResponse({
            "ok": True,
            # Flat fields expected by the admin status bar
            "tool_count":     len(app_state.tool_states),
            "disabled_count": sum(1 for v in app_state.tool_states.values() if not v),
            "denied_classes": [],
            "turn_count":     turn_count,
            "session_id":     app_state.session_id,
            "boot_time_s":    boot_elapsed,
            "data": {
                "entity": entity_status,
                "topology": topology_status,
                "capabilities": app_state.runtime_discovery.capabilities if app_state.runtime_discovery else {},
                "services": app_state.runtime_discovery.to_dict().get("services", {}) if app_state.runtime_discovery else {},
                "signal_bus": _signal_bus_health_summary(),
                "backup": _backup_health_summary(),
                "overnight": _get_overnight_status(include_history=True),
                "tracer": tracer_summary,
                "activation_policy": app_state.server_activation_manager.status() if app_state.server_activation_manager else None,
            }
        })
    except Exception as exc:
        logger.error("Admin status error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_servers_status():
    """Get server status list."""
    if not app_state.topology:
        return JSONResponse(
            {"ok": False, "error": "Topology not ready"},
            status_code=503
        )
    try:
        summary = app_state.topology.status_summary()
        probe_snap = app_state.backend_probe.status_snapshot() if app_state.backend_probe else {}
        servers = []
        srv_cfgs = app_state.cfg.get("servers", {})
        for role, srv in summary.get("servers", {}).items():
            probe = probe_snap.get(role, {})
            gpu_layers = srv_cfgs.get(role, {}).get("n_gpu_layers", -1)
            hardware = "GPU" if gpu_layers > 0 else ("CPU" if gpu_layers == 0 else None)
            servers.append({
                **srv,
                "latency_ms":   probe.get("last_latency_ms"),
                "last_checked": probe.get("last_check_at"),
                "hardware":     hardware,
                "error":        srv.get("error") or (
                    "health probe: offline" if probe.get("status") == "offline" else None
                ),
            })
        return JSONResponse({"ok": True, "data": servers})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_all_logs(limit: int = 200):
    """Return all recent log entries from the ring (main live-log feed)."""
    try:
        return JSONResponse({"ok": True, "data": list(app_state.log_ring)[-limit:]})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_server_logs(server_key: str, limit: int = 200):
    """Get logs for a specific server role, filtered from the ring."""
    try:
        logs = [
            entry for entry in list(app_state.log_ring)[-limit:]
            if entry.get("source") == server_key or server_key in entry.get("message", "")
        ]
        return JSONResponse({"ok": True, "data": logs})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Admin: Tools ───────────────────────────────────────────────────────────

async def admin_get_tools():
    """Get tool list with full governance metadata from registry."""
    try:
        if app_state.tool_registry:
            tools = [
                {
                    "name":                spec.name,
                    "description":         spec.description,
                    "pack":                spec.pack,
                    "tags":                spec.tags,
                    "risk_level":          spec.risk_level,
                    "trust_level":         spec.trust_level,
                    "confirmation_policy": spec.confirmation_policy,
                    "enabled":             spec.enabled,
                    "timeout_seconds":     spec.timeout_seconds,
                }
                for spec in app_state.tool_registry.all_tools()
            ]
        else:
            # Fallback: old-style state dict
            tools = [
                {"name": name, "enabled": app_state.tool_states.get(name, True)}
                for name in app_state.tool_states.keys()
            ]
        return JSONResponse({"ok": True, "data": tools})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_tools_audit(limit: int = 50):
    """Get recent tool action audit log (reversible + irreversible actions only)."""
    try:
        if not app_state.tool_registry:
            return JSONResponse({"ok": False, "error": "Registry not available"}, status_code=503)
        summary = app_state.tool_registry.audit_summary()
        # Trim entries to requested limit
        summary["entries"] = summary["entries"][-limit:]
        return JSONResponse({"ok": True, "data": summary})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_enable_tool(tool_name: str, request: Request):
    """Enable a tool."""
    try:
        if app_state.tool_registry:
            app_state.tool_registry.set_enabled(tool_name, True)
        app_state.tool_states[tool_name] = True
        _emit_log("info", "admin", f"Tool enabled: {tool_name}")
        _audit = get_audit_store()
        if _audit:
            _ot, _ip = _get_request_origin(request)
            _audit.record_admin_action("tool_toggle", target=tool_name, details={"enabled": True},
                                       origin_tier=_ot, client_ip=_ip)
        return JSONResponse({"ok": True, "data": {"enabled": True}})
    except KeyError:
        return JSONResponse({"ok": False, "error": f"Unknown tool: {tool_name}"}, status_code=404)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_disable_tool(tool_name: str, request: Request):
    """Disable a tool."""
    try:
        if app_state.tool_registry:
            app_state.tool_registry.set_enabled(tool_name, False)
        app_state.tool_states[tool_name] = False
        _emit_log("info", "admin", f"Tool disabled: {tool_name}")
        _audit = get_audit_store()
        if _audit:
            _ot, _ip = _get_request_origin(request)
            _audit.record_admin_action("tool_toggle", target=tool_name, details={"enabled": False},
                                       origin_tier=_ot, client_ip=_ip)
        return JSONResponse({"ok": True, "data": {"enabled": False}})
    except KeyError:
        return JSONResponse({"ok": False, "error": f"Unknown tool: {tool_name}"}, status_code=404)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Admin: Permissions ─────────────────────────────────────────────────────

async def admin_get_permissions():
    """Get permission allowlist."""
    try:
        perms = [
            {
                "class": cls,
                "allowed": cls in app_state.perm_allowlist,
            }
            for cls in sorted(app_state.perm_allowlist)
        ]
        return JSONResponse({"ok": True, "data": perms})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_allow_permission(perm_class: str, request: Request):
    """Add permission to allowlist."""
    try:
        app_state.perm_allowlist.add(perm_class)
        _emit_log("info", "admin", f"Permission allowed: {perm_class}")
        _audit = get_audit_store()
        if _audit:
            _ot, _ip = _get_request_origin(request)
            _audit.record_admin_action("permission_change", target=perm_class, details={"allowed": True},
                                       origin_tier=_ot, client_ip=_ip)
        return JSONResponse({"ok": True, "data": {"allowed": True}})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_deny_permission(perm_class: str, request: Request):
    """Remove permission from allowlist."""
    try:
        app_state.perm_allowlist.discard(perm_class)
        _emit_log("info", "admin", f"Permission denied: {perm_class}")
        _audit = get_audit_store()
        if _audit:
            _ot, _ip = _get_request_origin(request)
            _audit.record_admin_action("permission_change", target=perm_class, details={"allowed": False},
                                       origin_tier=_ot, client_ip=_ip)
        return JSONResponse({"ok": True, "data": {"allowed": False}})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_get_allowlist():
    """Get current allowlist."""
    try:
        return JSONResponse({
            "ok": True,
            "data": {"allowlist": sorted(app_state.perm_allowlist)}
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_allowlist_add(body: dict):
    """Add class to allowlist."""
    try:
        cls = body.get("class", "")
        if cls:
            app_state.perm_allowlist.add(cls)
        return JSONResponse({
            "ok": True,
            "data": {"allowlist": sorted(app_state.perm_allowlist)}
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_allowlist_remove(body: dict):
    """Remove class from allowlist."""
    try:
        cls = body.get("class", "")
        if cls:
            app_state.perm_allowlist.discard(cls)
        return JSONResponse({
            "ok": True,
            "data": {"allowlist": sorted(app_state.perm_allowlist)}
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Admin: Toolpacks ───────────────────────────────────────────────────────

async def admin_get_toolpacks():
    """Get toolpack list with tool governance metadata from registry."""
    try:
        if app_state.tool_registry:
            # Build pack map from live registry
            pack_map: dict[str, list] = {}
            for spec in app_state.tool_registry.all_tools():
                pack_map.setdefault(spec.pack, []).append({
                    "name":                spec.name,
                    "description":         spec.description,
                    "risk_level":          spec.risk_level,
                    "trust_level":         spec.trust_level,
                    "confirmation_policy": spec.confirmation_policy,
                    "enabled":             spec.enabled,
                })
            packs = [
                {
                    "name":        pack_name,
                    "tools":       tools,
                    "tool_count":  len(tools),
                    "enabled":     app_state.toolpack_states.get(pack_name, True),
                    "enabled_count": sum(1 for t in tools if t["enabled"]),
                }
                for pack_name, tools in pack_map.items()
            ]
        else:
            # Fallback: old-style
            toolpacks = _group_tools_by_category()
            packs = [
                {"name": name, "tools": tools, "tool_count": len(tools),
                 "enabled": app_state.toolpack_states.get(name, True)}
                for name, tools in toolpacks.items()
            ]
        return JSONResponse({"ok": True, "data": packs})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_enable_toolpack(pack_name: str, request: Request):
    """Enable a toolpack and all its tools."""
    try:
        app_state.toolpack_states[pack_name] = True
        if app_state.tool_registry:
            for spec in app_state.tool_registry.all_tools():
                if spec.pack == pack_name:
                    app_state.tool_registry.set_enabled(spec.name, True)
                    app_state.tool_states[spec.name] = True
        _emit_log("info", "admin", f"Toolpack enabled: {pack_name}")
        _audit = get_audit_store()
        if _audit:
            _ot, _ip = _get_request_origin(request)
            _audit.record_admin_action("toolpack_toggle", target=pack_name, details={"enabled": True},
                                       origin_tier=_ot, client_ip=_ip)
        return JSONResponse({"ok": True, "data": {"enabled": True}})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_disable_toolpack(pack_name: str, request: Request):
    """Disable a toolpack and all its tools."""
    try:
        app_state.toolpack_states[pack_name] = False
        if app_state.tool_registry:
            for spec in app_state.tool_registry.all_tools():
                if spec.pack == pack_name:
                    app_state.tool_registry.set_enabled(spec.name, False)
                    app_state.tool_states[spec.name] = False
        _emit_log("info", "admin", f"Toolpack disabled: {pack_name}")
        _audit = get_audit_store()
        if _audit:
            _ot, _ip = _get_request_origin(request)
            _audit.record_admin_action("toolpack_toggle", target=pack_name, details={"enabled": False},
                                       origin_tier=_ot, client_ip=_ip)
        return JSONResponse({"ok": True, "data": {"enabled": False}})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Admin: Computer Use ────────────────────────────────────────────────────

def _cu_not_available():
    return JSONResponse(
        {"ok": False, "error": "ComputerUseService not initialized. "
         "Set computer_use.enabled=true in config and restart."},
        status_code=503,
    )


def _serialize_cu_state(state: Any) -> dict[str, Any]:
    """Serialize computer-use snapshot into a JSON-friendly dict."""
    if hasattr(state, "to_dict"):
        return state.to_dict()
    if isinstance(state, dict):
        return state
    return {
        "mode": getattr(state, "mode", None),
        "mode_description": getattr(state, "mode_description", None),
        "active_app_id": getattr(state, "active_app_id", None),
        "active_shortcut_id": getattr(state, "active_shortcut_id", None),
        "active_window_title": getattr(state, "active_window_title", None),
        "pending_confirmation": getattr(state, "pending_confirmation", None),
        "approved_shortcuts": list(getattr(state, "approved_shortcuts", []) or []),
        "recent_decisions": list(getattr(state, "recent_decisions", []) or []),
        "total_decisions": getattr(state, "total_decisions", 0),
        "session_started_at": getattr(state, "session_started_at", None),
        "last_action_at": getattr(state, "last_action_at", None),
    }


async def admin_cu_state():
    """Get full computer-use state snapshot."""
    if app_state.computer_use_service is None:
        return _cu_not_available()
    try:
        state = app_state.computer_use_service.get_state()
        return JSONResponse({"ok": True, "data": _serialize_cu_state(state)})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_cu_set_mode(body: ComputerUseModeRequest, request: Request):
    """Set computer-use mode (off | command_only | supervised_session)."""
    if app_state.computer_use_service is None:
        return _cu_not_available()
    try:
        from runtime.computer_use_service import ComputerUseMode
        if body.mode not in ComputerUseMode.ALL_MODES:
            return JSONResponse(
                {"ok": False, "error": f"Invalid mode '{body.mode}'. Valid: {ComputerUseMode.ALL_MODES}"},
                status_code=400,
            )
        changed = app_state.computer_use_service.set_mode(body.mode, reason=body.reason)
        _emit_log(
            "info" if body.mode != "off" else "warn",
            "computer_use",
            f"Mode set to '{body.mode}' (reason={body.reason})",
            {"mode": body.mode, "changed": changed},
        )
        _audit = get_audit_store()
        if _audit:
            _ot, _ip = _get_request_origin(request)
            _audit.record_admin_action("computer_use_mode", target=body.mode,
                                       details={"reason": body.reason, "changed": changed},
                                       origin_tier=_ot, client_ip=_ip)
        state = app_state.computer_use_service.get_state()
        return JSONResponse({"ok": True, "data": _serialize_cu_state(state)})
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_cu_halt(request: Request, body: ComputerUseHaltRequest = None):
    """Emergency halt: immediately disable all computer use."""
    if app_state.computer_use_service is None:
        return _cu_not_available()
    try:
        reason = body.reason if body else "admin halt"
        result = app_state.computer_use_service.halt(reason=reason)
        _emit_log("warn", "computer_use", f"HALT issued (reason={reason})", result)
        _audit = get_audit_store()
        if _audit:
            _ot, _ip = _get_request_origin(request)
            _audit.record_admin_action("computer_use_halt", details={"reason": reason},
                                       origin_tier=_ot, client_ip=_ip)
        return JSONResponse({"ok": True, "data": result})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_cu_shortcuts():
    """List all currently-approved shortcuts."""
    if app_state.computer_use_service is None:
        return _cu_not_available()
    try:
        shortcuts = app_state.computer_use_service.get_shortcuts()
        return JSONResponse({"ok": True, "data": shortcuts})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_cu_policies():
    """List all per-application action policies."""
    if app_state.computer_use_service is None:
        return _cu_not_available()
    try:
        policies = app_state.computer_use_service.get_policies()
        return JSONResponse({"ok": True, "data": policies})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_cu_reload():
    """Reload shortcuts and policy from disk (hot-reload without restart)."""
    if app_state.computer_use_service is None:
        return _cu_not_available()
    try:
        result = app_state.computer_use_service.reload()
        _emit_log("info", "computer_use", "Policy reloaded", result)
        return JSONResponse({"ok": True, "data": result})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_cu_confirm(confirmation_id: str):
    """Confirm a pending computer-use action."""
    if app_state.computer_use_service is None:
        return _cu_not_available()
    try:
        ok = app_state.computer_use_service.confirm_pending(confirmation_id)
        if not ok:
            return JSONResponse(
                {"ok": False, "error": f"Confirmation ID '{confirmation_id}' not found or already resolved."},
                status_code=404,
            )
        _emit_log("info", "computer_use", f"Action confirmed: {confirmation_id}")
        return JSONResponse({"ok": True, "data": {"confirmed": True, "confirmation_id": confirmation_id}})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_cu_deny(confirmation_id: str):
    """Deny a pending computer-use action."""
    if app_state.computer_use_service is None:
        return _cu_not_available()
    try:
        ok = app_state.computer_use_service.deny_pending(confirmation_id)
        if not ok:
            return JSONResponse(
                {"ok": False, "error": f"Confirmation ID '{confirmation_id}' not found or already resolved."},
                status_code=404,
            )
        _emit_log("info", "computer_use", f"Action denied: {confirmation_id}")
        return JSONResponse({"ok": True, "data": {"denied": True, "confirmation_id": confirmation_id}})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Admin: Cognition Trace ─────────────────────────────────────────────────

async def admin_cognition_turns(limit: int = 50):
    """Get turn history from tracer."""
    if not app_state.tracer:
        return JSONResponse(
            {"ok": False, "error": "Tracer not available"},
            status_code=503
        )
    try:
        turns = app_state.tracer.list_turns(limit)
        return JSONResponse({"ok": True, "data": turns})
    except Exception as exc:
        logger.error("Cognition turns error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_cognition_turn_detail(turn_id: str):
    """Get detail for a single turn."""
    if not app_state.tracer:
        return JSONResponse(
            {"ok": False, "error": "Tracer not available"},
            status_code=503
        )
    try:
        turn = app_state.tracer.get_turn(turn_id)
        return JSONResponse({"ok": True, "data": turn})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_cognition_memory(limit: int = 50):
    """Get memory list from tracer."""
    if not app_state.tracer:
        return JSONResponse(
            {"ok": False, "error": "Tracer not available"},
            status_code=503
        )
    try:
        mem = app_state.tracer.list_memory(limit)
        return JSONResponse({"ok": True, "data": mem})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_cognition_reflection(limit: int = 50):
    """Get reflection list from tracer."""
    if not app_state.tracer:
        return JSONResponse(
            {"ok": False, "error": "Tracer not available"},
            status_code=503
        )
    try:
        refl = app_state.tracer.list_reflections(limit)
        return JSONResponse({"ok": True, "data": refl})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_cognition_state(limit: int = 50):
    """Get state deltas from tracer."""
    if not app_state.tracer:
        return JSONResponse(
            {"ok": False, "error": "Tracer not available"},
            status_code=503
        )
    try:
        state = app_state.tracer.list_state_deltas(limit)
        return JSONResponse({"ok": True, "data": state})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_cognition_summary():
    """Get cognition tracer summary."""
    if not app_state.tracer:
        return JSONResponse(
            {"ok": False, "error": "Tracer not available"},
            status_code=503
        )
    try:
        summary = app_state.tracer.summary()
        return JSONResponse({"ok": True, "data": summary})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Admin: Config & Diagnostics ────────────────────────────────────────────

async def admin_get_config():
    """Get sanitized config."""
    try:
        safe_cfg = _sanitize_config(app_state.cfg)
        return JSONResponse({"ok": True, "data": safe_cfg})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_subsystems():
    """Get subsystem health status."""
    health = {
        "memory_db": "ok" if app_state.cfg.get("db_path") else "unconfigured",
        "chroma": "ok" if app_state.cfg.get("retrieval", {}).get("chroma_path") else "unconfigured",
        "stt": (app_state.runtime_discovery.services.get("stt").status if app_state.runtime_discovery and app_state.runtime_discovery.services.get("stt") else "unconfigured"),
        "tts": (app_state.runtime_discovery.services.get("tts").status if app_state.runtime_discovery and app_state.runtime_discovery.services.get("tts") else "unconfigured"),
        "discord": "ok" if app_state.cfg.get("discord", {}).get("enabled") else "disabled",
        "google": "ok" if app_state.cfg.get("google", {}).get("enabled") else "disabled",
    }
    return JSONResponse({"ok": True, "data": health})


async def admin_export():
    """Export full diagnostic bundle — comprehensive JSON for LLM analysis."""
    try:
        import sys, platform as _platform
        topology_summary = app_state.topology.status_summary() if app_state.topology else {}
        probe_snap = app_state.backend_probe.status_snapshot() if app_state.backend_probe else {}
        _bt = topology_summary.get("boot_time")
        bundle = {
            "_meta": {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "eos_version": "1.0.0",
                "python_version": _platform.python_version(),
                "platform": sys.platform,
                "uptime_seconds": int(time.time() - _bt) if _bt else None,
            },
            "entity": {
                "identity": get_identity_state(),
                "relational": get_relational_model(),
                "autonomy": get_full_profile(),
                "name": get_status(app_state.cfg).get("name"),
                "interaction_count": get_status(app_state.cfg).get("interaction_count"),
            },
            "runtime": {
                "topology": topology_summary,
                "backend_health": probe_snap,
                "capabilities": app_state.runtime_discovery.capabilities if app_state.runtime_discovery else {},
                "signal_bus": _signal_bus_health_summary(),
                "deployment_mode": topology_summary.get("deployment_mode"),
                "activation_policy": app_state.server_activation_manager.status() if app_state.server_activation_manager else None,
            },
            "tools": {
                "total": len(app_state.tool_states),
                "enabled": sum(1 for v in app_state.tool_states.values() if v),
                "disabled": sum(1 for v in app_state.tool_states.values() if not v),
                "states": app_state.tool_states,
            },
            "recent_interactions": get_recent_interactions(50),
            "recent_logs": list(app_state.log_ring)[-100:],
            "config_sanitized": _sanitize_config(app_state.cfg),
        }
        return JSONResponse({"ok": True, "data": bundle})
    except Exception as exc:
        logger.error("Export error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_runtime_diagnostics():
    """Get runtime diagnostics."""
    if not app_state.topology:
        return JSONResponse(
            {"ok": False, "error": "Topology not ready"},
            status_code=503
        )
    try:
        import sys
        import platform
        latest_snapshot = app_state.entity_state_service.latest_snapshot() if app_state.entity_state_service is not None else None
        diagnostics = {
            "topology": app_state.topology.status_summary(),
            "boot_time": app_state.topology._boot_time,
            "uptime_seconds": time.time() - app_state.topology._boot_time,
            "python_version": platform.python_version(),
            "platform": sys.platform,
            "startup_guidance": app_state.startup_guidance,
            "startup_issues": list(app_state.startup_issues),
            "behavior_mode": getattr(latest_snapshot, "behavior_mode", None),
            "behavior": getattr(latest_snapshot, "behavior_summary", None),
            "signal_bus": _signal_bus_health_summary(),
            "backup": _backup_health_summary(),
            "activation_policy": app_state.server_activation_manager.status() if app_state.server_activation_manager else None,
        }
        return JSONResponse({"ok": True, "data": diagnostics})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_entity_state_diagnostic():
    """Get the latest shared entity-state snapshot with a short recent history."""
    try:
        if app_state.entity_state_service is None:
            return JSONResponse(
                {"ok": False, "error": "EntityStateService not initialized"},
                status_code=503,
            )
        latest = _build_entity_snapshot(
            scope="diagnostic",
            source="/admin/diagnostic/entity-state",
            metadata={"endpoint": "/admin/diagnostic/entity-state"},
        )
        return JSONResponse({
            "ok": True,
            "data": {
                "latest": latest.to_dict() if latest is not None else None,
                "history": app_state.entity_state_service.history(limit=5),
            },
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_environment_model_diagnostic():
    """Inspect the structured environment model the entity is currently using."""
    try:
        if app_state.entity_state_service is None:
            return JSONResponse(
                {"ok": False, "error": "EntityStateService not initialized"},
                status_code=503,
            )
        latest = _build_entity_snapshot(
            scope="diagnostic",
            source="/admin/diagnostic/environment-model",
            metadata={"endpoint": "/admin/diagnostic/environment-model"},
        )
        environment = latest.environment_summary if latest is not None else None
        return JSONResponse({
            "ok": True,
            "data": {
                "environment": environment,
                "prompt_block": getattr(latest, "environment_block", "") if latest is not None else "",
                "tool_context": getattr(latest, "environment_tool_context", "") if latest is not None else "",
            },
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_tool_registry():
    """Get tool registry diagnostics."""
    try:
        from tools.dispatcher import TOOL_SCHEMA
        diagnostics = {
            "total_tools": len(TOOL_SCHEMA),
            "enabled_count": sum(1 for v in app_state.tool_states.values() if v),
            "tools": [
                {"name": name, "enabled": app_state.tool_states.get(name, True)}
                for name in TOOL_SCHEMA.keys()
            ]
        }
        return JSONResponse({"ok": True, "data": diagnostics})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_shadow_databases():
    """Get database statistics."""
    try:
        db_stats = {}
        db_path = Path(app_state.cfg.get("db_path", "data/entity_app_state.db"))
        if db_path.is_file():
            db_stats["entity_app_state.db"] = db_path.stat().st_size

        chroma_path = Path(app_state.cfg.get("retrieval", {}).get("chroma_path", "data/memory_store"))
        if chroma_path.is_dir():
            total_size = sum(f.stat().st_size for f in chroma_path.rglob("*") if f.is_file())
            db_stats["chroma"] = total_size

        return JSONResponse({"ok": True, "data": db_stats})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_memory_health():
    """Get memory store health: SQLite + ChromaDB sizes, entry counts."""
    try:
        from runtime.memory_maintenance import health_check
        result = health_check(app_state.cfg)
        return JSONResponse({"ok": True, "data": result})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_memory_maintenance():
    """Manually trigger a memory maintenance run (prune + consolidate)."""
    if not app_state.topology:
        return JSONResponse(
            {"ok": False, "error": "Topology not ready"},
            status_code=503,
        )
    try:
        from runtime.memory_maintenance import run_maintenance
        _emit_log("info", "memory_maintenance", "Manual maintenance triggered")
        if app_state.current_focus_service is not None:
            app_state.current_focus_service.set_background_focus(
                focus_id="maintenance-admin",
                title="Run manual memory maintenance",
                why_now="An admin explicitly requested a maintenance cycle.",
                next_action="Execute maintenance immediately and capture the result.",
                source="maintenance",
            )
        result = await run_maintenance(
            app_state.topology, app_state.cfg, tracer=app_state.tracer, bus=app_state.bus
        )
        app_state.last_maintenance_result = result
        if app_state.current_focus_service is not None:
            app_state.current_focus_service.set_background_focus(
                focus_id="maintenance-admin",
                title="Manual memory maintenance finished",
                why_now="The requested maintenance cycle completed.",
                next_action="Review maintenance results or wait for the next task.",
                status="done",
                source="maintenance",
                metadata={"result": result},
            )
        return JSONResponse({"ok": True, "data": result, "current_focus": _get_current_focus_dict()})
    except Exception as exc:
        logger.error("Manual maintenance error: %s", exc)
        if app_state.current_focus_service is not None:
            app_state.current_focus_service.set_background_focus(
                focus_id="maintenance-admin",
                title="Manual memory maintenance failed",
                why_now="The admin-requested maintenance cycle raised an error.",
                next_action="Inspect the maintenance failure before retrying.",
                status="blocked",
                source="maintenance",
                metadata={"error": str(exc)},
            )
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_memory_maintenance_last():
    """Get results of the last maintenance run."""
    return JSONResponse({"ok": True, "data": app_state.last_maintenance_result or {"note": "No maintenance run yet"}})


async def admin_degradation_status():
    """Report current degradation state of the system."""
    try:
        servers_up = []
        servers_down = []
        if app_state.topology:
            for role, state in app_state.topology.servers.items():
                if state.is_absent():
                    continue
                probe = _resolve_service_probe(role)
                endpoint = _resolve_service_endpoint(role, state)
                service_data = {
                    "role": role,
                    "status": state.status.value,
                    "error": state.error,
                    "pid": state.pid,
                    "port": state.port,
                    "endpoint": endpoint,
                    "discovery_status": probe.status if probe is not None else None,
                }
                if state.is_ready():
                    servers_up.append(service_data)
                else:
                    servers_down.append(service_data)

        return JSONResponse({
            "ok": True,
            "data": {
                "primary_degraded": app_state.primary_degraded,
                "chat_available": not app_state.primary_degraded and app_state.topology is not None,
                "servers_up": servers_up,
                "servers_down": servers_down,
                "topology_ready": app_state.topology is not None,
            }
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_latency():
    """Latency tracking derived from CognitionTracer turn records."""
    try:
        if not app_state.tracer:
            return JSONResponse({
                "ok": True,
                "data": {"available": False, "note": "Tracer not enabled"}
            })

        turns = app_state.tracer.list_turns(limit=100)
        if not turns:
            return JSONResponse({
                "ok": True,
                "data": {"available": True, "turns": 0}
            })

        latencies = [t["latency_ms"] for t in turns if "latency_ms" in t]
        if not latencies:
            return JSONResponse({
                "ok": True,
                "data": {"available": True, "turns": len(turns), "note": "No latency data yet"}
            })

        latencies_sorted = sorted(latencies)
        n = len(latencies_sorted)
        avg = sum(latencies_sorted) / n
        p50 = latencies_sorted[n // 2]
        p95 = latencies_sorted[min(int(n * 0.95), n - 1)]
        p99 = latencies_sorted[min(int(n * 0.99), n - 1)]

        return JSONResponse({
            "ok": True,
            "data": {
                "available":     True,
                "sample_size":   n,
                "avg_ms":        round(avg, 1),
                "min_ms":        latencies_sorted[0],
                "max_ms":        latencies_sorted[-1],
                "p50_ms":        p50,
                "p95_ms":        p95,
                "p99_ms":        p99,
                "recent_turns":  [
                    {"latency_ms": t.get("latency_ms"), "timestamp": t.get("timestamp")}
                    for t in turns[-10:]
                ],
            }
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_storage():
    """Data directory file sizes."""
    try:
        data_dir = Path(app_state.cfg.get("db_path", "data/entity_app_state.db")).parent
        storage = {}
        if data_dir.is_dir():
            for item in data_dir.iterdir():
                if item.is_file():
                    storage[item.name] = item.stat().st_size
                elif item.is_dir():
                    size = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
                    storage[item.name] = size
        return JSONResponse({"ok": True, "data": storage})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Admin: Autonomy ────────────────────────────────────────────────────────

async def admin_autonomy_status():
    """Get full autonomy profile."""
    try:
        profile = get_full_profile()
        return JSONResponse({"ok": True, "data": profile})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_autonomy_status_update(body: AutonomyRequest, request: Request):
    """Update autonomy dimension."""
    try:
        set_dimension(body.dimension, body.enabled)
        _emit_log("info", "admin", f"Autonomy update: {body.dimension}={body.enabled}")
        _audit = get_audit_store()
        if _audit:
            _ot, _ip = _get_request_origin(request)
            _audit.record_admin_action("autonomy_change", target=body.dimension, details={"enabled": body.enabled},
                                       origin_tier=_ot, client_ip=_ip)
        profile = get_full_profile()
        return JSONResponse({"ok": True, "data": profile})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Admin: Free Mode (maps to initiative) ──────────────────────────────────

async def admin_free_mode():
    """Get free mode (initiative) status."""
    try:
        active = can("initiative")
        return JSONResponse({
            "ok": True,
            "data": {
                "active": active,
                "dimension": "initiative",
            }
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_free_mode_activate():
    """Activate free mode."""
    try:
        set_dimension("initiative", True)
        _emit_log("info", "admin", "Free mode activated")
        return JSONResponse({
            "ok": True,
            "data": {"active": True}
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_free_mode_deactivate():
    """Deactivate free mode."""
    try:
        set_dimension("initiative", False)
        _emit_log("info", "admin", "Free mode deactivated")
        return JSONResponse({
            "ok": True,
            "data": {"active": False}
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Admin: Capabilities (live runtime flags) ───────────────────────────────

async def admin_get_capabilities():
    """Return current state of all operator-controllable capability flags."""
    try:
        profile = get_full_profile()
        cu  = app_state.cfg.get("computer_use", {})
        ws  = app_state.cfg.get("workspace_tools", {})
        cr  = app_state.cfg.get("creativity", {})
        goo = app_state.cfg.get("google", {})
        return JSONResponse({
            "ok": True,
            "data": {
                "autonomy": profile,
                "computer_use": {
                    "enabled":      cu.get("enabled", False),
                    "default_mode": cu.get("default_mode", "off"),
                },
                "workspace": {
                    "allow_delete": ws.get("allow_delete", False),
                    "allow_exec":   ws.get("allow_exec",   False),
                },
                "creativity": {
                    "enabled":             cr.get("enabled", True),
                    "injection_frequency": cr.get("injection_frequency", "medium"),
                    "autonomous_idle":     cr.get("invocation_domains", {}).get("autonomous_idle", False),
                },
                "google": {
                    "enabled":          goo.get("enabled", False),
                    "calendar_enabled": goo.get("calendar_enabled", False),
                    "gmail_enabled":    goo.get("gmail_enabled", False),
                    "drive_enabled":    goo.get("drive_enabled", False),
                    "gmail_send_enabled": goo.get("gmail_send_enabled", False),
                    "drive_download_enabled": goo.get("drive_download_enabled", False),
                },
            },
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_set_capability(body: CapabilityRequest):
    """Update a capability flag in memory (runtime only — does not write config.json)."""
    try:
        group = body.group
        key   = body.key
        value = body.value

        if group == "autonomy":
            set_dimension(key, bool(value))
            _emit_log("info", "admin", f"Capability update: autonomy.{key}={value}")

        elif group == "computer_use":
            app_state.cfg.setdefault("computer_use", {})[key] = value
            _emit_log("info", "admin", f"Capability update: computer_use.{key}={value}")

        elif group == "workspace":
            app_state.cfg.setdefault("workspace_tools", {})[key] = value
            _emit_log("info", "admin", f"Capability update: workspace_tools.{key}={value}")

        elif group == "creativity":
            if key == "autonomous_idle":
                app_state.cfg.setdefault("creativity", {}).setdefault("invocation_domains", {})["autonomous_idle"] = value
            else:
                app_state.cfg.setdefault("creativity", {})[key] = value
            _emit_log("info", "admin", f"Capability update: creativity.{key}={value}")

        elif group == "google":
            app_state.cfg.setdefault("google", {})[key] = value
            _emit_log("info", "admin", f"Capability update: google.{key}={value}")

        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Admin: Initiative Engine ────────────────────────────────────────────────

async def admin_initiative_status():
    """Get initiative engine status + queue."""
    try:
        from core.autonomy import can
        if app_state.initiative_engine is None:
            return JSONResponse({"ok": True, "data": {"available": False}, "current_focus": _get_current_focus_dict()})
        status = app_state.initiative_engine.get_status()
        status["autonomy_gate"] = can("initiative")
        return JSONResponse({"ok": True, "data": status, "current_focus": _get_current_focus_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_initiative_queue():
    """Get current initiative queue."""
    try:
        if app_state.initiative_engine is None:
            return JSONResponse({"ok": True, "data": [], "current_focus": _get_current_focus_dict()})
        return JSONResponse({"ok": True, "data": app_state.initiative_engine.get_queue(), "current_focus": _get_current_focus_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_initiative_trigger(body: InitiativeTriggerRequest):
    """Manually trigger an initiative evaluation cycle."""
    try:
        if app_state.initiative_engine is None:
            return JSONResponse(
                {"ok": False, "error": "InitiativeEngine not available"},
                status_code=503,
            )
        rationale = body.rationale
        result = app_state.initiative_engine.trigger_eval(rationale=rationale)
        _emit_log("info", "initiative", "Manual trigger", result)
        return JSONResponse({"ok": True, "data": result, "current_focus": _get_current_focus_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_initiative_feedback(body: InitiativeFeedbackRequest):
    """Apply accept / defer / dismiss to a queued initiative."""
    try:
        if app_state.initiative_engine is None:
            return JSONResponse(
                {"ok": False, "error": "InitiativeEngine not available"},
                status_code=503,
            )
        result = app_state.initiative_engine.apply_feedback(body.initiative_id, body.feedback)
        if result.get("ok"):
            _emit_log("info", "initiative", f"Feedback '{body.feedback}' → {body.initiative_id}")
        return JSONResponse({**result, "current_focus": _get_current_focus_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_initiative_execute():
    """Execute all ready queued initiatives immediately."""
    try:
        if app_state.initiative_engine is None or app_state.topology is None:
            return JSONResponse(
                {"ok": False, "error": "InitiativeEngine or topology not available"},
                status_code=503,
            )
        snapshot = _build_entity_snapshot(
            scope="background",
            source="initiative.admin_execute",
            metadata={"endpoint": "/admin/initiative/execute"},
        )
        dispatched = await app_state.initiative_engine.execute_queued(
            app_state.topology,
            app_state.cfg,
            tracer=app_state.tracer,
            bus=app_state.bus,
            entity_snapshot=snapshot,
            overnight_status=_get_overnight_status(),
        )
        _emit_log("info", "initiative", f"Admin executed {len(dispatched)} initiatives")
        return JSONResponse({"ok": True, "data": {"dispatched": len(dispatched)}, "current_focus": _get_current_focus_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_initiative_clear():
    """Flush all items from the initiative queue."""
    try:
        if app_state.initiative_engine is None:
            return JSONResponse({"ok": True, "data": {"cleared": 0}})
        before = len(app_state.initiative_engine.get_queue())
        app_state.initiative_engine.clear_queue()
        _emit_log("info", "initiative", f"Queue cleared ({before} items removed)")
        return JSONResponse({"ok": True, "data": {"cleared": before}, "current_focus": _get_current_focus_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Admin: Investigation Engine ─────────────────────────────────────────────

async def admin_investigation_list(status: str = "", limit: int = 20):
    """List investigations, optionally filtered by status."""
    try:
        if app_state.investigation_engine is None:
            return JSONResponse({"ok": True, "data": []})
        items = app_state.investigation_engine.list(
            status=status or None,
            limit=max(1, min(limit, 100)),
        )
        return JSONResponse({"ok": True, "data": items, "current_focus": _get_current_focus_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_investigation_create(body: InvestigationCreateRequest):
    """Create a new investigation."""
    try:
        if app_state.investigation_engine is None:
            return JSONResponse(
                {"ok": False, "error": "InvestigationEngine not available"},
                status_code=503,
            )
        inv = app_state.investigation_engine.create(
            title=body.title,
            description=body.description,
            category=body.category,
            priority=body.priority,
            created_by="admin",
        )
        _emit_log("info", "investigation", f"Created: {body.title}", {"id": inv["investigation_id"]})
        return JSONResponse({"ok": True, "data": inv, "current_focus": _get_current_focus_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_investigation_get(investigation_id: str):
    """Get investigation by ID with pass history."""
    try:
        if app_state.investigation_engine is None:
            return JSONResponse(
                {"ok": False, "error": "InvestigationEngine not available"},
                status_code=503,
            )
        inv = app_state.investigation_engine.get(investigation_id)
        if inv is None:
            return JSONResponse(
                {"ok": False, "error": "Not found"},
                status_code=404,
            )
        return JSONResponse({"ok": True, "data": inv, "current_focus": _get_current_focus_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_investigation_run_pass(investigation_id: str, body: InvestigationRunPassRequest):
    """Run an investigation pass."""
    try:
        if app_state.investigation_engine is None or app_state.topology is None:
            return JSONResponse(
                {"ok": False, "error": "InvestigationEngine or topology not available"},
                status_code=503,
            )
        task_type = body.task_type
        objective = body.objective
        _emit_log("info", "investigation", f"Running pass: {task_type}", {"id": investigation_id})
        snapshot = _build_entity_snapshot(
            scope="background",
            source="investigation.run_pass",
            metadata={
                "endpoint": "/admin/investigation/run-pass",
                "investigation_id": investigation_id,
                "task_type": task_type,
            },
        )
        result = await app_state.investigation_engine.run_pass(
            app_state.topology,
            investigation_id,
            task_type=task_type,
            trigger_type="admin",
            objective=objective,
            tracer=app_state.tracer,
            bus=app_state.bus,
            entity_snapshot=snapshot,
        )
        payload = result if "ok" in result else {"ok": True, "data": result}
        if isinstance(payload, dict):
            payload["current_focus"] = _get_current_focus_dict()
        return JSONResponse(payload)
    except Exception as exc:
        logger.error("Investigation run_pass error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_investigation_resolve(investigation_id: str, body: InvestigationResolveRequest):
    """Resolve an investigation with a summary."""
    try:
        if app_state.investigation_engine is None:
            return JSONResponse(
                {"ok": False, "error": "InvestigationEngine not available"},
                status_code=503,
            )
        inv = app_state.investigation_engine.resolve(investigation_id, body.resolution_summary)
        if inv is None:
            return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
        _emit_log("info", "investigation", f"Resolved: {investigation_id}")
        return JSONResponse({"ok": True, "data": inv, "current_focus": _get_current_focus_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_investigation_reopen(investigation_id: str):
    """Reopen a resolved/archived investigation."""
    try:
        if app_state.investigation_engine is None:
            return JSONResponse(
                {"ok": False, "error": "InvestigationEngine not available"},
                status_code=503,
            )
        inv = app_state.investigation_engine.reopen(investigation_id)
        if inv is None:
            return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
        _emit_log("info", "investigation", f"Reopened: {investigation_id}")
        return JSONResponse({"ok": True, "data": inv, "current_focus": _get_current_focus_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_investigation_delete(investigation_id: str):
    """Delete an investigation."""
    try:
        if app_state.investigation_engine is None:
            return JSONResponse(
                {"ok": False, "error": "InvestigationEngine not available"},
                status_code=503,
            )
        deleted = app_state.investigation_engine.delete(investigation_id)
        if not deleted:
            return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
        _emit_log("info", "investigation", f"Deleted: {investigation_id}")
        return JSONResponse({"ok": True, "data": {"deleted": investigation_id}, "current_focus": _get_current_focus_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_investigation_diagnostics():
    """Get investigation engine diagnostics."""
    try:
        if app_state.investigation_engine is None:
            return JSONResponse({"ok": True, "data": {"available": False}})
        return JSONResponse({"ok": True, "data": app_state.investigation_engine.get_diagnostics(), "current_focus": _get_current_focus_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Admin: Diagnostics (force calls) ───────────────────────────────────────

async def admin_force_tool(body: ForceToolRequest):
    """Force-run a tool directly."""
    try:
        from runtime.orchestrator import _tool_executor as _exec
        if _exec is None:
            return JSONResponse({"ok": False, "error": "Tool executor not initialized"}, status_code=503)
        result = _exec.execute(body.tool_name, body.params, caller_trust="OPERATOR_ONLY")
        payload = {
            "tool": body.tool_name,
            "success": result.success,
            "result": result.output,
            "error": result.error,
            "pending_confirmation_id": result.pending_confirmation_id,
            "audit_id": result.audit_id,
        }
        status_code = 200 if result.success or result.pending_confirmation_id else 400
        return JSONResponse({"ok": result.success, "data": payload}, status_code=status_code)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_force_retrieval(body: ForceRetrievalRequest):
    """Force memory search."""
    try:
        results = search_memory(body.query, top_k=body.n)
        return JSONResponse({
            "ok": True,
            "data": {"query": body.query, "results": results}
        })
    except Exception as exc:
        logger.error("Force retrieval error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_connectivity():
    """Check all server health endpoints."""
    if not app_state.topology:
        return JSONResponse(
            {"ok": False, "error": "Topology not ready"},
            status_code=503
        )
    try:
        results = {}
        async with httpx.AsyncClient(timeout=5) as client:
            for role, state in app_state.topology.servers.items():
                if state.is_absent():
                    continue
                endpoint = _resolve_service_endpoint(role, state)
                try:
                    resp = await client.get(f"{endpoint}/health")
                    results[role] = {
                        "reachable": resp.status_code == 200,
                        "status": state.status.value,
                        "endpoint": endpoint,
                        "port": state.port,
                    }
                except Exception as exc:
                    results[role] = {
                        "reachable": False,
                        "error": str(exc),
                        "status": state.status.value,
                        "endpoint": endpoint,
                        "port": state.port,
                    }

        return JSONResponse({"ok": True, "data": results})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Admin WebSocket ────────────────────────────────────────────────────────

async def websocket_admin(websocket: WebSocket):
    """Admin WebSocket: bi-directional log stream + command channel.

    Requires ?token=<admin_token> in the URL — the WS API does not
    support custom headers, so auth is via query parameter.

    On connect:
      - Validates token; closes with code 4401 if invalid
      - Sends a 'hello' message with server status
      - Replays the last 50 log entries so the admin panel gets context
    Ongoing:
      - New log entries are pushed via _emit_log → _broadcast_log_to_admins
      - Pings are echoed as pongs
      - 'clear_log' command clears the ring
    """
    # Token check before accepting — middleware can't gate WS at the right layer
    token = websocket.query_params.get("token", "")
    admin_token = get_admin_token()
    if not token or not admin_token or token != admin_token:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    app_state.admin_ws_clients.append(websocket)

    # Send hello + replay recent logs
    try:
        await websocket.send_json({
            "type": "hello",
            "data": {
                "server": "EOS WebUI",
                "topology_ready": app_state.topology is not None,
                "tracer_ready":   app_state.tracer is not None,
                "bus_ready":      app_state.bus is not None,
            },
        })
        # Replay last 50 entries from the ring so admin gets context on connect
        recent = list(app_state.log_ring)[-50:]
        for entry in recent:
            try:
                await websocket.send_json({"type": "log", "data": entry})
            except Exception:
                break
    except Exception as exc:
        logger.debug("Admin WS hello failed: %s", exc)

    try:
        while True:
            data = await websocket.receive_json()
            cmd = data.get("type", "")

            if cmd == "ping":
                await websocket.send_json({"type": "pong"})

            elif cmd == "clear_log":
                app_state.log_ring.clear()
                await websocket.send_json({"type": "log_cleared"})

            elif cmd == "get_status":
                await websocket.send_json({
                    "type": "status",
                    "data": {
                        "topology_ready": app_state.topology is not None,
                        "tracer_summary": app_state.tracer.summary() if app_state.tracer else None,
                    },
                })

    except WebSocketDisconnect:
        logger.debug("Admin client disconnected")
        if websocket in app_state.admin_ws_clients:
            app_state.admin_ws_clients.remove(websocket)
    except Exception as exc:
        logger.error("Admin WS error: %s", exc)
        if websocket in app_state.admin_ws_clients:
            app_state.admin_ws_clients.remove(websocket)


# ── Auth verify ────────────────────────────────────────────────────────────

async def auth_verify(request: Request):
    """Check whether the X-Admin-Token in the request is valid.

    Returns {"ok": true, "valid": true} on success or {"ok": true, "valid": false}.
    This endpoint is intentionally exempt from the AdminAuthMiddleware so the
    admin UI can probe before redirecting.
    """
    token = request.headers.get("X-Admin-Token", "")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    admin_token = get_admin_token()
    valid = bool(token and admin_token and token == admin_token)
    return JSONResponse({"ok": True, "valid": valid})


# ── Admin: Durable audit query ──────────────────────────────────────────────

async def admin_audit_actions(
    action_type: str = "",
    target: str = "",
    since: float = 0.0,
    until: float = 0.0,
    limit: int = 100,
):
    """Query durable admin action history."""
    try:
        _audit = get_audit_store()
        if not _audit:
            return JSONResponse({"ok": False, "error": "Audit store not available"}, status_code=503)
        rows = _audit.query_admin_actions(
            action_type=action_type or None,
            target=target or None,
            since=since or None,
            until=until or None,
            limit=min(limit, 500),
        )
        return JSONResponse({"ok": True, "data": rows})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_audit_tools(
    tool_name: str = "",
    pack: str = "",
    since: float = 0.0,
    until: float = 0.0,
    limit: int = 100,
):
    """Query durable tool execution history."""
    try:
        _audit = get_audit_store()
        if not _audit:
            return JSONResponse({"ok": False, "error": "Audit store not available"}, status_code=503)
        rows = _audit.query_tool_executions(
            tool_name=tool_name or None,
            pack=pack or None,
            since=since or None,
            until=until or None,
            limit=min(limit, 500),
        )
        return JSONResponse({"ok": True, "data": rows})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_audit_summary():
    """Get aggregate audit statistics."""
    try:
        _audit = get_audit_store()
        if not _audit:
            return JSONResponse({"ok": False, "error": "Audit store not available"}, status_code=503)
        return JSONResponse({"ok": True, "data": _audit.summary()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Admin: Secrets management ───────────────────────────────────────────────

async def admin_secrets_list():
    """List secret key names stored in the system keyring (no values returned)."""
    try:
        from core.secrets import secrets_manager
        return JSONResponse({"ok": True, "data": secrets_manager.status()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_secrets_set(key: str, body: SecretSetRequest, request: Request):
    """Store a secret in the system keyring.

    Body: {"value": "<secret value>"}
    The value is never logged or returned.
    """
    try:
        from core.secrets import secrets_manager
        ok = secrets_manager.set(key, body.value)
        if ok:
            _emit_log("info", "secrets", f"Secret stored: {key}")
            _audit = get_audit_store()
            if _audit:
                _ot, _ip = _get_request_origin(request)
                _audit.record_admin_action("secret_set", target=key, origin_tier=_ot, client_ip=_ip)
        return JSONResponse({"ok": ok, "data": {"key": key, "stored": ok}})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_secrets_delete(key: str, request: Request):
    """Delete a secret from the system keyring."""
    try:
        from core.secrets import secrets_manager
        ok = secrets_manager.delete(key)
        if ok:
            _emit_log("info", "secrets", f"Secret deleted: {key}")
            _audit = get_audit_store()
            if _audit:
                _ot, _ip = _get_request_origin(request)
                _audit.record_admin_action("secret_delete", target=key, origin_tier=_ot, client_ip=_ip)
        return JSONResponse({"ok": True, "data": {"key": key, "deleted": ok}})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Admin: Pending tool confirmations ───────────────────────────────────────

async def admin_tools_pending():
    """List tool calls waiting for HARD_CONFIRM approval."""
    try:
        from runtime.orchestrator import _tool_executor as _exec
        if _exec is None:
            return JSONResponse({"ok": True, "data": []})
        return JSONResponse({"ok": True, "data": _exec.list_pending()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_tool_confirm(confirmation_id: str, request: Request):
    """Approve a HARD_CONFIRM-gated tool call and execute it."""
    try:
        from runtime.orchestrator import _tool_executor as _exec
        if _exec is None:
            return JSONResponse({"ok": False, "error": "Tool executor not available"}, status_code=503)
        result = _exec.confirm_pending(confirmation_id)
        _emit_log("info", "admin", f"Tool confirmed: {confirmation_id}")
        _audit = get_audit_store()
        if _audit:
            _ot, _ip = _get_request_origin(request)
            _audit.record_admin_action("tool_confirm", target=confirmation_id,
                                       details={"success": result.success, "error": result.error},
                                       origin_tier=_ot, client_ip=_ip)
        return JSONResponse({
            "ok": True,
            "data": {
                "confirmed": True,
                "success": result.success,
                "output": result.output,
                "error": result.error,
            }
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_tool_deny(confirmation_id: str, request: Request):
    """Deny a HARD_CONFIRM-gated tool call."""
    try:
        from runtime.orchestrator import _tool_executor as _exec
        if _exec is None:
            return JSONResponse({"ok": False, "error": "Tool executor not available"}, status_code=503)
        denied = _exec.deny_pending(confirmation_id)
        if not denied:
            return JSONResponse(
                {"ok": False, "error": f"Confirmation ID '{confirmation_id}' not found"},
                status_code=404,
            )
        _emit_log("info", "admin", f"Tool denied: {confirmation_id}")
        _audit = get_audit_store()
        if _audit:
            _ot, _ip = _get_request_origin(request)
            _audit.record_admin_action("tool_deny", target=confirmation_id,
                                       origin_tier=_ot, client_ip=_ip)
        return JSONResponse({"ok": True, "data": {"denied": True, "confirmation_id": confirmation_id}})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Google Workspace ───────────────────────────────────────────────────────

def _google_base_url(request: Request) -> str:
    """Derive the server's base URL from the incoming request."""
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host   = request.headers.get("x-forwarded-host", request.url.netloc)
    return f"{scheme}://{host}"


def _google_cfg() -> dict[str, Any]:
    return app_state.cfg.get("google", {}) if isinstance(app_state.cfg, dict) else {}


def _google_service_enabled(service: str) -> bool:
    gcfg = _google_cfg()
    if not gcfg.get("enabled", False):
        return False
    flag_map = {
        "calendar": "calendar_enabled",
        "gmail": "gmail_enabled",
        "drive": "drive_enabled",
    }
    flag = flag_map.get(service)
    return bool(gcfg.get(flag, False)) if flag else False


def _google_service_disabled(service: str) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": f"Google {service} integration is disabled in config",
            "disabled": True,
        },
        status_code=503,
    )


def _google_calendar_event_dict(event: dict[str, Any]) -> dict[str, Any]:
    start = event.get("start", {})
    end = event.get("end", {})
    return {
        "id": event.get("id", ""),
        "summary": event.get("summary", "(no title)"),
        "description": event.get("description", ""),
        "location": event.get("location", ""),
        "status": event.get("status", ""),
        "start": start.get("dateTime", start.get("date", "")),
        "end": end.get("dateTime", end.get("date", "")),
        "html_link": event.get("htmlLink", ""),
    }


async def google_status():
    """Return Google Workspace status: whether authorized, account info, scopes."""
    try:
        from core.google_oauth import (
            configure as oauth_cfg,
            is_authorized,
            get_account_info,
            get_credentials,
            validate_client_secret_path,
            _token_path,
        )
        gcfg = _google_cfg()
        last_auth_error = None
        try:
            oauth_cfg(app_state.cfg)
            validate_client_secret_path()
            client_secret_exists = True
        except FileNotFoundError as exc:
            client_secret_exists = False
            last_auth_error = str(exc)
        authorized = is_authorized() if client_secret_exists else False
        account = get_account_info() if authorized else {}
        token_exists = _token_path().is_file()
        token_valid = get_credentials() is not None if client_secret_exists else False
        services_enabled = {
            "calendar": _google_service_enabled("calendar"),
            "gmail": _google_service_enabled("gmail"),
            "drive": _google_service_enabled("drive"),
        }
        if not gcfg.get("enabled", False):
            overall_status = "disabled"
        elif not client_secret_exists:
            overall_status = "unavailable"
        elif token_valid:
            overall_status = "connected"
        elif token_exists:
            overall_status = "needs_reauth"
        else:
            overall_status = "needs_auth"
        return JSONResponse({
            "ok": True,
            "data": {
                "enabled": gcfg.get("enabled", False),
                "authorized": authorized,
                "account": account,
                "integration_enabled": gcfg.get("enabled", False),
                "overall_status": overall_status,
                "client_secret_exists": client_secret_exists,
                "token_exists": token_exists,
                "token_valid": token_valid,
                "services_enabled": services_enabled,
                "calendar_create_enabled": bool(services_enabled["calendar"]),
                "drive_download_enabled": bool(services_enabled["drive"]),
                "last_auth_error": last_auth_error,
            }
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def google_authorize(request: Request):
    """Begin the Google OAuth web flow.

    Returns {"ok": True, "data": {"auth_url": "https://accounts.google.com/..."}}
    The Admin Panel frontend opens auth_url in a new tab when the operator
    clicks Connect Google Account. After the user grants access, Google
    redirects to /api/google_workspace/callback which finalises the flow automatically.
    """
    try:
        if not _google_cfg().get("enabled", False):
            return _google_service_disabled("workspace")
        from core.google_oauth import configure as oauth_cfg, build_authorize_url
        oauth_cfg(app_state.cfg)
        redirect_uri = _google_base_url(request) + "/api/google_workspace/callback"
        auth_url, state = build_authorize_url(redirect_uri=redirect_uri)
        return JSONResponse({
            "ok": True,
            "data": {"auth_url": auth_url, "state": state}
        })
    except FileNotFoundError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    except Exception as exc:
        logger.error("Google authorize error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def google_oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Handle the Google OAuth2 redirect callback.

    Google redirects here after the user grants (or denies) access.
    On success, stores the token and redirects the browser to /admin with
    a status message.  On failure, returns a plain JSON error.
    """
    from fastapi.responses import RedirectResponse

    if error:
        logger.warning("[google_oauth] User denied access or error: %s", error)
        return RedirectResponse(
            url=f"/admin?google_auth=error&reason={error}",
            status_code=302,
        )

    if not code or not state:
        return JSONResponse(
            {"ok": False, "error": "Missing code or state in callback"},
            status_code=400,
        )

    try:
        from core.google_oauth import configure as oauth_cfg, exchange_code
        oauth_cfg(app_state.cfg)
        redirect_uri = _google_base_url(request) + "/api/google_workspace/callback"
        result = exchange_code(code=code, state=state, redirect_uri=redirect_uri)

        if result.get("ok"):
            email = result.get("account", {}).get("email", "unknown")
            _emit_log("info", "google_oauth", f"Authorized: {email}")
            return RedirectResponse(
                url=f"/admin?google_auth=success&email={email}",
                status_code=302,
            )
        else:
            logger.error("[google_oauth] Callback exchange failed: %s", result.get("error"))
            return JSONResponse({"ok": False, "error": result.get("error")}, status_code=400)

    except Exception as exc:
        logger.error("Google OAuth callback error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def google_revoke():
    """Revoke Google OAuth access and delete the local token."""
    try:
        if not _google_cfg().get("enabled", False):
            return _google_service_disabled("workspace")
        from core.google_oauth import configure as oauth_cfg, revoke
        oauth_cfg(app_state.cfg)
        result = revoke()
        if result.get("ok"):
            _emit_log("info", "google_oauth", "Access revoked")
        return JSONResponse({"ok": True, "data": result})
    except FileNotFoundError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    except Exception as exc:
        logger.error("Google revoke error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def google_account():
    """Return the authenticated Google account's profile (email, name, picture)."""
    try:
        if not _google_cfg().get("enabled", False):
            return _google_service_disabled("workspace")
        from core.google_oauth import configure as oauth_cfg, is_authorized, get_account_info
        oauth_cfg(app_state.cfg)
        if not is_authorized():
            return JSONResponse(
                {"ok": False, "error": "Not authorized — open Admin Panel → Integrations and click Connect Google Account first"},
                status_code=401,
            )
        return JSONResponse({"ok": True, "data": {"account": get_account_info()}})
    except FileNotFoundError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def google_calendar_today():
    """Return today's Google Calendar events."""
    try:
        if not _google_service_enabled("calendar"):
            return _google_service_disabled("calendar")
        from datetime import datetime, timedelta, timezone
        from core.google_oauth import configure as oauth_cfg, build_service
        oauth_cfg(app_state.cfg)
        svc = build_service("calendar", "v3", scopes=["https://www.googleapis.com/auth/calendar.readonly"])
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=1)
        res = svc.events().list(
            calendarId="primary",
            timeMin=now.isoformat().replace("+00:00", "Z"),
            timeMax=end.isoformat().replace("+00:00", "Z"),
            maxResults=20,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = [_google_calendar_event_dict(event) for event in res.get("items", [])]
        return JSONResponse({"ok": True, "data": {"events": events}})
    except FileNotFoundError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    except PermissionError as exc:
        return JSONResponse({"ok": False, "error": str(exc), "needs_auth": True}, status_code=401)
    except Exception as exc:
        logger.warning("google_calendar_today error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def google_calendar_upcoming(days: int = 7):
    """Return upcoming Google Calendar events."""
    try:
        if not _google_service_enabled("calendar"):
            return _google_service_disabled("calendar")
        from datetime import datetime, timedelta, timezone
        from core.google_oauth import configure as oauth_cfg, build_service
        oauth_cfg(app_state.cfg)
        svc = build_service("calendar", "v3", scopes=["https://www.googleapis.com/auth/calendar.readonly"])
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=days)
        res = svc.events().list(
            calendarId="primary",
            timeMin=now.isoformat().replace("+00:00", "Z"),
            timeMax=end.isoformat().replace("+00:00", "Z"),
            maxResults=50,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = [_google_calendar_event_dict(event) for event in res.get("items", [])]
        return JSONResponse({"ok": True, "data": {"events": events}})
    except FileNotFoundError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    except PermissionError as exc:
        return JSONResponse({"ok": False, "error": str(exc), "needs_auth": True}, status_code=401)
    except Exception as exc:
        logger.warning("google_calendar_upcoming error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def google_gmail_inbox(max_results: int = 10, query: str = ""):
    """Return recent Gmail inbox messages (subject, from, date)."""
    try:
        if not _google_service_enabled("gmail"):
            return _google_service_disabled("gmail")
        from core.google_oauth import configure as oauth_cfg, build_service
        oauth_cfg(app_state.cfg)
        svc = build_service("gmail", "v1",
                            scopes=["https://www.googleapis.com/auth/gmail.readonly"])
        query = query.strip()
        list_kwargs: dict[str, Any] = {"userId": "me", "maxResults": max_results}
        if query:
            list_kwargs["q"] = query
        else:
            list_kwargs["labelIds"] = ["INBOX"]
        response = svc.users().messages().list(**list_kwargs).execute()
        messages = []
        for msg_ref in response.get("messages", []):
            msg = svc.users().messages().get(
                userId="me", id=msg_ref["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            messages.append({
                "id":      msg_ref["id"],
                "subject": headers.get("Subject", ""),
                "from":    headers.get("From", ""),
                "date":    headers.get("Date", ""),
                "snippet": msg.get("snippet", ""),
            })
        return JSONResponse({"ok": True, "data": {"messages": messages, "query": query}})
    except FileNotFoundError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    except PermissionError as exc:
        return JSONResponse({"ok": False, "error": str(exc), "needs_auth": True}, status_code=401)
    except Exception as exc:
        logger.warning("google_gmail_inbox error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def google_drive_recent(max_results: int = 10):
    """Return recently modified Google Drive files."""
    try:
        if not _google_service_enabled("drive"):
            return _google_service_disabled("drive")
        from core.google_oauth import configure as oauth_cfg, build_service
        oauth_cfg(app_state.cfg)
        svc = build_service("drive", "v3",
                            scopes=["https://www.googleapis.com/auth/drive.readonly"])
        response = svc.files().list(
            pageSize=max_results,
            orderBy="modifiedTime desc",
            fields="files(id,name,mimeType,modifiedTime,webViewLink)",
        ).execute()
        files = response.get("files", [])
        return JSONResponse({"ok": True, "data": {"files": files}})
    except FileNotFoundError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    except PermissionError as exc:
        return JSONResponse({"ok": False, "error": str(exc), "needs_auth": True}, status_code=401)
    except Exception as exc:
        logger.warning("google_drive_recent error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def google_drive_search(query: str = "", q: str = "", max_results: int = 10):
    """Search Google Drive files by name/content."""
    query = (query or q).strip()
    if not query:
        return JSONResponse({"ok": False, "error": "query parameter required"}, status_code=400)
    try:
        if not _google_service_enabled("drive"):
            return _google_service_disabled("drive")
        from core.google_oauth import configure as oauth_cfg, build_service
        oauth_cfg(app_state.cfg)
        svc = build_service("drive", "v3",
                            scopes=["https://www.googleapis.com/auth/drive.readonly"])
        response = svc.files().list(
            q=f"fullText contains '{query}'",
            pageSize=max_results,
            fields="files(id,name,mimeType,modifiedTime,webViewLink)",
        ).execute()
        files = response.get("files", [])
        return JSONResponse({"ok": True, "data": {"query": query, "files": files}})
    except FileNotFoundError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    except PermissionError as exc:
        return JSONResponse({"ok": False, "error": str(exc), "needs_auth": True}, status_code=401)
    except Exception as exc:
        logger.warning("google_drive_search error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Discord ────────────────────────────────────────────────────────────────

async def discord_status():
    """Get Discord bot status: connection, guilds, uptime, turn count."""
    try:
        try:
            from interfaces.discord_bot import get_bot_status
            status = get_bot_status()
        except ImportError:
            status = {
                "enabled":   app_state.cfg.get("discord", {}).get("enabled", False),
                "connected": False,
                "note":      "Discord module not importable",
            }
        return JSONResponse({"ok": True, "data": status})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def discord_connect():
    """Start the Discord bot if it isn't running and is configured."""
    try:
        disc_cfg = app_state.cfg.get("discord", {})
        if not disc_cfg.get("enabled", False):
            return JSONResponse(
                {"ok": False, "error": "Discord is disabled in config"},
                status_code=400,
            )
        if app_state.topology is None:
            return JSONResponse(
                {"ok": False, "error": "Topology not ready"},
                status_code=503,
            )

        from interfaces.discord_bot import get_bot_status, start as discord_start

        if get_bot_status().get("connected"):
            return JSONResponse({"ok": True, "data": {"message": "Already connected"}})

        task_discord = asyncio.create_task(
            discord_start(
                app_state.topology, app_state.cfg,
                tracer=app_state.tracer,
                bus=app_state.bus,
                turn_notifiers=[_build_discord_turn_notifier()],
            )
        )
        _track_background_task("discord_bot", task_discord)
        _emit_log("info", "discord", "Discord bot connect requested")
        return JSONResponse({"ok": True, "data": {"message": "Discord bot starting"}})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def discord_disconnect():
    """Gracefully disconnect the Discord bot."""
    try:
        from interfaces.discord_bot import stop as discord_stop
        await discord_stop()
        _emit_log("info", "discord", "Discord bot disconnected")
        return JSONResponse({"ok": True, "data": {"message": "Discord bot disconnected"}})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── System sensors / capability / crash recovery endpoints ─────────────────

async def admin_system_sensors():
    """Return the latest hardware sensor snapshot (CPU, RAM, GPU, disk, servers)."""
    try:
        if app_state.sensor_poller is None:
            return JSONResponse({"ok": False, "error": "SensorPoller not initialized"}, status_code=503)
        snap = app_state.sensor_poller.snapshot()
        if snap is None:
            # Trigger first poll if not yet available
            snap = app_state.sensor_poller.poll_once()
        return JSONResponse({"ok": True, "data": snap.to_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_capabilities():
    """Return the capability registry health summary."""
    try:
        if app_state.capability_registry is None:
            return JSONResponse({"ok": False, "error": "CapabilityRegistry not initialized"}, status_code=503)
        return JSONResponse({"ok": True, "data": app_state.capability_registry.health_summary()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_crash_recovery():
    """Return the crash recovery report for the current boot."""
    try:
        if app_state.crash_recovery is None:
            return JSONResponse({"ok": False, "error": "CrashRecoveryService not initialized"}, status_code=503)
        report = app_state.crash_recovery.get_recovery_report()
        if report is None:
            return JSONResponse({"ok": False, "error": "No recovery report yet"}, status_code=503)
        return JSONResponse({"ok": True, "data": report.to_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_backend_health():
    """Return the backend health probe snapshot for all model servers."""
    try:
        if app_state.backend_probe is None:
            return JSONResponse({"ok": False, "error": "BackendHealthProbe not initialized"}, status_code=503)
        return JSONResponse({"ok": True, "data": app_state.backend_probe.status_snapshot()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_idle_cognition_status():
    """Return the idle cognition engine status."""
    try:
        if app_state.idle_cognition is None:
            return JSONResponse({"ok": False, "error": "IdleCognitionEngine not initialized"}, status_code=503)
        return JSONResponse({"ok": True, "data": app_state.idle_cognition.status()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_overnight_status():
    """Return live overnight-cycle diagnostics for the admin panel."""
    try:
        return JSONResponse({"ok": True, "data": _get_overnight_status(include_history=True)})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_identity_continuity():
    """Return cross-session identity stability score, drift history, and revision audit."""
    try:
        if app_state.identity_continuity is None:
            return JSONResponse({"ok": False, "error": "IdentityContinuityMonitor not initialized"}, status_code=503)
        return JSONResponse({"ok": True, "data": {
            "stability": {
                "score": app_state.identity_continuity.stability_score(),
                "label": app_state.identity_continuity.stability_label(),
            },
            "drift_summary": app_state.identity_continuity.drift_summary(),
            "recent_revisions": app_state.identity_continuity.revision_history(limit=10),
            "snapshot_count": app_state.identity_continuity.snapshot_count(),
        }})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_identity_revisions(limit: int = 50):
    """Return the full revision history of the entity's identity domains."""
    try:
        if app_state.identity_continuity is None:
            return JSONResponse({"ok": False, "error": "IdentityContinuityMonitor not initialized"}, status_code=503)
        return JSONResponse({"ok": True, "data": app_state.identity_continuity.revision_history(limit=limit)})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_force_idle_cognition():
    """Force an idle cognition fire immediately (for testing/admin use)."""
    try:
        if app_state.idle_cognition is None or app_state.topology is None:
            return JSONResponse({"ok": False, "error": "IdleCognitionEngine or topology not ready"}, status_code=503)
        snapshot = _build_entity_snapshot(
            scope="background",
            source="idle_cognition.force_fire",
            metadata={"endpoint": "/admin/system/idle-cognition/force"},
        )
        if app_state.current_focus_service is not None:
            app_state.current_focus_service.set_background_focus(
                focus_id="idle-cognition-force",
                title="Force idle cognition",
                why_now="An admin explicitly requested an immediate idle-cognition run.",
                next_action="Run the idle-cognition cycle now.",
                source="maintenance",
            )
        result = await app_state.idle_cognition.force_fire(
            app_state.topology, app_state.tracer, app_state.bus, entity_snapshot=snapshot,
            overnight_status=_get_overnight_status(),
        )
        if app_state.current_focus_service is not None:
            app_state.current_focus_service.set_background_focus(
                focus_id="idle-cognition-force",
                title="Idle cognition force-run complete",
                why_now="The admin-triggered idle-cognition cycle finished.",
                next_action="Wait for the next scheduled idle-cognition window.",
                status="done",
                source="maintenance",
                metadata={"result": result},
            )
        return JSONResponse({"ok": True, "data": result or {"message": "fire returned no result"}, "current_focus": _get_current_focus_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_entity_lifecycle():
    """
    Return the entity's full operational lifecycle record.

    Includes: entity_id, boot count, boot reason, total runtime, first init timestamp,
    current version, unclean shutdown count, and in-progress session duration.
    This data is determined deterministically by the runtime — not inferred from memory.
    """
    try:
        if app_state.entity_lifecycle is None:
            return JSONResponse(
                {"ok": False, "error": "EntityLifecycleService not initialized"},
                status_code=503,
            )
        summary = app_state.entity_lifecycle.lifecycle_summary()
        return JSONResponse({
            "ok": True,
            "data": {
                "record": app_state.entity_lifecycle.to_dict(),
                "summary": summary.to_dict(),
                "compact": summary.compact(),
            },
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_session_continuity():
    """Return the prior session continuity record (excerpt from last session)."""
    try:
        if app_state.session_continuity is None:
            return JSONResponse(
                {"ok": False, "error": "SessionContinuityService not initialized"},
                status_code=503,
            )
        return JSONResponse({"ok": True, "data": app_state.session_continuity.to_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_goals_list():
    """Return all goals (active, paused, completed, abandoned)."""
    try:
        if app_state.goal_store is None:
            return JSONResponse(
                {"ok": False, "error": "GoalStore not initialized"},
                status_code=503,
            )
        all_g = app_state.goal_store.all_goals(limit=50)
        return JSONResponse({
            "ok": True,
            "data": {
                "active":  [g.to_dict() for g in app_state.goal_store.active_goals()],
                "all":     [g.to_dict() for g in all_g],
                "active_count": app_state.goal_store.active_count(),
            },
            "current_focus": _get_current_focus_dict(),
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_goals_create(body: GoalCreateRequest):
    """Create a new goal."""
    try:
        if app_state.goal_store is None:
            return JSONResponse(
                {"ok": False, "error": "GoalStore not initialized"},
                status_code=503,
            )
        goal_id = app_state.goal_store.add_goal(
            description = body.description,
            priority    = body.priority,
            context     = body.context,
            source      = body.source,
        )
        return JSONResponse({"ok": True, "goal_id": goal_id, "current_focus": _get_current_focus_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_goals_complete(goal_id: str, body: GoalNoteRequest = None):
    """Mark a goal as completed. Optional body: {note}"""
    try:
        if app_state.goal_store is None:
            return JSONResponse({"ok": False, "error": "GoalStore not initialized"}, status_code=503)
        found = app_state.goal_store.complete_goal(goal_id, note=body.note if body else "")
        if not found:
            return JSONResponse({"ok": False, "error": "Goal not found"}, status_code=404)
        return JSONResponse({"ok": True, "goal_id": goal_id, "status": "completed", "current_focus": _get_current_focus_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_goals_abandon(goal_id: str, body: GoalAbandonRequest = None):
    """Mark a goal as abandoned. Optional body: {reason}"""
    try:
        if app_state.goal_store is None:
            return JSONResponse({"ok": False, "error": "GoalStore not initialized"}, status_code=503)
        found = app_state.goal_store.abandon_goal(goal_id, reason=body.reason if body else "")
        if not found:
            return JSONResponse({"ok": False, "error": "Goal not found"}, status_code=404)
        return JSONResponse({"ok": True, "goal_id": goal_id, "status": "abandoned", "current_focus": _get_current_focus_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Admin: Workspace ───────────────────────────────────────────────────────

async def admin_workspace():
    """
    Return workspace status: root path, subdirectory file counts,
    and list of context documents (passive context library).
    """
    try:
        if app_state.workspace_service is None:
            return JSONResponse(
                {"ok": False, "error": "WorkspaceService not initialized"},
                status_code=503,
            )
        state = app_state.workspace_service.state()
        return JSONResponse({
            "ok": True,
            "data": {
                "workspace": app_state.workspace_service.to_dict(),
                "block_preview": app_state.workspace_service.workspace_block(),
            },
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_workspace_scan():
    """Force-refresh the context library scan."""
    try:
        if app_state.workspace_service is None:
            return JSONResponse(
                {"ok": False, "error": "WorkspaceService not initialized"},
                status_code=503,
            )
        docs = app_state.workspace_service.scan_context()
        return JSONResponse({
            "ok": True,
            "data": {"context_documents": [
                {"filename": d.filename, "size_bytes": d.size_bytes, "mtime": d.mtime}
                for d in docs
            ]},
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Admin: Worldview ──────────────────────────────────────────────────────

async def admin_worldview_status():
    """
    Return worldview subsystem status: profile existence, last extraction date,
    source document counts (total, processed, unprocessed), and block preview.
    """
    try:
        if app_state.worldview_service is None:
            return JSONResponse(
                {"ok": False, "error": "WorldviewService not initialized"},
                status_code=503,
            )
        return JSONResponse({
            "ok": True,
            "data": {
                "profile":       app_state.worldview_service.profile_summary(),
                "sources":       app_state.worldview_service.sources_summary(),
                "block_preview": app_state.worldview_service.worldview_block(),
            },
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_worldview_refresh():
    """
    Force-refresh the worldview service cache (re-reads profile.md and extraction_log.json).
    Use after manually editing the profile or extraction log on disk.
    """
    try:
        if app_state.worldview_service is None:
            return JSONResponse(
                {"ok": False, "error": "WorldviewService not initialized"},
                status_code=503,
            )
        app_state.worldview_service.refresh()
        return JSONResponse({
            "ok": True,
            "data": {
                "profile":       app_state.worldview_service.profile_summary(),
                "sources":       app_state.worldview_service.sources_summary(),
            },
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_worldview_extract():
    """
    Trigger worldview extraction from source documents via the runtime workflow.
    """
    try:
        if app_state.worldview_service is None:
            return JSONResponse(
                {"ok": False, "error": "WorldviewService not initialized"},
                status_code=503,
            )
        if app_state.topology is None:
            return JSONResponse(
                {"ok": False, "error": "Runtime topology not initialized"},
                status_code=503,
            )

        entity_snapshot = _build_entity_snapshot(
            scope="admin",
            source="admin.system.worldview.extract",
            metadata={"route": "/admin/system/worldview/extract"},
        )
        result = await execute_worldview_extraction(
            app_state.topology,
            "Admin API requested worldview extraction.",
            app_state.cfg,
            entity_snapshot=entity_snapshot,
            trigger_source="admin_api",
        )
        return JSONResponse({
            "ok": True,
            "data": {
                "result": result,
                "profile": app_state.worldview_service.profile_summary(),
                "sources": app_state.worldview_service.sources_summary(),
            },
        })
    except Exception as exc:
        logger.exception("Worldview extraction route failed")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Admin: Backup & Restore ────────────────────────────────────────────────

async def admin_backup_list():
    """List all state snapshots with manifest metadata."""
    try:
        if app_state.backup_service is None:
            return JSONResponse(
                {"ok": False, "error": "BackupService not initialized"},
                status_code=503,
            )
        backups = app_state.backup_service.list_backups()
        return JSONResponse({
            "ok": True,
            "data": {"backups": [b.to_dict() for b in backups]},
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_backup_create(request: Request):
    """
    Create a new state snapshot.

    Optional body: {label?: str, notes?: str}
    """
    try:
        if app_state.backup_service is None:
            return JSONResponse(
                {"ok": False, "error": "BackupService not initialized"},
                status_code=503,
            )
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        label = body.get("label", "")
        notes = body.get("notes", "")
        _emit_log("info", "backup", f"Manual backup requested", {"label": label})
        manifest = app_state.backup_service.create_backup(
            label=label, trigger="admin_manual", notes=notes
        )
        _emit_log("info", "backup", "Backup complete",
                  {"backup_id": manifest.backup_id, "size_bytes": manifest.total_size_bytes})
        return JSONResponse({"ok": True, "data": manifest.to_dict()})
    except Exception as exc:
        logger.error("Backup create error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_backup_get(backup_id: str):
    """Get manifest details for a single backup."""
    try:
        if app_state.backup_service is None:
            return JSONResponse(
                {"ok": False, "error": "BackupService not initialized"},
                status_code=503,
            )
        backups = app_state.backup_service.list_backups()
        match = next((b for b in backups if b.backup_id == backup_id), None)
        if match is None:
            return JSONResponse(
                {"ok": False, "error": f"Backup not found: {backup_id}"},
                status_code=404,
            )
        return JSONResponse({"ok": True, "data": match.to_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_backup_restore(backup_id: str):
    """
    Restore a specific backup snapshot.

    The current state is saved to a safety snapshot before overwriting.
    EOS must be restarted after restore for changes to take effect.
    """
    try:
        if app_state.backup_service is None:
            return JSONResponse(
                {"ok": False, "error": "BackupService not initialized"},
                status_code=503,
            )
        _emit_log("warn", "backup", f"Restore requested", {"backup_id": backup_id})
        result = app_state.backup_service.restore_backup(backup_id)
        _emit_log("warn", "backup", "Restore complete — restart required",
                  {"backup_id": backup_id, "pre_restore_id": result.get("pre_restore_id")})
        return JSONResponse({"ok": True, "data": result})
    except FileNotFoundError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    except Exception as exc:
        logger.error("Backup restore error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_integrity_check():
    """
    Run an integrity check across all EOS state components:
    SQLite, ChromaDB vector store, workspace, and JSON state files.
    """
    try:
        if app_state.backup_service is None:
            return JSONResponse(
                {"ok": False, "error": "BackupService not initialized"},
                status_code=503,
            )
        report = app_state.backup_service.integrity_check()
        return JSONResponse({"ok": True, "data": report.to_dict()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Access Tiers ──────────────────────────────────────────────────────────


async def admin_access_tiers_list():
    """Get current policy for all access tiers (localhost, lan, external)."""
    try:
        ctrl = get_access_controller()
        if ctrl is None:
            return JSONResponse({"ok": False, "error": "Access controller not initialised"}, status_code=503)
        return JSONResponse({"ok": True, "data": ctrl.status()})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_access_tier_update(tier: str, body: AccessTierUpdateRequest, request: Request):
    """Partially update a tier's policy.  Only fields present in the body are changed.

    tier: localhost | lan | external
    """
    try:
        ctrl = get_access_controller()
        if ctrl is None:
            return JSONResponse({"ok": False, "error": "Access controller not initialised"}, status_code=503)
        updates = body.model_dump(exclude_none=True)
        if not updates:
            return JSONResponse({"ok": False, "error": "No fields provided"}, status_code=400)
        updated = ctrl.policies.update(tier, updates)
        _emit_log("info", "access_ctrl", f"Tier '{tier}' policy updated", updates)
        _audit = get_audit_store()
        if _audit:
            _ot, _ip = _get_request_origin(request)
            _audit.record_admin_action("access_tier_update", target=tier, details=updates,
                                       origin_tier=_ot, client_ip=_ip)
        return JSONResponse({"ok": True, "data": {"tier": tier, "policy": updated.to_dict()}})
    except KeyError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_lan_generate_pairing_code(request: Request):
    """Generate a one-time pairing code for a new LAN device.

    The code expires in 5 minutes and can only be used once.
    Share it with the LAN device which then calls POST /api/auth/lan/pair.
    """
    try:
        ctrl = get_access_controller()
        if ctrl is None:
            return JSONResponse({"ok": False, "error": "Access controller not initialised"}, status_code=503)
        code = ctrl.pairing.generate()
        _emit_log("info", "access_ctrl", "LAN pairing code generated")
        _audit = get_audit_store()
        if _audit:
            _ot, _ip = _get_request_origin(request)
            _audit.record_admin_action("lan_pairing_code_generated", origin_tier=_ot, client_ip=_ip)
        return JSONResponse({"ok": True, "data": {"code": code, "expires_in_seconds": 300}})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_lan_sessions_list():
    """List all active LAN sessions (token values are never returned)."""
    try:
        ctrl = get_access_controller()
        if ctrl is None:
            return JSONResponse({"ok": False, "error": "Access controller not initialised"}, status_code=503)
        return JSONResponse({"ok": True, "data": {"sessions": ctrl.sessions.list_sessions()}})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_lan_session_revoke(token_prefix: str, request: Request):
    """Revoke a LAN session by its token prefix (first 8 characters shown in session list)."""
    try:
        ctrl = get_access_controller()
        if ctrl is None:
            return JSONResponse({"ok": False, "error": "Access controller not initialised"}, status_code=503)
        # Find session by prefix
        sessions = ctrl.sessions._sessions
        match = next((t for t in sessions if t.startswith(token_prefix)), None)
        if match is None:
            return JSONResponse({"ok": False, "error": "Session not found"}, status_code=404)
        ctrl.sessions.revoke(match)
        _emit_log("info", "access_ctrl", f"LAN session revoked: {token_prefix}…")
        _audit = get_audit_store()
        if _audit:
            _ot, _ip = _get_request_origin(request)
            _audit.record_admin_action("lan_session_revoked", target=token_prefix,
                                       origin_tier=_ot, client_ip=_ip)
        return JSONResponse({"ok": True, "data": {"revoked": True}})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── LAN Auth (public, non-admin) ───────────────────────────────────────────


async def api_lan_pair(body: LanPairRequest, request: Request):
    """Exchange a one-time pairing code (from admin panel) for a LAN session token.

    Body: {"code": "<pairing-code>", "label": "optional device name"}
    Returns: {"token": "<session-token>", "expires_at": <unix-timestamp>}

    The returned token should be included as  X-Lan-Token: <token>  on
    subsequent requests.  Tokens expire per the LAN tier's session_ttl_sec setting.
    """
    try:
        ctrl = get_access_controller()
        if ctrl is None:
            return JSONResponse({"ok": False, "error": "Access controller not initialised"}, status_code=503)

        client_ip = extract_client_ip(request)
        tier = classify_origin(client_ip)

        if not ctrl.pairing.consume(body.code):
            return JSONResponse(
                {"ok": False, "error": "Invalid or expired pairing code"},
                status_code=401,
            )

        policy = ctrl.policies.get(tier)
        session = ctrl.sessions.create(
            client_ip=client_ip,
            ttl_sec=policy.session_ttl_sec,
            label=body.label or "",
        )
        _emit_log("info", "access_ctrl", f"LAN session created for {client_ip}",
                  {"label": body.label, "tier": tier})
        return JSONResponse({
            "ok": True,
            "data": {
                "token":      session.token,
                "expires_at": session.expires_at,
                "tier":       tier,
            },
        })
    except Exception as exc:
        logger.error("LAN pair error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def api_lan_status(request: Request):
    """Return the caller's origin tier and session validity."""
    try:
        ctrl = get_access_controller()
        client_ip = extract_client_ip(request)
        tier = classify_origin(client_ip)
        lan_token = request.headers.get("X-Lan-Token", "").strip() or None
        session_valid = False
        if ctrl and lan_token:
            session_valid = ctrl.sessions.validate(lan_token) is not None
        policy = ctrl.policies.get(tier) if ctrl else None
        return JSONResponse({
            "ok": True,
            "data": {
                "origin_tier":   tier,
                "client_ip":     client_ip,
                "session_valid": session_valid,
                "policy": policy.to_dict() if policy else None,
            },
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── External Inference Admin Handlers ─────────────────────────────────────────
#
# All handlers enforce backend-side policy.  The UI is a convenience layer;
# the backend is the sole source of truth for budget, origin, and gating.
# ─────────────────────────────────────────────────────────────────────────────


def _get_ei_policy():
    """Return the active ExternalInferencePolicy, or None."""
    return getattr(app_state, "ei_policy", None)


async def admin_ei_status(request: Request):
    """Return the current external inference config and budget state."""
    try:
        policy = _get_ei_policy()
        if policy is None:
            return JSONResponse({"ok": True, "data": {"available": False,
                "reason": "External inference subsystem not initialised"}})

        cfg_safe    = policy.get_ei_config_safe()
        budget      = policy.get_budget_state()

        return JSONResponse({
            "ok": True,
            "data": {
                "available":     True,
                "config":        cfg_safe,
                "budget": {
                    "cycle_start":          budget.cycle_start,
                    "cycle_end":            budget.cycle_end,
                    "monthly_budget_usd":   budget.monthly_budget_usd,
                    "effective_budget_usd": budget.effective_budget_usd,
                    "spent_usd":            budget.spent_usd,
                    "remaining_usd":        budget.remaining_usd,
                    "request_count":        budget.request_count,
                    "denied_count":         budget.denied_count,
                    "daily_count_today":    budget.daily_count_today,
                    "daily_cap":            budget.daily_cap,
                    "per_request_cap_usd":  budget.per_request_cap_usd,
                    "warning_level":        budget.warning_level,
                    "thresholds":           budget.thresholds,
                },
            },
        })
    except Exception as exc:
        logger.error("admin_ei_status: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_ei_config_update(body: ExternalInferenceConfigUpdate, request: Request):
    """Partially update the external_inference config block."""
    from runtime.external_inference_policy import VALID_APPROVAL_MODES, VALID_ESCALATION_MODES
    try:
        policy = _get_ei_policy()
        if policy is None:
            return JSONResponse({"ok": False, "error": "External inference not initialised"}, status_code=503)

        updates: dict = body.model_dump(exclude_none=True)

        # Validate enum fields
        if "approval_mode" in updates and updates["approval_mode"] not in VALID_APPROVAL_MODES:
            return JSONResponse({"ok": False, "error": f"Invalid approval_mode. Must be one of: {sorted(VALID_APPROVAL_MODES)}"}, status_code=422)
        if "escalation_mode" in updates and updates["escalation_mode"] not in VALID_ESCALATION_MODES:
            return JSONResponse({"ok": False, "error": f"Invalid escalation_mode. Must be one of: {sorted(VALID_ESCALATION_MODES)}"}, status_code=422)

        # Remap model_id / timeout_sec / max_retries into the huggingface sub-dict
        hf_updates: dict = {}
        for hf_key in ("model_id", "timeout_sec", "max_retries"):
            if hf_key in updates:
                hf_updates[hf_key] = updates.pop(hf_key)
        if hf_updates:
            updates["huggingface"] = hf_updates

        # Resolve config path for persistence
        config_path = getattr(app_state, "config_path", None)
        persist_path = Path(config_path) if config_path else None

        policy.update_ei_config(updates, persist_path=persist_path)

        _emit_log("info", "ext_inference", "External inference config updated",
                  {k: v for k, v in updates.items() if "key" not in k.lower()})
        return JSONResponse({"ok": True, "data": policy.get_ei_config_safe()})
    except Exception as exc:
        logger.error("admin_ei_config_update: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_ei_set_api_key(body: ExternalInferenceApiKeyRequest, request: Request):
    """Store the HuggingFace API key in the secrets manager.

    The key value is NEVER returned in any response after this call.
    """
    from runtime.external_inference_policy import HF_SECRET_KEY
    from core.secrets import secrets_manager as sm
    try:
        api_key = body.api_key.strip()
        if not api_key:
            return JSONResponse({"ok": False, "error": "API key must not be empty"}, status_code=422)
        # Never log the key itself
        ok = sm.set(HF_SECRET_KEY, api_key)
        if not ok:
            return JSONResponse({"ok": False, "error": "Keyring not available — secret could not be stored"}, status_code=503)
        _emit_log("info", "ext_inference", "HuggingFace API key stored in secrets manager")
        return JSONResponse({"ok": True, "data": {"api_key_configured": True,
            "note": "Key stored in system keyring. It is not returned by any API."}})
    except Exception as exc:
        logger.error("admin_ei_set_api_key: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_ei_delete_api_key(request: Request):
    """Remove the HuggingFace API key from the secrets manager."""
    from runtime.external_inference_policy import HF_SECRET_KEY
    from core.secrets import secrets_manager as sm
    try:
        sm.delete(HF_SECRET_KEY)
        _emit_log("info", "ext_inference", "HuggingFace API key removed from secrets manager")
        return JSONResponse({"ok": True, "data": {"api_key_configured": False}})
    except Exception as exc:
        logger.error("admin_ei_delete_api_key: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_ei_test_connection(request: Request):
    """Test the configured HuggingFace API key and model reachability."""
    try:
        policy = _get_ei_policy()
        if policy is None:
            return JSONResponse({"ok": False, "error": "External inference not initialised"}, status_code=503)
        result = policy.test_connection()
        return JSONResponse({"ok": result.get("ok", False), "data": result})
    except Exception as exc:
        logger.error("admin_ei_test_connection: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_ei_usage_history(request: Request):
    """Return recent external inference usage records from the ledger."""
    from runtime.external_inference_ledger import get_ledger
    try:
        limit = int(request.query_params.get("limit", 50))
        limit = max(1, min(limit, 200))
        ledger = get_ledger()
        if ledger is None:
            return JSONResponse({"ok": True, "data": {"records": [], "total_rows": 0}})
        rows   = ledger.recent_history(limit=limit)
        total  = ledger.total_rows()
        return JSONResponse({"ok": True, "data": {"records": rows, "total_rows": total}})
    except Exception as exc:
        logger.error("admin_ei_usage_history: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_ei_budget_state(request: Request):
    """Return just the budget summary for the current billing cycle."""
    try:
        policy = _get_ei_policy()
        if policy is None:
            return JSONResponse({"ok": True, "data": None})
        budget = policy.get_budget_state()
        return JSONResponse({"ok": True, "data": {
            "cycle_start":          budget.cycle_start,
            "cycle_end":            budget.cycle_end,
            "monthly_budget_usd":   budget.monthly_budget_usd,
            "effective_budget_usd": budget.effective_budget_usd,
            "spent_usd":            budget.spent_usd,
            "remaining_usd":        budget.remaining_usd,
            "request_count":        budget.request_count,
            "denied_count":         budget.denied_count,
            "daily_count_today":    budget.daily_count_today,
            "daily_cap":            budget.daily_cap,
            "per_request_cap_usd":  budget.per_request_cap_usd,
            "warning_level":        budget.warning_level,
            "thresholds":           budget.thresholds,
        }})
    except Exception as exc:
        logger.error("admin_ei_budget_state: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── External inference pending approvals (ask_for_paid_calls flow) ──────────


async def admin_ei_pending_list(request: Request):
    """Return all pending external inference approval requests."""
    try:
        pending = app_state.ei_pending_approvals
        now = time.time()
        rows = [
            {
                "approval_id":    aid,
                "reason":         entry.get("reason", ""),
                "local_severity": entry.get("local_outcome_severity", ""),
                "estimated_cost": entry.get("estimated_cost", 0.0),
                "origin_tier":    entry.get("origin_tier", ""),
                "requested_at":   entry.get("requested_at", 0.0),
                "age_seconds":    round(now - entry.get("requested_at", now), 1),
            }
            for aid, entry in list(pending.items())
        ]
        return JSONResponse({"ok": True, "data": {"pending": rows, "count": len(rows)}})
    except Exception as exc:
        logger.error("admin_ei_pending_list: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_ei_pending_confirm(approval_id: str, request: Request):
    """
    Approve a pending external inference call and execute it.

    The EI call is executed exactly once.  The response is returned in this
    endpoint's body.  The pending entry is removed regardless of success.
    """
    try:
        entry = app_state.ei_pending_approvals.pop(approval_id, None)
        if entry is None:
            return JSONResponse(
                {"ok": False, "error": f"Approval ID '{approval_id}' not found or already resolved"},
                status_code=404,
            )
        policy = _get_ei_policy()
        if policy is None:
            return JSONResponse({"ok": False, "error": "External inference not initialised"}, status_code=503)

        from runtime.external_inference_policy import APPROVAL_ALWAYS
        # Execute with approval_mode forced to "always" for this one call
        saved_mode = policy._ei_cfg.get("approval_mode")
        policy._ei_cfg["approval_mode"] = APPROVAL_ALWAYS
        decision, result = policy.call_external(
            entry["messages"],
            origin_tier=entry.get("origin_tier", "localhost"),
            origin_ip=entry.get("origin_ip", "127.0.0.1"),
            reason=entry.get("reason", "admin_approved"),
            local_outcome_severity=entry.get("local_outcome_severity", "hard_fail"),
        )
        policy._ei_cfg["approval_mode"] = saved_mode

        _ot, _ip = _get_request_origin(request)
        _audit = get_audit_store()
        if _audit:
            _audit.record_admin_action(
                "ei_approval_confirmed", target=approval_id,
                details={"ok": result.ok if result else False},
                origin_tier=_ot, client_ip=_ip,
            )
        _emit_log("info", "ext_inference", f"Pending EI call approved and executed: {approval_id}")

        if result is None or not result.ok:
            err = (result.error if result else "Policy denied after approval")
            return JSONResponse({"ok": False, "error": err,
                                 "data": {"approval_id": approval_id, "executed": True, "succeeded": False}})
        return JSONResponse({
            "ok": True,
            "data": {
                "approval_id":  approval_id,
                "executed":     True,
                "succeeded":    True,
                "response":     result.content,
                "model_id":     result.model_id,
                "tokens_input": result.tokens_input,
                "tokens_output": result.tokens_output,
                "latency_ms":   result.latency_ms,
            },
        })
    except Exception as exc:
        logger.error("admin_ei_pending_confirm: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


async def admin_ei_pending_deny(approval_id: str, request: Request):
    """Reject and discard a pending external inference call without executing it."""
    try:
        entry = app_state.ei_pending_approvals.pop(approval_id, None)
        if entry is None:
            return JSONResponse(
                {"ok": False, "error": f"Approval ID '{approval_id}' not found or already resolved"},
                status_code=404,
            )
        _ot, _ip = _get_request_origin(request)
        _audit = get_audit_store()
        if _audit:
            _audit.record_admin_action(
                "ei_approval_denied", target=approval_id,
                origin_tier=_ot, client_ip=_ip,
            )
        _emit_log("info", "ext_inference", f"Pending EI call denied: {approval_id}")
        return JSONResponse({"ok": True, "data": {"approval_id": approval_id, "denied": True}})
    except Exception as exc:
        logger.error("admin_ei_pending_deny: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Entry point ────────────────────────────────────────────────────────────
