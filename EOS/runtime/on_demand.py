"""
EOS — On-Demand Server Manager
================================
Starts helper servers (tool, thinking, creativity) only when the primary
model needs them, and shuts them down after they have been idle for
``idle_ttl_seconds`` (default: 300 s / 5 min).

Primary and vision servers are always managed externally (via bat files /
service discovery). This manager owns only the three helper roles.

Usage
-----
Call ``init_on_demand_manager(cfg, root, topology)`` once at startup.
Anywhere a helper is needed, call:

    manager = get_on_demand_manager()
    endpoint = await manager.ensure("thinking")   # starts if needed
    if endpoint:
        # use endpoint

Idle cleanup runs as a background asyncio task started by ``start_idle_loop()``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from runtime.topology import RuntimeTopology

logger = logging.getLogger("eos.on_demand")

# Roles managed on-demand (primary and vision are always externally managed)
ON_DEMAND_ROLES: frozenset[str] = frozenset({"tool", "thinking", "creativity"})

_DEFAULT_IDLE_TTL = 300.0   # seconds


class OnDemandServerManager:
    """
    Lifecycle manager for optional helper servers.

    Thread / coroutine safety
    -------------------------
    ``ensure()`` is async and holds a per-role asyncio.Lock so concurrent
    callers do not double-start the same server.

    ``is_available()`` is sync and non-blocking; it checks the topology state
    only — it does NOT trigger a start.
    """

    def __init__(
        self,
        cfg: dict,
        root: Path,
        topology: "RuntimeTopology",
    ) -> None:
        self._cfg      = cfg
        self._root     = root
        self._topology = topology
        self._idle_ttl = float(
            cfg.get("on_demand", {}).get("idle_ttl_seconds", _DEFAULT_IDLE_TTL)
        )
        self._procs:     dict[str, object] = {}   # role → subprocess.Popen
        self._last_used: dict[str, float]  = {}   # role → monotonic timestamp
        self._locks:     dict[str, asyncio.Lock] = {
            role: asyncio.Lock() for role in ON_DEMAND_ROLES
        }

    # ── Public API ─────────────────────────────────────────────────────────────

    async def ensure(self, role: str) -> str | None:
        """
        Ensure *role* server is running and healthy.

        Returns the endpoint URL (e.g. ``"http://127.0.0.1:8082"``) or None
        on failure. Never raises.
        """
        if role not in ON_DEMAND_ROLES:
            logger.warning("[OnDemand] ensure() called for non-managed role: %s", role)
            return None

        lock = self._locks[role]
        async with lock:
            srv = self._topology.server(role)

            # Already up — just refresh the idle clock and return
            if srv and srv.is_ready():
                self._last_used[role] = time.monotonic()
                return srv.endpoint

            # Need to start it
            endpoint = await self._start(role)
            if endpoint:
                self._last_used[role] = time.monotonic()
            return endpoint

    def is_available(self, role: str) -> bool:
        """Non-blocking check — True only if the server is currently READY."""
        srv = self._topology.server(role)
        return bool(srv and srv.is_ready())

    async def shutdown_all(self) -> None:
        """Stop all managed servers (called on EOS shutdown)."""
        for role in list(ON_DEMAND_ROLES):
            if self.is_available(role):
                await self._stop(role, reason="shutdown")

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _start(self, role: str) -> str | None:
        """Launch the server, wait for health, update topology. Returns endpoint or None."""
        from runtime.boot import _launch_server, _wait_for_health_with_retry

        srv_cfg = self._cfg.get("servers", {}).get(role)
        if not srv_cfg:
            logger.error("[OnDemand] No server config for role: %s", role)
            return None

        port     = int(srv_cfg.get("port", 0))
        host     = srv_cfg.get("host", "127.0.0.1")
        endpoint = f"http://{host}:{port}"
        timeout  = float(srv_cfg.get("health_timeout", 60.0))

        logger.info("[OnDemand] Starting %s on %s …", role, endpoint)
        try:
            proc = _launch_server(role, srv_cfg, self._root)
        except Exception as exc:
            logger.error("[OnDemand] Failed to launch %s: %s", role, exc)
            self._topology.mark_error(role, str(exc))
            return None

        self._procs[role] = proc
        self._topology.mark_starting(role, proc.pid)

        # Health-check is blocking (uses time.sleep) — run in thread executor
        loop = asyncio.get_event_loop()
        try:
            ready = await loop.run_in_executor(
                None,
                lambda: _wait_for_health_with_retry(
                    role, endpoint, timeout=timeout, poll_interval=1.0, proc=proc
                ),
            )
        except Exception as exc:
            logger.error("[OnDemand] Health check error for %s: %s", role, exc)
            ready = False

        if ready:
            self._topology.mark_ready(role, proc.pid)
            logger.info("[OnDemand] %s ready at %s", role, endpoint)
            return endpoint
        else:
            self._topology.mark_error(role, "health check failed")
            try:
                proc.terminate()
            except Exception:
                pass
            self._procs.pop(role, None)
            logger.warning("[OnDemand] %s failed to become healthy", role)
            return None

    async def _stop(self, role: str, *, reason: str = "idle") -> None:
        """Terminate the server process and reset topology state."""
        proc = self._procs.pop(role, None)
        if proc is not None:
            try:
                proc.terminate()
                logger.info("[OnDemand] %s stopped (%s)", role, reason)
            except Exception as exc:
                logger.warning("[OnDemand] Could not terminate %s: %s", role, exc)
        self._topology.mark_absent(role)
        self._last_used.pop(role, None)

    # ── Idle cleanup loop ──────────────────────────────────────────────────────

    async def _idle_loop(self, check_interval: float = 60.0) -> None:
        """Background task: shut down servers that have been idle > TTL."""
        while True:
            await asyncio.sleep(check_interval)
            now = time.monotonic()
            for role in list(ON_DEMAND_ROLES):
                if not self.is_available(role):
                    continue
                idle_for = now - self._last_used.get(role, now)
                if idle_for >= self._idle_ttl:
                    logger.info(
                        "[OnDemand] %s idle for %.0fs (TTL=%.0fs) — stopping",
                        role, idle_for, self._idle_ttl,
                    )
                    await self._stop(role, reason="idle timeout")

    def start_idle_loop(self) -> asyncio.Task:
        """Schedule the idle-cleanup loop as a background asyncio task."""
        task = asyncio.create_task(self._idle_loop())
        logger.info(
            "[OnDemand] Idle loop started (TTL=%.0fs, check every 60s)", self._idle_ttl
        )
        return task


# ── Module-level singleton ──────────────────────────────────────────────────

_manager: OnDemandServerManager | None = None


def init_on_demand_manager(
    cfg: dict,
    root: Path,
    topology: "RuntimeTopology",
) -> OnDemandServerManager:
    """Initialise the singleton. Call once at WebUI startup."""
    global _manager
    _manager = OnDemandServerManager(cfg, root, topology)
    logger.info("[OnDemand] Manager initialised (idle_ttl=%.0fs)", _manager._idle_ttl)
    return _manager


def get_on_demand_manager() -> OnDemandServerManager | None:
    """Return the singleton, or None if not yet initialised."""
    return _manager
