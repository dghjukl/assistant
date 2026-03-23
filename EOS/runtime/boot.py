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
import time
from pathlib import Path
from typing import Any

import httpx

from runtime.topology import (
    RuntimeTopology,
    ServerState,
    ServerStatus,
    build_topology_from_config,
)

logger = logging.getLogger("eos.boot")


# ── Exceptions ────────────────────────────────────────────────────────────────

class BootError(RuntimeError):
    """Fatal boot failure. System cannot continue."""


# ── Config loading ────────────────────────────────────────────────────────────

def load_config(config_path: str | Path) -> dict[str, Any]:
    """Load and minimally validate the JSON config."""
    p = Path(config_path)
    if not p.is_file():
        raise BootError(f"Config file not found: {p}")
    with p.open(encoding="utf-8") as f:
        cfg = json.load(f)
    if "deployment_mode" not in cfg:
        raise BootError("Config missing required field: deployment_mode")
    if cfg["deployment_mode"] not in ("standard", "vision"):
        raise BootError(f"Invalid deployment_mode: {cfg['deployment_mode']!r}")
    logger.info("Config loaded: %s (mode=%s)", p.name, cfg["deployment_mode"])
    return cfg


# ── Model path resolution ─────────────────────────────────────────────────────

def _warn_on_ambiguous_choice(role_label: str, candidates: list[Path], selected: Path) -> None:
    """Log a warning when directory-based model selection is ambiguous."""
    if len(candidates) <= 1:
        return
    logger.warning(
        "[%s] Multiple GGUF files found; selected %s from: %s. "
        "Set an explicit file path in config to remove ambiguity.",
        role_label,
        selected.name,
        ", ".join(path.name for path in candidates),
    )


def _resolve_model_path(model_path: str, root: Path, *, role: str = "model") -> Path | None:
    """
    Resolve a model_path that may be either:
      - A specific file:  "models/primary/Qwen3-8B-Q6_K.gguf"
      - A directory:      "models/primary/"

    Directory mode: returns the first .gguf in the directory that is NOT
    an mmproj file, sorted alphabetically (deterministic).
    This makes the config directory-dependent, not filename-dependent —
    upgrading a model is a drop-in with no config changes required.

    Returns None if the path doesn't exist or no .gguf is found.
    """
    p = Path(model_path) if Path(model_path).is_absolute() else root / model_path
    if p.is_file():
        return p
    if p.is_dir():
        candidates = sorted(
            f for f in p.glob("*.gguf")
            if not f.name.lower().startswith("mmproj")
        )
        if not candidates:
            return None
        selected = candidates[0]
        _warn_on_ambiguous_choice(role, candidates, selected)
        return selected
    return None


def _resolve_mmproj_path(mmproj_path: str, root: Path, *, role: str = "mmproj") -> Path | None:
    """
    Resolve an mmproj_path that may be a file or a directory.
    Directory mode: returns the first mmproj*.gguf found.
    """
    p = Path(mmproj_path) if Path(mmproj_path).is_absolute() else root / mmproj_path
    if p.is_file():
        return p
    if p.is_dir():
        candidates = sorted(p.glob("mmproj*.gguf"))
        if not candidates:
            return None
        selected = candidates[0]
        _warn_on_ambiguous_choice(role, candidates, selected)
        return selected
    return None


# ── Process launcher ──────────────────────────────────────────────────────────

def _resolve_binary(srv_cfg: dict, root: Path) -> Path:
    """Pick GPU binary if n_gpu_layers > 0, else CPU binary."""
    layers = srv_cfg.get("n_gpu_layers", 0)
    key = "binary_gpu" if layers > 0 else "binary_cpu"
    # Fallback: try the other binary if preferred not found
    for try_key in (key, "binary_gpu", "binary_cpu"):
        val = srv_cfg.get(try_key)
        if val:
            p = root / val
            if p.is_file():
                return p
    raise BootError(
        f"No valid llama-server binary found for server config: {srv_cfg}"
    )


def _launch_server(role: str, srv_cfg: dict, root: Path) -> subprocess.Popen:
    """Start a llama-server process. Returns the Popen handle."""
    binary = _resolve_binary(srv_cfg, root)
    host   = srv_cfg.get("host", "127.0.0.1")
    port   = srv_cfg["port"]
    ctx      = srv_cfg.get("context_size", 4096)
    layers   = srv_cfg.get("n_gpu_layers", 0)
    parallel = srv_cfg.get("parallel", 1)

    # Resolve model path — supports both specific files and bare directories.
    model = _resolve_model_path(srv_cfg["model_path"], root, role=role)
    if model is None:
        raise BootError(
            f"[{role}] No model file found at: {srv_cfg['model_path']} — "
            f"place a .gguf file in that directory."
        )

    cmd = [
        str(binary),
        "--model",        str(model),
        "--host",         host,
        "--port",         str(port),
        "--ctx-size",     str(ctx),
        "--n-gpu-layers", str(layers),
        "--parallel",     str(parallel),
        "--log-disable",  # suppress llama.cpp verbose stdout
    ]

    # Resolve mmproj — supports both specific files and bare directories.
    mmproj_str = srv_cfg.get("mmproj_path")
    if mmproj_str:
        mmproj = _resolve_mmproj_path(mmproj_str, root, role=f"{role}:mmproj")
        if mmproj:
            cmd += ["--mmproj", str(mmproj)]
        else:
            logger.warning("[%s] mmproj not found at %s — skipping", role, mmproj_str)

    logger.info("[%s] Launching: port=%d layers=%d model=%s", role, port, layers, model.name)

    # On Windows, create process without a console window
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )
    logger.info("[%s] PID %d", role, proc.pid)
    return proc


# ── Health checks ─────────────────────────────────────────────────────────────

def _wait_for_health(
    role: str,
    endpoint: str,
    timeout: float = 120.0,
    poll_interval: float = 2.0,
    proc: "subprocess.Popen | None" = None,
) -> bool:
    """Poll /health until 200 or timeout.

    If proc is supplied, checks process liveness every poll cycle — exits early
    if the process has already crashed (returncode is set), instead of waiting
    out the full timeout.

    Returns True if ready, False if timed out or process died.
    """
    deadline = time.time() + timeout
    start    = time.time()
    url      = f"{endpoint}/health"
    logger.info("[%s] Waiting for health at %s (timeout=%.0fs)…", role, url, timeout)

    while time.time() < deadline:
        # Check process liveness first — fail fast if it's already dead
        if proc is not None:
            ret = proc.poll()
            if ret is not None:
                logger.error(
                    "[%s] Process exited with code %d before health check passed",
                    role, ret,
                )
                return False

        try:
            r = httpx.get(url, timeout=5.0)
            if r.status_code == 200:
                elapsed = time.time() - start
                logger.info("[%s] READY (%.1fs)", role, elapsed)
                return True
        except httpx.ConnectError:
            pass  # Normal during startup — server not listening yet
        except Exception as exc:
            logger.debug("[%s] Health probe error: %s", role, exc)

        time.sleep(poll_interval)

    logger.error("[%s] Timed out waiting for health after %.0fs", role, timeout)
    return False


def _wait_for_health_with_retry(
    role: str,
    endpoint: str,
    timeout: float = 120.0,
    poll_interval: float = 2.0,
    proc: "subprocess.Popen | None" = None,
    retries: int = 1,
) -> bool:
    """Wrapper around _wait_for_health that retries once on failure.

    The retry uses a shorter timeout (10s) to confirm the failure is real
    and not a transient blip.
    """
    if _wait_for_health(role, endpoint, timeout, poll_interval, proc=proc):
        return True

    if retries > 0:
        logger.warning(
            "[%s] Health check failed — retrying once (10s window)…", role
        )
        return _wait_for_health(role, endpoint, 10.0, 1.0, proc=proc)

    return False


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
            topology.mark_absent(role)
            continue

        try:
            proc = _launch_server(role, srv_cfg, root)
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
        ready = _wait_for_health_with_retry(
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
