"""
EOS — Elastic Auxiliary Server Manager
======================================
Policy-driven lifecycle management for auxiliary llama-server backends.

The primary model remains the persistent executive. Auxiliary reasoning servers
(such as thinking and creativity) are treated as elastic capability pools:
  * resident baseline services start outside this manager
  * auxiliary services default to stopped
  * activation is decided centrally by policy + current operating mode +
    resource headroom
  * idle teardown and cooldown protections prevent thrashing

This module intentionally remains import-compatible with the older
``runtime.on_demand`` surface so existing callers can continue using
``init_on_demand_manager()`` and ``get_on_demand_manager()``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from runtime.server_activation import (
    ActivationDecision,
    ActivationRequest,
    OperatingMode,
    OperatingModeResolver,
    ResourceSnapshotProvider,
    ServerActivationPolicy,
    normalize_activation_config,
)

if TYPE_CHECKING:
    from runtime.topology import RuntimeTopology

logger = logging.getLogger("eos.on_demand")


class OnDemandServerManager:
    """Lifecycle manager for auxiliary helper servers."""

    def __init__(
        self,
        cfg: dict,
        root: Path,
        topology: "RuntimeTopology",
        *,
        sensor_provider: Callable[[], Any] | None = None,
        posture_provider: Callable[[], dict[str, Any] | None] | None = None,
        interaction_age_provider: Callable[[], float | None] | None = None,
    ) -> None:
        self._cfg = normalize_activation_config(cfg)
        self._root = root
        self._topology = topology
        self._sensor_provider = sensor_provider
        self._posture_provider = posture_provider
        self._interaction_age_provider = interaction_age_provider

        self._resource_provider = ResourceSnapshotProvider(sensor_provider=sensor_provider)
        self._mode_resolver = OperatingModeResolver(self._cfg, posture_provider=posture_provider)
        self._policy = ServerActivationPolicy(
            self._cfg,
            resource_provider=self._resource_provider,
            operating_mode_resolver=self._mode_resolver,
        )

        self._managed_roles = {
            role for role, role_policy in self._policy.config.role_policies.items()
            if role_policy.activation_mode == "on_demand"
        }
        self._procs: dict[str, object] = {}
        self._last_used: dict[str, float] = {}
        self._last_started: dict[str, float] = {}
        self._cooldown_until: dict[str, float] = {}
        self._last_decisions: dict[str, ActivationDecision] = {}
        self._decision_log: deque[dict[str, Any]] = deque(maxlen=100)
        self._locks = {role: asyncio.Lock() for role in self._managed_roles}
        self._idle_task: asyncio.Task | None = None

        for role in self._managed_roles:
            server = self._topology.server(role)
            if server is not None:
                server.residency = self._policy.residency_for(role)
                server.activation_mode = self._policy.activation_mode_for(role)
                if not server.is_ready():
                    self._topology.mark_absent(role, intentional=True)

    @property
    def managed_roles(self) -> set[str]:
        return set(self._managed_roles)

    @property
    def policy(self) -> ServerActivationPolicy:
        return self._policy

    def bind_runtime_providers(
        self,
        *,
        sensor_provider: Callable[[], Any] | None = None,
        posture_provider: Callable[[], dict[str, Any] | None] | None = None,
        interaction_age_provider: Callable[[], float | None] | None = None,
    ) -> None:
        if sensor_provider is not None:
            self._sensor_provider = sensor_provider
            self._resource_provider = ResourceSnapshotProvider(sensor_provider=sensor_provider)
        if posture_provider is not None:
            self._posture_provider = posture_provider
            self._mode_resolver = OperatingModeResolver(self._cfg, posture_provider=posture_provider)
        if interaction_age_provider is not None:
            self._interaction_age_provider = interaction_age_provider
        self._policy = ServerActivationPolicy(
            self._cfg,
            resource_provider=self._resource_provider,
            operating_mode_resolver=self._mode_resolver,
        )

    async def ensure(
        self,
        role: str,
        *,
        reason: str = "",
        task_type: str = "general",
        escalation: bool = False,
        operating_mode: OperatingMode | str | None = None,
        priority: float = 0.0,
        requested_by: str = "executive",
    ) -> str | None:
        """Ensure *role* is running and healthy, subject to centralized policy."""
        if role not in self._managed_roles:
            srv = self._topology.server(role)
            return srv.endpoint if srv and srv.is_ready() else None

        lock = self._locks[role]
        async with lock:
            srv = self._topology.server(role)
            if srv and srv.is_ready():
                self._touch(role)
                return srv.endpoint

            request = ActivationRequest(
                role=role,
                reason=reason,
                task_type=task_type,
                escalation=escalation,
                operating_mode=operating_mode,
                priority=priority,
                requested_by=requested_by,
            )
            last_interaction_age_s = self._interaction_age_provider() if self._interaction_age_provider else None
            cooldown_remaining = max(0.0, self._cooldown_until.get(role, 0.0) - time.monotonic())
            decision = self._policy.evaluate(
                request,
                last_interaction_age_s=last_interaction_age_s,
                cooldown_remaining_seconds=cooldown_remaining,
            )
            self._record_decision(decision)
            if not decision.allowed:
                logger.info("[Elastic] Activation denied for %s: %s", role, decision.reason)
                if srv is not None and hasattr(self._topology, "mark_decision"):
                    self._topology.mark_decision(role, decision.to_dict())
                return None

            endpoint = await self._start(role, decision)
            if endpoint:
                self._touch(role)
            return endpoint

    def is_available(self, role: str) -> bool:
        srv = self._topology.server(role)
        return bool(srv and srv.is_ready())

    def status(self) -> dict[str, Any]:
        return {
            "managed_roles": sorted(self._managed_roles),
            "policy": self._policy.config.to_dict(),
            "roles": {
                role: {
                    "running": self.is_available(role),
                    "last_used_monotonic": self._last_used.get(role),
                    "last_started_monotonic": self._last_started.get(role),
                    "cooldown_remaining_seconds": round(max(0.0, self._cooldown_until.get(role, 0.0) - time.monotonic()), 2),
                    "last_decision": self._last_decisions.get(role).to_dict() if self._last_decisions.get(role) else None,
                }
                for role in sorted(self._managed_roles)
            },
            "recent_decisions": list(self._decision_log),
        }

    async def shutdown_all(self) -> None:
        for role in list(self._managed_roles):
            if self.is_available(role):
                await self._stop(role, reason="shutdown", apply_cooldown=False)

    async def _start(self, role: str, decision: ActivationDecision) -> str | None:
        from runtime.server_runtime import is_port_bound, launch_server, wait_for_health_with_retry

        srv_cfg = self._cfg.get("servers", {}).get(role)
        if not srv_cfg:
            logger.error("[Elastic] No server config for role: %s", role)
            return None

        port = int(srv_cfg.get("port", 0))
        host = srv_cfg.get("host", "127.0.0.1")
        endpoint = f"http://{host}:{port}"
        timeout = float(srv_cfg.get("health_timeout", 60.0))

        if is_port_bound(host, port):
            logger.warning("[Elastic] Port already bound for %s on %s — not launching", role, endpoint)
            self._topology.mark_error(role, "port already bound")
            return None

        logger.info("[Elastic] Starting %s (%s, %s)", role, decision.mode, decision.reason)
        try:
            proc = launch_server(role, srv_cfg, self._root)
        except Exception as exc:
            logger.error("[Elastic] Failed to launch %s: %s", role, exc)
            self._topology.mark_error(role, str(exc))
            return None

        self._procs[role] = proc
        self._last_started[role] = time.monotonic()
        self._topology.mark_starting(role, proc.pid)
        if hasattr(self._topology, "mark_decision"):
            self._topology.mark_decision(role, decision.to_dict())

        loop = asyncio.get_event_loop()
        try:
            ready = await loop.run_in_executor(
                None,
                lambda: wait_for_health_with_retry(role, endpoint, timeout=timeout, poll_interval=1.0, proc=proc),
            )
        except Exception as exc:
            logger.error("[Elastic] Health check error for %s: %s", role, exc)
            ready = False

        if ready:
            self._topology.mark_ready(role, proc.pid)
            logger.info("[Elastic] %s ready at %s", role, endpoint)
            return endpoint

        self._topology.mark_error(role, "health check failed")
        try:
            proc.terminate()
        except Exception:
            pass
        self._procs.pop(role, None)
        logger.warning("[Elastic] %s failed to become healthy", role)
        return None

    async def _stop(self, role: str, *, reason: str = "idle", apply_cooldown: bool = True) -> None:
        proc = self._procs.pop(role, None)
        if proc is not None:
            try:
                proc.terminate()
                logger.info("[Elastic] %s stopped (%s)", role, reason)
            except Exception as exc:
                logger.warning("[Elastic] Could not terminate %s: %s", role, exc)
        self._topology.mark_absent(role, intentional=True)
        self._last_used.pop(role, None)
        if apply_cooldown:
            cooldown_seconds = self._policy.config.role_policies[role].cooldown_seconds
            self._cooldown_until[role] = time.monotonic() + cooldown_seconds
            if hasattr(self._topology, "mark_cooldown"):
                self._topology.mark_cooldown(role, self._cooldown_until[role])

    async def _idle_loop(self, check_interval: float = 30.0) -> None:
        while True:
            await asyncio.sleep(check_interval)
            now = time.monotonic()
            mode = self._mode_resolver.resolve(last_interaction_age_s=self._interaction_age_provider() if self._interaction_age_provider else None)
            for role in sorted(self._managed_roles):
                if not self.is_available(role):
                    continue
                role_policy = self._policy.config.role_policies[role]
                idle_timeout = role_policy.idle_timeout_seconds
                if mode == OperatingMode.ACTIVE_INTERACTION:
                    idle_timeout = min(idle_timeout, max(60.0, idle_timeout * 0.6))
                idle_for = now - self._last_used.get(role, now)
                uptime = now - self._last_started.get(role, now)
                if idle_for >= idle_timeout and uptime >= role_policy.min_uptime_seconds:
                    logger.info("[Elastic] %s idle for %.0fs (timeout=%.0fs) — stopping", role, idle_for, idle_timeout)
                    await self._stop(role, reason="idle timeout")

    def start_idle_loop(self) -> asyncio.Task:
        self._idle_task = asyncio.create_task(self._idle_loop())
        logger.info("[Elastic] Idle loop started for roles: %s", ", ".join(sorted(self._managed_roles)) or "none")
        return self._idle_task

    def _touch(self, role: str) -> None:
        now = time.monotonic()
        self._last_used[role] = now
        if hasattr(self._topology, "mark_used"):
            self._topology.mark_used(role, now)

    def _record_decision(self, decision: ActivationDecision) -> None:
        self._last_decisions[decision.role] = decision
        payload = decision.to_dict()
        payload["recorded_at"] = time.time()
        self._decision_log.append(payload)
        if hasattr(self._topology, "mark_decision"):
            self._topology.mark_decision(decision.role, payload)


_manager: OnDemandServerManager | None = None


def init_on_demand_manager(
    cfg: dict,
    root: Path,
    topology: "RuntimeTopology",
    *,
    sensor_provider: Callable[[], Any] | None = None,
    posture_provider: Callable[[], dict[str, Any] | None] | None = None,
    interaction_age_provider: Callable[[], float | None] | None = None,
) -> OnDemandServerManager:
    global _manager
    _manager = OnDemandServerManager(
        cfg,
        root,
        topology,
        sensor_provider=sensor_provider,
        posture_provider=posture_provider,
        interaction_age_provider=interaction_age_provider,
    )
    logger.info("[Elastic] Manager initialised for managed roles: %s", ", ".join(sorted(_manager.managed_roles)) or "none")
    return _manager


def get_on_demand_manager() -> OnDemandServerManager | None:
    return _manager
