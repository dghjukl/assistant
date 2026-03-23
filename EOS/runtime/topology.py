"""
EOS — Runtime Topology
Created from config + runtime discovery or boot-time health checks.

The topology remains the runtime routing source of truth:
  deployment_mode    — static config intent
  primary_multimodal — static primary-model capability
  vision_provider    — live derived backend for image routing
  vision_enabled     — live derived boolean
  vision_available   — live availability check
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Literal


class ServerStatus(str, Enum):
    PENDING = "pending"
    STARTING = "starting"
    READY = "ready"
    ABSENT = "absent"
    ERROR = "error"


class VisionProvider(str, Enum):
    NONE = "none"
    PRIMARY = "primary"
    DEDICATED = "dedicated"


@dataclass
class ServerState:
    role: str
    port: int
    endpoint: str
    status: ServerStatus = ServerStatus.PENDING
    pid: int | None = None
    error: str | None = None
    started_at: float | None = None
    model_path: str | None = None
    required: bool = False
    residency: str = "resident"
    activation_mode: str = "persistent"
    last_used_at: float | None = None
    last_stopped_at: float | None = None
    cooldown_until: float | None = None
    intentional_absence: bool = False
    last_decision: dict | None = None

    def is_ready(self) -> bool:
        return self.status == ServerStatus.READY

    def is_absent(self) -> bool:
        return self.status == ServerStatus.ABSENT


class RuntimeTopology:
    def __init__(
        self,
        deployment_mode: Literal["standard", "vision"],
        primary_multimodal: bool,
        servers: dict[str, ServerState],
    ) -> None:
        self._deployment_mode = deployment_mode
        self._primary_multimodal = primary_multimodal
        self._servers = servers
        self._lock = threading.Lock()
        self._boot_time = time.time()

    @property
    def deployment_mode(self) -> Literal["standard", "vision"]:
        return self._deployment_mode

    @property
    def primary_multimodal(self) -> bool:
        return self._primary_multimodal

    @property
    def vision_provider(self) -> VisionProvider:
        return self._derive_vision_provider()

    @property
    def vision_enabled(self) -> bool:
        return self.vision_provider != VisionProvider.NONE

    @property
    def vision_available(self) -> bool:
        provider = self.vision_provider
        with self._lock:
            if provider == VisionProvider.PRIMARY:
                primary = self._servers.get("primary")
                return bool(primary and primary.is_ready())
            if provider == VisionProvider.DEDICATED:
                vision = self._servers.get("vision")
                return bool(vision and vision.is_ready())
        return False

    @property
    def servers(self) -> dict[str, ServerState]:
        return self._servers

    def server(self, role: str) -> ServerState | None:
        return self._servers.get(role)

    def primary_endpoint(self) -> str:
        return self._servers["primary"].endpoint

    def vision_endpoint(self) -> str | None:
        vision = self._servers.get("vision")
        return vision.endpoint if vision and vision.is_ready() else None

    def tool_endpoint(self) -> str | None:
        tool = self._servers.get("tool")
        return tool.endpoint if tool and tool.is_ready() else None

    def thinking_endpoint(self) -> str | None:
        thinking = self._servers.get("thinking")
        return thinking.endpoint if thinking and thinking.is_ready() else None

    def creativity_endpoint(self) -> str | None:
        creativity = self._servers.get("creativity")
        return creativity.endpoint if creativity and creativity.is_ready() else None

    def mark_ready(self, role: str, pid: int | None = None) -> None:
        with self._lock:
            if role in self._servers:
                self._servers[role].status = ServerStatus.READY
                self._servers[role].pid = pid
                self._servers[role].error = None
                self._servers[role].started_at = time.time()
                self._servers[role].intentional_absence = False
                self._servers[role].cooldown_until = None

    def mark_error(self, role: str, error: str) -> None:
        with self._lock:
            if role in self._servers:
                self._servers[role].status = ServerStatus.ERROR
                self._servers[role].error = error

    def mark_absent(self, role: str, *, intentional: bool = False) -> None:
        with self._lock:
            if role in self._servers:
                self._servers[role].status = ServerStatus.ABSENT
                self._servers[role].error = None
                self._servers[role].pid = None
                self._servers[role].last_stopped_at = time.time()
                self._servers[role].intentional_absence = intentional

    def mark_starting(self, role: str, pid: int) -> None:
        with self._lock:
            if role in self._servers:
                self._servers[role].status = ServerStatus.STARTING
                self._servers[role].pid = pid
                self._servers[role].intentional_absence = False

    def mark_used(self, role: str, when: float | None = None) -> None:
        with self._lock:
            if role in self._servers:
                self._servers[role].last_used_at = when or time.time()

    def mark_cooldown(self, role: str, until: float | None) -> None:
        with self._lock:
            if role in self._servers:
                self._servers[role].cooldown_until = until

    def mark_decision(self, role: str, decision: dict | None) -> None:
        with self._lock:
            if role in self._servers:
                self._servers[role].last_decision = decision

    def route_image_input(self) -> dict:
        provider = self.vision_provider
        if provider == VisionProvider.DEDICATED:
            endpoint = self.vision_endpoint()
            if endpoint:
                return {"route": "dedicated", "endpoint": endpoint}
            return {"route": "error", "reason": "vision server unavailable"}

        if provider == VisionProvider.PRIMARY:
            return {"route": "primary", "endpoint": self.primary_endpoint()}

        return {"route": "reject", "reason": "vision unavailable"}

    def status_summary(self) -> dict:
        with self._lock:
            servers_out = {
                role: {
                    "role": s.role,
                    "port": s.port,
                    "status": s.status.value,
                    "pid": s.pid,
                    "required": s.required,
                    "model_path": s.model_path,
                    "error": s.error,
                    "started_at": s.started_at,
                    "residency": s.residency,
                    "activation_mode": s.activation_mode,
                    "last_used_at": s.last_used_at,
                    "last_stopped_at": s.last_stopped_at,
                    "cooldown_until": s.cooldown_until,
                    "intentional_absence": s.intentional_absence,
                    "last_decision": s.last_decision,
                }
                for role, s in self._servers.items()
            }
        return {
            "deployment_mode": self._deployment_mode,
            "primary_multimodal": self._primary_multimodal,
            "vision_provider": self.vision_provider.value,
            "vision_enabled": self.vision_enabled,
            "vision_available": self.vision_available,
            "boot_time": self._boot_time,
            "servers": servers_out,
        }

    def _derive_vision_provider(self) -> VisionProvider:
        vision = self._servers.get("vision")
        if vision and vision.is_ready():
            return VisionProvider.DEDICATED
        primary = self._servers.get("primary")
        if self._primary_multimodal and primary and primary.is_ready():
            return VisionProvider.PRIMARY
        return VisionProvider.NONE

    def __repr__(self) -> str:
        return (
            f"RuntimeTopology(mode={self._deployment_mode}, "
            f"vision_provider={self.vision_provider.value}, "
            f"vision_available={self.vision_available})"
        )


def build_topology_from_config(cfg: dict) -> RuntimeTopology:
    from runtime.server_activation import normalize_activation_config

    cfg = normalize_activation_config(cfg)
    mode = cfg.get("deployment_mode", "standard")
    primary_mm = cfg.get("primary", {}).get("is_multimodal", False)

    servers: dict[str, ServerState] = {}
    for role, srv_cfg in cfg.get("servers", {}).items():
        port = int(srv_cfg.get("port", 0))
        host = srv_cfg.get("host", "127.0.0.1")
        enabled = srv_cfg.get("enabled", False)
        activation_mode = str(srv_cfg.get("activation_mode", "persistent"))
        if enabled and activation_mode == "persistent":
            status = ServerStatus.PENDING
        else:
            status = ServerStatus.ABSENT
        servers[role] = ServerState(
            role=role,
            port=port,
            endpoint=f"http://{host}:{port}",
            status=status,
            required=srv_cfg.get("required", False),
            model_path=srv_cfg.get("model_path"),
            residency=str(srv_cfg.get("residency", "resident")),
            activation_mode=activation_mode,
            intentional_absence=bool(enabled and activation_mode == "on_demand"),
        )

    return RuntimeTopology(
        deployment_mode=mode,
        primary_multimodal=primary_mm,
        servers=servers,
    )
