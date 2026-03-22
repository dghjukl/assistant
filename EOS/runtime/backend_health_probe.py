"""
EOS — Backend Health Probe
Proactive model backend monitoring with hysteresis and latency tracking.

Improvements over the basic ``_server_health_loop`` in server.py
-----------------------------------------------------------------
* Hysteresis / debounce — a backend is not declared OFFLINE until
  ``failure_threshold`` consecutive failures.  A single transient timeout
  will not flip the status.  Recovery requires one success (aggressive recovery).
* Latency tracking — ``last_latency_ms`` and ``avg_latency_ms`` (EMA) per backend.
* Degraded state — backends responding slower than ``degraded_latency_ms`` are
  marked DEGRADED even while technically reachable.
* Signal bus integration — status transitions publish to the signal bus so the
  initiative engine can react.
* Rich status snapshot — ``status_snapshot()`` returns per-backend latency,
  failure counts, last check time, and status — not just a bool.

Usage
-----
    from runtime.backend_health_probe import BackendHealthProbe
    from runtime.topology import RuntimeTopology

    probe = BackendHealthProbe(
        topology=topology,
        signal_bus=bus,           # optional
        interval_seconds=60,
        failure_threshold=3,
        degraded_latency_ms=2000,
    )
    probe.start()     # daemon thread
    probe.stop()      # graceful shutdown

    snap = probe.status_snapshot()   # dict: role → health dict
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("eos.backend_health_probe")
UTC = timezone.utc

_EMA_ALPHA = 0.2   # exponential moving average weight for latency


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# ── Backend status enum ───────────────────────────────────────────────────────

class BackendStatus:
    HEALTHY   = "healthy"
    DEGRADED  = "degraded"   # responding but slow
    OFFLINE   = "offline"    # consecutive failures >= threshold
    UNKNOWN   = "unknown"    # never probed


# ── Per-backend state ─────────────────────────────────────────────────────────

@dataclass
class _BackendState:
    role: str
    endpoint: str
    status: str = BackendStatus.UNKNOWN
    failure_count: int = 0
    last_latency_ms: Optional[float] = None
    avg_latency_ms: Optional[float] = None
    last_check_at: Optional[str] = None
    last_success_at: Optional[str] = None
    transition_at: Optional[str] = None   # when status last changed

    def update_latency(self, ms: float) -> None:
        self.last_latency_ms = ms
        if self.avg_latency_ms is None:
            self.avg_latency_ms = ms
        else:
            self.avg_latency_ms = _EMA_ALPHA * ms + (1 - _EMA_ALPHA) * self.avg_latency_ms

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "endpoint": self.endpoint,
            "status": self.status,
            "failure_count": self.failure_count,
            "last_latency_ms": round(self.last_latency_ms, 1) if self.last_latency_ms is not None else None,
            "avg_latency_ms": round(self.avg_latency_ms, 1) if self.avg_latency_ms is not None else None,
            "last_check_at": self.last_check_at,
            "last_success_at": self.last_success_at,
            "transition_at": self.transition_at,
        }


# ── BackendHealthProbe ────────────────────────────────────────────────────────

class BackendHealthProbe:
    """
    Proactive health prober for all llama-server backends.

    Parameters
    ----------
    topology : RuntimeTopology
        Runtime topology providing server endpoints and state management.
    signal_bus : SignalBus, optional
        If provided, status transitions are published to the bus.
    interval_seconds : float
        How often to probe all backends (default 60).
    failure_threshold : int
        Consecutive failures before marking OFFLINE (default 3).
    degraded_latency_ms : float
        Response latency above this is DEGRADED even if reachable (default 2000).
    probe_timeout_s : float
        HTTP timeout per probe (default 5).
    """

    def __init__(
        self,
        topology: Any,
        signal_bus: Any = None,
        interval_seconds: float = 60.0,
        failure_threshold: int = 3,
        degraded_latency_ms: float = 2000.0,
        probe_timeout_s: float = 5.0,
    ) -> None:
        self._topology = topology
        self._bus = signal_bus
        self._interval = interval_seconds
        self._failure_threshold = failure_threshold
        self._degraded_latency_ms = degraded_latency_ms
        self._probe_timeout = probe_timeout_s

        self._lock = threading.Lock()
        self._states: dict[str, _BackendState] = {}
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Seed state from topology on construction
        self._seed_from_topology()

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background probing thread (daemon)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="eos.backend_probe", daemon=True
        )
        self._thread.start()
        logger.info(
            "BackendHealthProbe started (interval=%.0fs, fail_threshold=%d)",
            self._interval, self._failure_threshold,
        )

    def stop(self) -> None:
        """Gracefully stop the probing thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def probe_all_once(self) -> dict[str, dict]:
        """Probe all backends immediately (blocking). Returns status dict."""
        self._seed_from_topology()
        with self._lock:
            roles = list(self._states.keys())
        results = {}
        for role in roles:
            results[role] = self._probe_one(role)
        return results

    def status_snapshot(self) -> dict[str, dict]:
        """Return current state of all known backends (non-blocking)."""
        with self._lock:
            return {role: state.to_dict() for role, state in self._states.items()}

    def get_status(self, role: str) -> str:
        """Return the current BackendStatus string for a role."""
        with self._lock:
            state = self._states.get(role)
            return state.status if state else BackendStatus.UNKNOWN

    # ── Internal ──────────────────────────────────────────────────────────────

    def _seed_from_topology(self) -> None:
        """Initialise state entries from the topology (idempotent)."""
        if not self._topology:
            return
        with self._lock:
            for role, server_state in self._topology.servers.items():
                if role not in self._states and not server_state.is_absent():
                    self._states[role] = _BackendState(
                        role=role,
                        endpoint=server_state.endpoint,
                    )

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._seed_from_topology()
                with self._lock:
                    roles = list(self._states.keys())
                for role in roles:
                    if self._stop_event.is_set():
                        break
                    self._probe_one(role)
            except Exception as exc:
                logger.debug("BackendHealthProbe loop error: %s", exc)
            self._stop_event.wait(timeout=self._interval)

    def _probe_one(self, role: str) -> dict:
        """Probe a single backend and update its state. Returns state dict."""
        with self._lock:
            state = self._states.get(role)
        if state is None:
            return {}

        url = f"{state.endpoint}/health"
        t0 = time.monotonic()
        success = False
        status_code = None

        try:
            with urllib.request.urlopen(url, timeout=self._probe_timeout) as resp:
                status_code = resp.status
                success = (status_code == 200)
        except Exception:
            pass

        latency_ms = (time.monotonic() - t0) * 1000
        now = _now_iso()

        with self._lock:
            old_status = state.status
            state.last_check_at = now

            if success:
                state.failure_count = 0
                state.update_latency(latency_ms)
                state.last_success_at = now

                if latency_ms > self._degraded_latency_ms:
                    new_status = BackendStatus.DEGRADED
                else:
                    new_status = BackendStatus.HEALTHY
            else:
                state.failure_count += 1
                if state.failure_count >= self._failure_threshold:
                    new_status = BackendStatus.OFFLINE
                else:
                    # Still within failure tolerance — keep previous status
                    new_status = old_status if old_status != BackendStatus.UNKNOWN else BackendStatus.OFFLINE

            if new_status != old_status:
                state.status = new_status
                state.transition_at = now
                logger.info(
                    "[backend_probe] %s: %s → %s (latency=%.0fms, failures=%d)",
                    role, old_status, new_status, latency_ms, state.failure_count,
                )
                self._emit_transition(role, old_status, new_status, latency_ms)

            result = state.to_dict()

        # Update topology state if applicable
        self._sync_topology(role, new_status if success else state.status, success)

        return result

    def _sync_topology(self, role: str, status: str, success: bool) -> None:
        """Keep RuntimeTopology in sync with probe findings."""
        if not self._topology:
            return
        try:
            if status == BackendStatus.HEALTHY and success:
                server = self._topology.servers.get(role)
                if server and not server.is_ready():
                    self._topology.mark_ready(role, server.pid)
            elif status == BackendStatus.OFFLINE:
                self._topology.mark_error(role, "health probe: offline")
        except Exception as e:
            logger.debug("topology sync error for %s: %s", role, e)

    def _emit_transition(
        self, role: str, old: str, new: str, latency_ms: float
    ) -> None:
        """Publish a transition signal to the bus (if connected)."""
        if not self._bus:
            return
        try:
            from runtime.signal_bus import SignalEnvelope
            category = "system_health"
            if new == BackendStatus.OFFLINE:
                priority = "high"
            elif new == BackendStatus.DEGRADED:
                priority = "medium"
            else:
                priority = "low"

            envelope = SignalEnvelope(
                signal_id=f"backend_probe.{role}.{new}",
                source="backend_health_probe",
                category=category,
                priority=priority,
                payload={
                    "role": role,
                    "old_status": old,
                    "new_status": new,
                    "latency_ms": round(latency_ms, 1),
                },
                correlation_key=f"backend.{role}",
            )
            self._bus.publish(envelope)
        except Exception as e:
            logger.debug("signal bus publish error: %s", e)
