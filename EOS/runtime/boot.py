"""
EOS — Boot System
Config-driven deterministic startup. Implements RUNTIME_INVARIANTS §§3-5.

Sequence:
  1. Load and validate config
  2. Build RuntimeTopology (all servers in PENDING/ABSENT state)
  3. Launch llama-server processes for enabled servers
  4. Wait for /health on each required server
  5. Apply graceful degradation rules
  6. Return ready RuntimeTopology or raise BootError

Invariant enforcement:
  - Primary unavailable → BootError (fatal)
  - Vision absent in vision mode → BootError (fatal)
  - Any optional server absent → log + continue
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from runtime.server_runtime import (
    BootError,
    launch_server,
    resolve_mmproj_path as _shared_resolve_mmproj_path,
    resolve_model_path as _shared_resolve_model_path,
    wait_for_health_with_retry,
)
from runtime.server_activation import normalize_activation_config
from runtime.topology import (
    RuntimeTopology,
    ServerState,
    ServerStatus,
    build_topology_from_config,
)

logger = logging.getLogger("eos.boot")


# ── Config loading ────────────────────────────────────────────────────────────

def load_config(config_path: str | Path) -> dict[str, Any]:
    """Load and minimally validate the JSON config."""
    p = Path(config_path)
    if not p.is_file():
        raise BootError(f"Config file not found: {p}")
    with p.open(encoding="utf-8") as f:
        cfg = json.load(f)
    cfg = normalize_activation_config(cfg)
    if "deployment_mode" not in cfg:
        raise BootError("Config missing required field: deployment_mode")
    if cfg["deployment_mode"] not in ("standard", "vision"):
        raise BootError(f"Invalid deployment_mode: {cfg['deployment_mode']!r}")
    logger.info("Config loaded: %s (mode=%s)", p.name, cfg["deployment_mode"])
    return cfg


# ── Model path resolution ─────────────────────────────────────────────────────

# Backward-compatible aliases for modules that still import these helpers from runtime.boot.


def _resolve_model_path(model_path: str, root: Path, *, role: str = "model") -> Path | None:
    return _shared_resolve_model_path(model_path, root, role=role)


def _resolve_mmproj_path(mmproj_path: str, root: Path, *, role: str = "mmproj") -> Path | None:
    return _shared_resolve_mmproj_path(mmproj_path, root, role=role)


# ── Main boot entry ───────────────────────────────────────────────────────────

def boot(config_path: str | Path, root: Path | None = None) -> RuntimeTopology:
    """
    Full boot sequence. Returns a ready RuntimeTopology or raises BootError.

    root: project root directory. Defaults to parent of config_path.
    """
    config_path = Path(config_path)
    if root is None:
        root = config_path.parent

    cfg      = load_config(config_path)
    topology = build_topology_from_config(cfg)
    procs: dict[str, subprocess.Popen] = {}

    mode = cfg["deployment_mode"]
    logger.info("Booting EOS in %s mode", mode)

    # ── Phase 1: Launch enabled servers ───────────────────────────────────────
    servers_cfg = cfg.get("servers", {})

    for role, srv_cfg in servers_cfg.items():
        if not srv_cfg.get("enabled", False):
            logger.info("[%s] Disabled — skipping", role)
            topology.mark_absent(role, intentional=True)
            continue

        if str(srv_cfg.get("activation_mode", "persistent")) == "on_demand":
            logger.info("[%s] Managed as on-demand auxiliary — not started during baseline boot", role)
            topology.mark_absent(role, intentional=True)
            continue

        try:
            proc = launch_server(role, srv_cfg, root)
            procs[role] = proc
            topology.mark_starting(role, proc.pid)
        except BootError as e:
            if srv_cfg.get("required", False):
                _terminate_all(procs)
                raise
            else:
                logger.warning("[%s] Optional server failed to launch: %s", role, e)
                topology.mark_error(role, str(e))

    # ── Phase 2: Wait for health ───────────────────────────────────────────────
    for role, proc in procs.items():
        srv_cfg  = servers_cfg[role]
        port     = srv_cfg["port"]
        endpoint = f"http://127.0.0.1:{port}"
        required = srv_cfg.get("required", False)

        timeout  = srv_cfg.get("health_timeout", 120.0)
        ready = wait_for_health_with_retry(
            role, endpoint,
            timeout=timeout,
            poll_interval=2.0,
            proc=procs.get(role),
        )

        if ready:
            topology.mark_ready(role, proc.pid)
        else:
            topology.mark_error(role, "health check timed out")
            if required:
                _terminate_all(procs)
                raise BootError(
                    f"Required server '{role}' failed to become ready. "
                    f"Check that the model file exists and the binary is compatible."
                )
            else:
                logger.warning("[%s] Optional — continuing without it", role)

    # ── Phase 3: Invariant enforcement ────────────────────────────────────────

    # Invariant 5a: Primary unavailable → fatal
    primary = topology.server("primary")
    if not primary or not primary.is_ready():
        _terminate_all(procs)
        raise BootError(
            "Primary server (Qwen3) is not ready. "
            "EOS cannot run without a cognitive center. "
            "Verify the model file exists and GPU drivers are available."
        )

    # Invariant 5b: Vision mode without vision server → fatal
    if mode == "vision":
        vision = topology.server("vision")
        if not vision or not vision.is_ready():
            _terminate_all(procs)
            raise BootError(
                "Vision mode requires a ready vision server but none is available. "
                "This is an error state, not a degraded state. "
                "Switch to standard mode or fix the vision server."
            )

    # Log optional server states
    # Creativity is first-class in architecture but optional at runtime —
    # its absence must never prevent boot or interrupt execution paths.
    optional_roles = ("tool", "thinking", "creativity")
    for role in optional_roles:
        s = topology.server(role)
        if s:
            if s.is_ready():
                logger.info("[%s] Available", role)
            elif s.is_absent():
                logger.info("[%s] Not configured", role)
            else:
                logger.warning("[%s] Unavailable (%s) — continuing", role, s.status.value)

    logger.info("Boot complete. %s", topology)
    return topology


# ── Helpers ───────────────────────────────────────────────────────────────────

def _terminate_all(procs: dict[str, subprocess.Popen]) -> None:
    """Terminate all launched processes on boot failure."""
    for role, proc in procs.items():
        try:
            proc.terminate()
            logger.info("[%s] Terminated (PID %d)", role, proc.pid)
        except Exception:
            pass


def shutdown(topology: RuntimeTopology) -> None:
    """
    Graceful shutdown: terminate all known llama-server PIDs.
    Called on Ctrl+C or SIGTERM.
    """
    logger.info("Shutting down EOS servers...")
    for role, state in topology.servers.items():
        if state.pid:
            try:
                import signal as _signal
                if sys.platform == "win32":
                    subprocess.call(
                        ["taskkill", "/F", "/PID", str(state.pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    os.kill(state.pid, _signal.SIGTERM)
                logger.info("[%s] Sent termination to PID %d", role, state.pid)
            except Exception as exc:
                logger.warning("[%s] Could not terminate PID %d: %s", role, state.pid, exc)
    logger.info("Shutdown complete.")
