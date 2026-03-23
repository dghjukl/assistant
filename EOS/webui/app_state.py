from __future__ import annotations

import collections
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AppState:
    topology: Any = None
    cfg: dict[str, Any] = field(default_factory=dict)
    tracer: Any = None
    bus: Any = None
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    reflection_pipeline: Any = None
    initiative_engine: Any = None
    investigation_engine: Any = None
    sensor_poller: Any = None
    crash_recovery: Any = None
    capability_registry: Any = None
    backend_probe: Any = None
    idle_cognition: Any = None
    identity_continuity: Any = None
    entity_lifecycle: Any = None
    session_continuity: Any = None
    goal_store: Any = None
    current_focus_service: Any = None
    workspace_service: Any = None
    worldview_service: Any = None
    entity_state_service: Any = None
    backup_service: Any = None
    computer_use_service: Any = None
    overnight_cycle_service: Any = None
    runtime_discovery: Any = None
    last_interaction_monotonic: float = 0.0
    primary_degraded: bool = False
    last_maintenance_result: dict[str, Any] = field(default_factory=dict)
    log_ring: collections.deque = field(default_factory=lambda: collections.deque(maxlen=500))
    admin_ws_clients: list[Any] = field(default_factory=list)
    tool_states: dict[str, bool] = field(default_factory=dict)
    perm_allowlist: set[str] = field(default_factory=set)
    toolpack_states: dict[str, bool] = field(default_factory=dict)
    tool_registry: Any = None
    vision_sessions: dict[str, bool] = field(default_factory=dict)
    background_tasks: set[Any] = field(default_factory=set)
    bus_seen_signals: set[str] = field(default_factory=set)
    startup_issues: list[dict[str, Any]] = field(default_factory=list)
    startup_guidance: str | None = None


app_state = AppState()
state = app_state
