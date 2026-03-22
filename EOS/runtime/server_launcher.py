from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from runtime.boot import (
    BootError,
    _launch_server,
    _wait_for_health_with_retry,
    load_config,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("eos.server_launcher")

ROOT = Path(__file__).resolve().parent.parent

ROLE_ALIASES = {
    "main": "primary",
    "primary": "primary",
    "tool": "tool",
    "tools": "tool",
    "thinking": "thinking",
    "creativity": "creativity",
    "vision": "vision",
}


def _normalize_role(role: str) -> str:
    key = role.strip().lower()
    if key not in ROLE_ALIASES:
        raise BootError(f"Unknown server role: {role}")
    return ROLE_ALIASES[key]


def _apply_accelerator(role: str, srv_cfg: dict, accel: str) -> dict:
    cfg = dict(srv_cfg)
    binary_key = f"binary_{accel}"
    if not cfg.get(binary_key):
        raise BootError(f"Role '{role}' does not define {binary_key} in config.json")

    if accel == "cpu":
        cfg["binary_cpu"] = cfg[binary_key]
        cfg["n_gpu_layers"] = int(cfg.get("cpu_n_gpu_layers", 0))
    else:
        cfg["binary_gpu"] = cfg[binary_key]
        cfg["n_gpu_layers"] = int(cfg.get("gpu_n_gpu_layers", cfg.get("n_gpu_layers", 99)))
    return cfg


def _stop_process(proc) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def launch_role(config_path: Path, role: str, accel: str, root: Path | None = None) -> int:
    if root is None:
        root = config_path.parent
    cfg = load_config(config_path)
    role = _normalize_role(role)
    accel = accel.lower()
    if accel not in {"cpu", "gpu"}:
        raise BootError(f"Unsupported accelerator: {accel}")

    srv_cfg = cfg.get("servers", {}).get(role)
    if not srv_cfg:
        raise BootError(f"Role '{role}' not found in config")

    launch_cfg = _apply_accelerator(role, srv_cfg, accel)
    host = launch_cfg.get("host", "127.0.0.1")
    port = int(launch_cfg.get("port", 0))
    endpoint = f"http://{host}:{port}"

    logger.info("[%s] Starting %s server on %s", role, accel.upper(), endpoint)
    proc = _launch_server(role, launch_cfg, root)

    def _terminate(*_args):
        logger.info("[%s] Stopping server (PID %d)", role, proc.pid)
        _stop_process(proc)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _terminate)
    signal.signal(signal.SIGTERM, _terminate)

    timeout = float(launch_cfg.get("health_timeout", 120.0))
    if not _wait_for_health_with_retry(role, endpoint, timeout=timeout, poll_interval=2.0, proc=proc):
        _stop_process(proc)
        raise BootError(f"[{role}] failed to pass health checks at {endpoint}/health")

    logger.info("[%s] Active and healthy at %s", role, endpoint)
    try:
        return proc.wait()
    finally:
        logger.info("[%s] Server exited", role)


def main() -> None:
    parser = argparse.ArgumentParser(description="Start a single EOS backend server.")
    parser.add_argument("role", help="Role to start: main|tool|thinking|creativity|vision")
    parser.add_argument("--accel", choices=("cpu", "gpu"), required=True, help="Execution target for this server")
    parser.add_argument("--config", default="config.json", help="Config file to read (default: config.json)")
    args = parser.parse_args()

    config_path = Path(args.config) if Path(args.config).is_absolute() else ROOT / args.config
    try:
        code = launch_role(config_path, args.role, args.accel, root=ROOT)
    except BootError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    sys.exit(code)


if __name__ == "__main__":
    main()
