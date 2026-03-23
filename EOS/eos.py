"""
EOS — Entry Point
=================
Starts the EOS WebUI after discovering whatever backends are already running.

Usage:
  python eos.py
  python eos.py --status
  python eos.py --host 0.0.0.0 --port 9000
  python eos.py --config config.json
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from runtime.launch_catalog import LEGACY_SURFACES
from runtime.service_discovery import discover_runtime, format_runtime_summary
from webui.server import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("eos")

ROOT = Path(__file__).parent.resolve()


def _banner(title: str) -> None:
    cyan = "\033[36m"
    reset = "\033[0m"
    line = "=" * 52
    print(f"\n{cyan}{line}{reset}")
    print(f"{cyan}  {title}{reset}")
    print(f"{cyan}{line}{reset}\n")


def _find_config(arg: str | None) -> Path:
    if arg:
        candidate = Path(arg) if Path(arg).is_absolute() else ROOT / arg
        if candidate.is_file():
            return candidate
        logger.error("Config file not found: %s", candidate)
        sys.exit(1)

    candidate = ROOT / "config.json"
    if candidate.is_file():
        return candidate

    logger.error("No config.json found in %s", ROOT)
    sys.exit(1)


def _print_summary(discovery) -> None:
    print(format_runtime_summary(discovery))
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="eos",
        description="EOS — discover running backends and start the WebUI.",
    )
    parser.add_argument("--config", metavar="FILE", default=None, help="Path to the canonical JSON config file.")
    parser.add_argument("--host", default=None, help="Host for the WebUI server.")
    parser.add_argument("--port", type=int, default=None, help="Port for the WebUI server.")
    parser.add_argument("--status", action="store_true", help="Print backend discovery and effective capabilities, then exit.")
    parser.add_argument("--profile", default=None, help="Deprecated legacy flag; ignored.")
    parser.add_argument("--no-boot", action="store_true", help="Deprecated legacy flag; ignored because eos.py no longer launches model servers.")
    args = parser.parse_args()

    if args.profile:
        logger.warning("--profile is deprecated and ignored. Use %s.", LEGACY_SURFACES["eos.py --profile"]["replacement"])
    if args.no_boot:
        logger.warning("--no-boot is deprecated and ignored. Use %s.", LEGACY_SURFACES["eos.py --no-boot"]["replacement"])

    config_path = _find_config(args.config)
    discovery = discover_runtime(config_path, root=ROOT)

    _banner("EOS  |  Runtime Discovery")
    _print_summary(discovery)

    if args.status:
        return

    os.environ["EOS_ROOT"] = str(ROOT)
    os.chdir(ROOT)

    webui_cfg = discovery.config.get("webui", {})
    host = args.host or webui_cfg.get("host", "127.0.0.1")
    port = args.port or webui_cfg.get("port", 7860)

    logger.info("Starting WebUI at http://%s:%d/", host, port)
    logger.info("Admin panel at  http://%s:%d/admin", host, port)

    import uvicorn
    app = create_app(config_path=config_path)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
