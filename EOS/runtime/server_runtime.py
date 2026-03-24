from __future__ import annotations

import errno
import logging
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

logger = logging.getLogger("eos.server_runtime")


class BootError(RuntimeError):
    """Fatal server launch / boot failure."""


def _warn_on_ambiguous_choice(role_label: str, candidates: list[Path], selected: Path) -> None:
    if len(candidates) <= 1:
        return
    logger.warning(
        "[%s] Multiple GGUF files found; selected %s from: %s. "
        "Set an explicit file path in config to remove ambiguity.",
        role_label,
        selected.name,
        ", ".join(path.name for path in candidates),
    )


def resolve_model_path(model_path: str, root: Path, *, role: str = "model") -> Path | None:
    """Resolve a model path that may point to a file or directory."""
    path = Path(model_path) if Path(model_path).is_absolute() else root / model_path
    if path.is_file():
        return path
    if path.is_dir():
        candidates = sorted(
            f for f in path.glob("*.gguf")
            if not f.name.lower().startswith("mmproj")
        )
        if not candidates:
            return None
        selected = candidates[0]
        _warn_on_ambiguous_choice(role, candidates, selected)
        return selected
    return None


def resolve_mmproj_path(mmproj_path: str, root: Path, *, role: str = "mmproj") -> Path | None:
    """Resolve an mmproj path that may point to a file or directory."""
    path = Path(mmproj_path) if Path(mmproj_path).is_absolute() else root / mmproj_path
    if path.is_file():
        return path
    if path.is_dir():
        candidates = sorted(path.glob("mmproj*.gguf"))
        if not candidates:
            return None
        selected = candidates[0]
        _warn_on_ambiguous_choice(role, candidates, selected)
        return selected
    return None


def resolve_binary(srv_cfg: dict, root: Path) -> Path:
    """Pick GPU binary if n_gpu_layers > 0, else CPU binary."""
    layers = srv_cfg.get("n_gpu_layers", 0)
    key = "binary_gpu" if layers > 0 else "binary_cpu"
    for try_key in (key, "binary_gpu", "binary_cpu"):
        val = srv_cfg.get(try_key)
        if val:
            path = root / val
            if path.is_file():
                return path
    raise BootError(f"No valid llama-server binary found for server config: {srv_cfg}")


def is_port_bound(host: str, port: int) -> bool:
    """Return True when *host:port* is already bound by another process."""
    if port <= 0:
        return False

    probe_host = host
    if host in {"0.0.0.0", "::", ""}:
        probe_host = None

    bind_errors: list[OSError] = []
    for family, socktype, proto, _canonname, sockaddr in socket.getaddrinfo(
        probe_host,
        port,
        type=socket.SOCK_STREAM,
        flags=socket.AI_PASSIVE if probe_host is None else 0,
    ):
        sock = socket.socket(family, socktype, proto)
        try:
            sock.bind(sockaddr)
            return False
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return True
            bind_errors.append(exc)
        finally:
            sock.close()

    if bind_errors:
        raise bind_errors[0]
    return False


def launch_server(role: str, srv_cfg: dict, root: Path) -> subprocess.Popen:
    """Start a llama-server process and return the Popen handle."""
    binary = resolve_binary(srv_cfg, root)
    host = srv_cfg.get("host", "127.0.0.1")
    port = srv_cfg["port"]
    ctx = srv_cfg.get("context_size", 4096)
    layers = srv_cfg.get("n_gpu_layers", 0)
    parallel = srv_cfg.get("parallel", 1)

    model = resolve_model_path(srv_cfg["model_path"], root, role=role)
    if model is None:
        raise BootError(
            f"[{role}] No model file found at: {srv_cfg['model_path']} — "
            f"place a .gguf file in that directory."
        )

    cmd = [
        str(binary),
        "--model", str(model),
        "--host", host,
        "--port", str(port),
        "--ctx-size", str(ctx),
        "--n-gpu-layers", str(layers),
        "--parallel", str(parallel),
        "--log-disable",
    ]

    mmproj_str = srv_cfg.get("mmproj_path")
    if mmproj_str:
        mmproj = resolve_mmproj_path(mmproj_str, root, role=f"{role}:mmproj")
        if mmproj is None:
            raise BootError(f"[{role}] mmproj file not found at: {mmproj_str}")
        cmd += ["--mmproj", str(mmproj)]

    logger.info("[%s] Launching: port=%d layers=%d model=%s", role, port, layers, model.name)

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


def wait_for_health(
    role: str,
    endpoint: str,
    timeout: float = 120.0,
    poll_interval: float = 2.0,
    proc: subprocess.Popen | None = None,
) -> bool:
    """Poll /health until 200 or timeout."""
    deadline = time.time() + timeout
    start = time.time()
    url = f"{endpoint}/health"
    logger.info("[%s] Waiting for health at %s (timeout=%.0fs)…", role, url, timeout)

    while time.time() < deadline:
        if proc is not None:
            ret = proc.poll()
            if ret is not None:
                logger.error(
                    "[%s] Process exited with code %d before health check passed",
                    role, ret,
                )
                return False

        try:
            response = httpx.get(url, timeout=5.0)
            if response.status_code == 200:
                elapsed = time.time() - start
                logger.info("[%s] READY (%.1fs)", role, elapsed)
                return True
        except httpx.ConnectError:
            pass
        except Exception as exc:
            logger.debug("[%s] Health probe error: %s", role, exc)

        time.sleep(poll_interval)

    logger.error("[%s] Timed out waiting for health after %.0fs", role, timeout)
    return False


def wait_for_health_with_retry(
    role: str,
    endpoint: str,
    timeout: float = 120.0,
    poll_interval: float = 2.0,
    proc: subprocess.Popen | None = None,
    retries: int = 1,
) -> bool:
    """Retry health polling once to filter transient startup failures."""
    if wait_for_health(role, endpoint, timeout, poll_interval, proc=proc):
        return True

    if retries > 0:
        logger.warning("[%s] Health check failed — retrying once (10s window)…", role)
        return wait_for_health(role, endpoint, 10.0, 1.0, proc=proc)

    return False
