"""Toolpacks package — Optional extensions that register tools into the registry

Toolpacks are importable modules that implement a register(registry, config) function.
This function is called during startup to add tools to the central ToolRegistry.

Each pack has:
  - A register(registry, config) -> None function that does the registration
  - One or more ToolSpec definitions
  - Handler functions that implement the tool logic

Configuration
==============

Toolpacks are loaded via the "toolpacks" section in the runtime config:

  toolpacks:
    enabled: true
    packs:
      - fs_tools
      - git_tools
      - text_tools

Packs require explicit opt-in (null/missing packs means load nothing).

Required packs will cause startup to fail if they don't load:

  toolpacks:
    required_packs:
      - fs_tools

Risk and trust assignments
==========================

Each tool is assigned:
  - risk_level: "read_only" | "draft" | "reversible_commit" | "irreversible_commit"
  - trust_level: "public" | "verified_user" | "operator_only"
  - confirmation_policy: "none" | "soft_confirm" | "hard_confirm"

General rules:
  - read_only + public + none → file reads, status checks, listing
  - read_only + verified_user + none → Gmail search, git status
  - draft + verified_user + soft_confirm → file writes, git add/commit
  - reversible_commit + verified_user + soft_confirm → calendar create, git push
  - irreversible_commit + operator_only + hard_confirm → Gmail send, delete, service control

Tags for broker filtering
=========================

Tools are tagged for context-aware discovery:
  - files: file system operations
  - git: version control
  - text: text processing
  - web: web fetch/search
  - notifications: Discord, email
  - scheduling: calendar, scheduler
  - system: process, service control, diagnostics
  - security: secrets, security tools
  - packages: package management
  - network: network tools
  - google: Google Workspace
  - ingestion: data ingestion

Built-in packs included
========================

fs_tools                — file system: read, write, list, delete (with safeguards)
workspace_tools        — workspace files: list, read, write (sandboxed)
git_tools              — git operations: status, log, diff, add, commit, push
text_tools             — text processing: strip ANSI, normalize, wrap, extract JSON
diff_tools             — diff operations: unified diff, apply patches
network_tools          — network diagnostics: ping, DNS, traceroute
http_diag_tools        — HTTP diagnostics: curl, resolve, headers
notifications_tools    — notifications: send Discord/email (with confirmation)
scheduler_tools        — schedule tools: cron, at, scheduler
process_tools          — process control: list, signal, monitor
service_control_tools  — service control: start, stop, restart (admin only)
system_cmd_tools       — safe system commands: readonly shell execution
secrets_tools          — secrets hygiene: rotate, audit, scan
telemetry_tools        — telemetry: metrics, logs, diagnostics
event_journal_tools    — event journal: record, query
ingestion_tools        — ingestion: import data, parse formats
package_tools          — package management: list, show, info
ca_tools               — CA tools: certificate diagnostics and fixes
recovery_tools         — recovery: backup, restore, recovery operations
google_tools           — Google Workspace: Gmail, Calendar, Docs (with API setup)
privileged_tools       — privileged operations: disabled by default
deterministic_tools    — deterministic: time, calculator, system info
"""

from __future__ import annotations

import logging
from importlib import import_module
from typing import Any, Dict

logger = logging.getLogger(__name__)


def try_register_pack(
    registry: Any,
    pack_name: str,
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Best-effort import and register a toolpack.

    Loads a toolpack module from this package and calls its register() function
    with the ToolRegistry and config dict. Catches all exceptions and returns
    a status dict suitable for logging/manifests.

    Parameters
    ----------
    registry : ToolRegistry
        The tool registry to register into.
    pack_name : str
        The pack module name (e.g., "fs_tools", "git_tools").
    config : dict, optional
        Configuration dict passed to the pack's register() function.

    Returns
    -------
    dict
        Status dict with keys:
        - status: "ok" | "skipped" | "failed"
        - error: Optional error message if status is "failed"

    Examples
    --------
    >>> result = try_register_pack(registry, "fs_tools", config)
    >>> if result["status"] == "ok":
    ...     print("fs_tools loaded successfully")
    """
    nm = str(pack_name).strip()
    if not nm:
        return {"status": "skipped", "error": "pack_name is empty"}

    config = config or {}

    try:
        # Import the pack module
        mod = import_module(f"{__name__}.{nm}")

        # Call its register() function if it exists
        if not hasattr(mod, "register"):
            return {
                "status": "failed",
                "error": f"Pack module '{nm}' has no register() function",
            }

        # Call register(registry, config)
        mod.register(registry, config)
        logger.info(f"[toolpack] Loaded pack: {nm}")
        return {"status": "ok"}

    except ImportError as e:
        return {
            "status": "failed",
            "error": f"Could not import pack '{nm}': {e}",
        }
    except Exception as e:
        return {
            "status": "failed",
            "error": f"Error registering pack '{nm}': {type(e).__name__}: {e}",
        }
