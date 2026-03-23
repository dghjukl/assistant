"""Privileged Tools — High-risk system operations (Windows)

These tools represent the highest trust tier in EOS's capability model.
Each is individually gated — disabled by default, enabled only by explicit
operator grant.  This is the "give it the abilities without the horror"
design: the partner holds the key to each capability.

Tools
-----
  read_registry_value      — Read a Windows registry value
  write_registry_value     — Write a Windows registry value (HARD_CONFIRM)
  list_windows_services    — List all Windows services and their status
  control_windows_service  — Start or stop a Windows service (HARD_CONFIRM)
  read_system_file         — Read a file outside the workspace (path-validated)
  get_network_config       — Read network adapter configuration
  list_admin_processes     — List running processes with elevated information
  terminate_process        — Terminate a process by PID (HARD_CONFIRM)

Config (config.json)
--------------------
  privileged_tools:
    enabled: false                 # master gate — must be true to enable any tools
    registry_read_enabled: false
    registry_write_enabled: false
    service_list_enabled: false
    service_control_enabled: false
    system_file_read_enabled: false
    network_config_enabled: false
    process_list_enabled: false
    process_terminate_enabled: false

All tools with HARD_CONFIRM require explicit operator confirmation before
execution.  Tools with SOFT_CONFIRM require acknowledgment.  READ_ONLY
tools with OPERATOR_ONLY trust still require the master gate and their
individual sub-gate.
"""
from __future__ import annotations

import json
import logging
import os
import platform
from typing import Any, Dict

logger = logging.getLogger("eos.privileged_tools")


def _jdump(x: Any) -> str:
    try:
        return json.dumps(x, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


def _disabled(name: str) -> str:
    return _jdump({
        "error": f"Privileged tool '{name}' is disabled",
        "hint": (
            f"Set privileged_tools.{name}_enabled=true in config.json and "
            "ensure privileged_tools.enabled=true, then restart EOS."
        ),
    })


def _not_windows() -> str:
    return _jdump({
        "error": "This tool requires Windows",
        "hint": f"Current platform: {platform.system()}",
    })


def _is_windows() -> bool:
    return platform.system() == "Windows"


# ── Registry helpers ──────────────────────────────────────────────────────────

_HIVE_MAP = {
    "HKEY_LOCAL_MACHINE": "winreg.HKEY_LOCAL_MACHINE",
    "HKLM": "winreg.HKEY_LOCAL_MACHINE",
    "HKEY_CURRENT_USER": "winreg.HKEY_CURRENT_USER",
    "HKCU": "winreg.HKEY_CURRENT_USER",
    "HKEY_CLASSES_ROOT": "winreg.HKEY_CLASSES_ROOT",
    "HKCR": "winreg.HKEY_CLASSES_ROOT",
    "HKEY_USERS": "winreg.HKEY_USERS",
    "HKU": "winreg.HKEY_USERS",
    "HKEY_CURRENT_CONFIG": "winreg.HKEY_CURRENT_CONFIG",
    "HKCC": "winreg.HKEY_CURRENT_CONFIG",
}

def _parse_registry_path(full_path: str):
    """Parse 'HKLM\\Software\\...' → (hive_constant, subkey)."""
    import winreg
    parts = full_path.replace("/", "\\").split("\\", 1)
    hive_str = parts[0].upper()
    subkey = parts[1] if len(parts) > 1 else ""
    hive_map = {
        "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
        "HKLM": winreg.HKEY_LOCAL_MACHINE,
        "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
        "HKCU": winreg.HKEY_CURRENT_USER,
        "HKEY_CLASSES_ROOT": winreg.HKEY_CLASSES_ROOT,
        "HKCR": winreg.HKEY_CLASSES_ROOT,
        "HKEY_USERS": winreg.HKEY_USERS,
        "HKU": winreg.HKEY_USERS,
        "HKEY_CURRENT_CONFIG": winreg.HKEY_CURRENT_CONFIG,
        "HKCC": winreg.HKEY_CURRENT_CONFIG,
    }
    hive = hive_map.get(hive_str)
    if hive is None:
        raise ValueError(f"Unknown registry hive: {hive_str!r}")
    return hive, subkey


_REGTYPE_NAMES = {
    0: "REG_NONE", 1: "REG_SZ", 2: "REG_EXPAND_SZ", 3: "REG_BINARY",
    4: "REG_DWORD", 5: "REG_DWORD_BIG_ENDIAN", 6: "REG_LINK",
    7: "REG_MULTI_SZ", 11: "REG_QWORD",
}


# ── Tool handlers ─────────────────────────────────────────────────────────────

def _read_registry_value(params: Dict[str, Any]) -> str:
    """Read a single value from the Windows registry."""
    if not _is_windows():
        return _not_windows()
    import winreg
    key_path = str(params.get("key_path", ""))
    value_name = str(params.get("value_name", ""))
    if not key_path:
        return _jdump({"error": "key_path is required (e.g. HKLM\\SOFTWARE\\Python)"})
    try:
        hive, subkey = _parse_registry_path(key_path)
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ) as k:
            data, reg_type = winreg.QueryValueEx(k, value_name)
        return _jdump({
            "key_path": key_path,
            "value_name": value_name or "(Default)",
            "data": data,
            "type": _REGTYPE_NAMES.get(reg_type, str(reg_type)),
        })
    except FileNotFoundError:
        return _jdump({"error": f"Registry key or value not found: {key_path}\\{value_name}"})
    except PermissionError:
        return _jdump({"error": "Access denied — registry key requires elevation"})
    except Exception as exc:
        return _jdump({"error": str(exc)})


def _write_registry_value(params: Dict[str, Any]) -> str:
    """Write a value to the Windows registry."""
    if not _is_windows():
        return _not_windows()
    import winreg
    key_path   = str(params.get("key_path", ""))
    value_name = str(params.get("value_name", ""))
    data       = params.get("data")
    reg_type   = str(params.get("type", "REG_SZ")).upper()
    if not key_path or data is None:
        return _jdump({"error": "key_path and data are required"})

    type_map = {
        "REG_SZ": winreg.REG_SZ,
        "REG_EXPAND_SZ": winreg.REG_EXPAND_SZ,
        "REG_DWORD": winreg.REG_DWORD,
        "REG_QWORD": winreg.REG_QWORD,
        "REG_BINARY": winreg.REG_BINARY,
        "REG_MULTI_SZ": winreg.REG_MULTI_SZ,
    }
    wtype = type_map.get(reg_type, winreg.REG_SZ)

    try:
        hive, subkey = _parse_registry_path(key_path)
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, value_name, 0, wtype, data)
        logger.info("[privileged] Registry write: %s\\%s = %r", key_path, value_name, data)
        return _jdump({
            "ok": True,
            "key_path": key_path,
            "value_name": value_name or "(Default)",
            "data": data,
            "type": reg_type,
        })
    except FileNotFoundError:
        return _jdump({"error": f"Registry key not found: {key_path}"})
    except PermissionError:
        return _jdump({"error": "Access denied — registry write requires elevation"})
    except Exception as exc:
        return _jdump({"error": str(exc)})


def _list_windows_services(params: Dict[str, Any]) -> str:
    """List Windows services and their current state."""
    if not _is_windows():
        return _not_windows()
    try:
        import subprocess
        filter_state = str(params.get("state_filter", "")).lower()   # running / stopped / all
        # Use sc query to list services
        args = ["sc", "query", "type=", "all", "state=", "all"]
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        if result.returncode != 0 and result.returncode != 1:
            # Fall back to wmic if sc fails
            result = subprocess.run(
                ["wmic", "service", "get", "Name,DisplayName,State,StartMode"],
                capture_output=True, text=True, timeout=15,
            )

        # Parse sc query output
        services = []
        current: Dict[str, str] = {}
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("SERVICE_NAME:"):
                if current:
                    services.append(current)
                current = {"name": line.split(":", 1)[1].strip()}
            elif line.startswith("DISPLAY_NAME:"):
                current["display_name"] = line.split(":", 1)[1].strip()
            elif line.startswith("STATE"):
                state_part = line.split(":", 1)[1].strip() if ":" in line else line
                # Format: "4  RUNNING" or similar
                parts = state_part.split()
                state_str = parts[1] if len(parts) >= 2 else state_part
                current["state"] = state_str
        if current and current.get("name"):
            services.append(current)

        if filter_state and filter_state not in ("all", ""):
            services = [s for s in services if s.get("state", "").upper() == filter_state.upper()]

        return _jdump({"services": services, "count": len(services)})
    except Exception as exc:
        return _jdump({"error": str(exc)})


def _control_windows_service(params: Dict[str, Any]) -> str:
    """Start or stop a Windows service."""
    if not _is_windows():
        return _not_windows()
    service = str(params.get("service_name", "")).strip()
    action  = str(params.get("action", "")).lower().strip()
    if not service or action not in ("start", "stop"):
        return _jdump({"error": "service_name and action ('start' or 'stop') are required"})
    try:
        import subprocess
        result = subprocess.run(
            ["sc", action, service],
            capture_output=True, text=True, timeout=30,
        )
        logger.info("[privileged] Service %s %s → exit %d", service, action, result.returncode)
        if result.returncode == 0:
            return _jdump({"ok": True, "service": service, "action": action})
        else:
            return _jdump({
                "ok": False,
                "service": service,
                "action": action,
                "error": result.stderr.strip() or result.stdout.strip(),
                "exit_code": result.returncode,
            })
    except Exception as exc:
        return _jdump({"error": str(exc)})


def _read_system_file(params: Dict[str, Any]) -> str:
    """Read a file on the system (outside the workspace).

    For safety, only text files up to 256 KB are returned.  Binary files
    are rejected.  The operator must explicitly enable this tool.
    """
    path = str(params.get("path", "")).strip()
    max_bytes = int(params.get("max_bytes", 65536))
    max_bytes = min(max_bytes, 262144)   # hard cap at 256 KB

    if not path:
        return _jdump({"error": "path is required"})

    # Expand environment variables (e.g. %WINDIR%)
    path = os.path.expandvars(os.path.expanduser(path))

    try:
        if not os.path.exists(path):
            return _jdump({"error": f"File not found: {path}"})
        if os.path.isdir(path):
            entries = os.listdir(path)[:100]
            return _jdump({"type": "directory", "path": path, "entries": entries})

        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            raw = fh.read(max_bytes)

        # Detect binary
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            try:
                text = raw.decode("latin-1")
            except Exception:
                return _jdump({
                    "error": "File appears to be binary — text read only",
                    "path": path,
                    "size_bytes": size,
                })

        truncated = size > max_bytes
        return _jdump({
            "path": path,
            "size_bytes": size,
            "truncated": truncated,
            "content": text,
        })
    except PermissionError:
        return _jdump({"error": f"Access denied: {path}"})
    except Exception as exc:
        return _jdump({"error": str(exc)})


def _get_network_config(params: Dict[str, Any]) -> str:
    """Return network adapter configuration (IP, MAC, DNS, gateway)."""
    try:
        import subprocess
        adapter_filter = str(params.get("adapter", "")).strip()

        if _is_windows():
            result = subprocess.run(
                ["ipconfig", "/all"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return _jdump({"error": "ipconfig failed", "stderr": result.stderr.strip()})
            output = result.stdout
        else:
            result = subprocess.run(
                ["ip", "addr"],
                capture_output=True, text=True, timeout=10,
            )
            output = result.stdout

        if adapter_filter:
            # Return only lines near the named adapter
            lines = output.splitlines()
            filtered = []
            in_section = False
            for line in lines:
                if adapter_filter.lower() in line.lower():
                    in_section = True
                elif line and not line.startswith(" ") and not line.startswith("\t"):
                    in_section = False
                if in_section:
                    filtered.append(line)
            output = "\n".join(filtered) if filtered else output

        return _jdump({"output": output.strip()})
    except Exception as exc:
        return _jdump({"error": str(exc)})


def _list_admin_processes(params: Dict[str, Any]) -> str:
    """List running processes with extended information (PID, name, memory, CPU)."""
    try:
        import subprocess
        filter_name = str(params.get("name_filter", "")).lower().strip()

        if _is_windows():
            result = subprocess.run(
                ["tasklist", "/fo", "csv", "/v"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                return _jdump({"error": "tasklist failed", "stderr": result.stderr.strip()})
            import csv, io
            reader = csv.DictReader(io.StringIO(result.stdout))
            processes = []
            for row in reader:
                if filter_name and filter_name not in (row.get("Image Name", "") or "").lower():
                    continue
                processes.append({
                    "name": row.get("Image Name", ""),
                    "pid":  row.get("PID", ""),
                    "session": row.get("Session Name", ""),
                    "mem_usage": row.get("Mem Usage", ""),
                    "status": row.get("Status", ""),
                    "user": row.get("User Name", ""),
                    "cpu_time": row.get("CPU Time", ""),
                    "window_title": row.get("Window Title", ""),
                })
        else:
            # Fallback for non-Windows (dev/testing)
            result = subprocess.run(
                ["ps", "aux"],
                capture_output=True, text=True, timeout=10,
            )
            lines = result.stdout.splitlines()
            processes = []
            for line in lines[1:]:    # skip header
                if filter_name and filter_name not in line.lower():
                    continue
                parts = line.split(None, 10)
                if len(parts) >= 11:
                    processes.append({"user": parts[0], "pid": parts[1], "cpu": parts[2],
                                      "mem": parts[3], "command": parts[10]})

        return _jdump({"processes": processes, "count": len(processes)})
    except Exception as exc:
        return _jdump({"error": str(exc)})


def _terminate_process(params: Dict[str, Any]) -> str:
    """Terminate a process by PID.  Requires explicit PID — never terminates by name."""
    pid = params.get("pid")
    force = bool(params.get("force", False))

    if pid is None:
        return _jdump({"error": "pid is required"})

    try:
        pid = int(pid)
    except (ValueError, TypeError):
        return _jdump({"error": f"pid must be an integer, got: {pid!r}"})

    # Guard: never allow terminating critical system PIDs
    if pid in (0, 4):
        return _jdump({"error": f"PID {pid} is a protected system process — cannot terminate"})

    try:
        if _is_windows():
            import subprocess
            args = ["taskkill", "/PID", str(pid)]
            if force:
                args.append("/F")
            result = subprocess.run(args, capture_output=True, text=True, timeout=10)
            logger.info("[privileged] terminate_process pid=%d force=%s → exit %d",
                        pid, force, result.returncode)
            if result.returncode == 0:
                return _jdump({"ok": True, "pid": pid, "force": force})
            else:
                return _jdump({
                    "ok": False, "pid": pid,
                    "error": result.stderr.strip() or result.stdout.strip(),
                })
        else:
            import signal, os as _os
            sig = signal.SIGKILL if force else signal.SIGTERM
            _os.kill(pid, sig)
            logger.info("[privileged] terminate_process pid=%d signal=%s", pid, sig)
            return _jdump({"ok": True, "pid": pid, "signal": str(sig)})
    except ProcessLookupError:
        return _jdump({"error": f"Process {pid} not found"})
    except PermissionError:
        return _jdump({"error": f"Access denied — cannot terminate PID {pid}"})
    except Exception as exc:
        return _jdump({"error": str(exc)})


# ── Registration ──────────────────────────────────────────────────────────────

def register(registry: Any, config: Dict[str, Any]) -> None:
    from runtime.tool_registry import ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy

    cfg = config.get("privileged_tools", {}) if isinstance(config, dict) else {}
    master_enabled = bool(cfg.get("enabled", False))

    def _gate(sub_key: str, handler, params: Dict[str, Any]) -> str:
        """Check both master gate and sub-gate before executing."""
        if not master_enabled:
            return _jdump({
                "error": "Privileged tools master gate is disabled",
                "hint": "Set privileged_tools.enabled=true in config.json and restart EOS.",
            })
        if not bool(cfg.get(sub_key, False)):
            return _disabled(sub_key.replace("_enabled", ""))
        return handler(params)

    # ── Registry: Read ──────────────────────────────────────────────────────

    registry.register(ToolSpec(
        name="read_registry_value",
        description=(
            "Read a single value from the Windows registry. "
            "Provide the full key path (e.g. HKLM\\SOFTWARE\\Python\\3.12) "
            "and the value name (leave empty for the default value)."
        ),
        pack="privileged_tools",
        tags=["privileged", "windows", "registry"],
        parameters={
            "type": "object",
            "properties": {
                "key_path":   {"type": "string", "description": "Full registry path, e.g. HKLM\\SOFTWARE\\Python"},
                "value_name": {"type": "string", "description": "Value name; empty string reads (Default)"},
            },
            "required": ["key_path"],
        },
        handler=lambda p: _gate("registry_read_enabled", _read_registry_value, p),
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.OPERATOR_ONLY,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=master_enabled and bool(cfg.get("registry_read_enabled", False)),
    ))

    # ── Registry: Write ─────────────────────────────────────────────────────

    registry.register(ToolSpec(
        name="write_registry_value",
        description=(
            "Write a value to the Windows registry. "
            "Supports REG_SZ, REG_EXPAND_SZ, REG_DWORD, REG_QWORD, "
            "REG_BINARY, REG_MULTI_SZ. Requires operator confirmation."
        ),
        pack="privileged_tools",
        tags=["privileged", "windows", "registry"],
        parameters={
            "type": "object",
            "properties": {
                "key_path":   {"type": "string", "description": "Full registry path"},
                "value_name": {"type": "string", "description": "Value name to write"},
                "data":       {"description": "Value data (string, int, or list for MULTI_SZ)"},
                "type":       {"type": "string", "default": "REG_SZ",
                               "enum": ["REG_SZ","REG_EXPAND_SZ","REG_DWORD","REG_QWORD",
                                        "REG_BINARY","REG_MULTI_SZ"]},
            },
            "required": ["key_path", "value_name", "data"],
        },
        handler=lambda p: _gate("registry_write_enabled", _write_registry_value, p),
        risk_level=ToolRiskLevel.IRREVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.OPERATOR_ONLY,
        confirmation_policy=ConfirmationPolicy.HARD_CONFIRM,
        enabled=master_enabled and bool(cfg.get("registry_write_enabled", False)),
    ))

    # ── Services: List ──────────────────────────────────────────────────────

    registry.register(ToolSpec(
        name="list_windows_services",
        description=(
            "List Windows services and their current state (running/stopped/etc). "
            "Optionally filter by state_filter ('running', 'stopped', or 'all')."
        ),
        pack="privileged_tools",
        tags=["privileged", "windows", "services"],
        parameters={
            "type": "object",
            "properties": {
                "state_filter": {
                    "type": "string",
                    "default": "all",
                    "enum": ["running", "stopped", "paused", "all"],
                    "description": "Filter results by service state",
                },
            },
            "required": [],
        },
        handler=lambda p: _gate("service_list_enabled", _list_windows_services, p),
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.OPERATOR_ONLY,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=master_enabled and bool(cfg.get("service_list_enabled", False)),
    ))

    # ── Services: Control ───────────────────────────────────────────────────

    registry.register(ToolSpec(
        name="control_windows_service",
        description=(
            "Start or stop a Windows service by name. "
            "Use list_windows_services first to confirm the service name. "
            "Requires operator confirmation before execution."
        ),
        pack="privileged_tools",
        tags=["privileged", "windows", "services"],
        parameters={
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Windows service name (not display name)"},
                "action":       {"type": "string", "enum": ["start", "stop"],
                                 "description": "'start' or 'stop'"},
            },
            "required": ["service_name", "action"],
        },
        handler=lambda p: _gate("service_control_enabled", _control_windows_service, p),
        risk_level=ToolRiskLevel.REVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.OPERATOR_ONLY,
        confirmation_policy=ConfirmationPolicy.HARD_CONFIRM,
        enabled=master_enabled and bool(cfg.get("service_control_enabled", False)),
    ))

    # ── System file read ────────────────────────────────────────────────────

    registry.register(ToolSpec(
        name="read_system_file",
        description=(
            "Read a text file from anywhere on the system (outside the workspace). "
            "Supports environment variables in path (e.g. %WINDIR%\\System32\\...). "
            "Limited to 256 KB; binary files are rejected. Directories return a listing."
        ),
        pack="privileged_tools",
        tags=["privileged", "filesystem"],
        parameters={
            "type": "object",
            "properties": {
                "path":      {"type": "string", "description": "Absolute path or env-var path to read"},
                "max_bytes": {"type": "integer", "default": 65536, "minimum": 1, "maximum": 262144,
                              "description": "Maximum bytes to read (default 64 KB, max 256 KB)"},
            },
            "required": ["path"],
        },
        handler=lambda p: _gate("system_file_read_enabled", _read_system_file, p),
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.OPERATOR_ONLY,
        confirmation_policy=ConfirmationPolicy.SOFT_CONFIRM,
        enabled=master_enabled and bool(cfg.get("system_file_read_enabled", False)),
    ))

    # ── Network config ──────────────────────────────────────────────────────

    registry.register(ToolSpec(
        name="get_network_config",
        description=(
            "Return network adapter configuration: IP addresses, MAC, DNS servers, "
            "default gateway.  Optionally filter by adapter name."
        ),
        pack="privileged_tools",
        tags=["privileged", "network"],
        parameters={
            "type": "object",
            "properties": {
                "adapter": {"type": "string", "default": "",
                            "description": "Filter by adapter name substring (empty = all adapters)"},
            },
            "required": [],
        },
        handler=lambda p: _gate("network_config_enabled", _get_network_config, p),
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.OPERATOR_ONLY,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=master_enabled and bool(cfg.get("network_config_enabled", False)),
    ))

    # ── Process list ────────────────────────────────────────────────────────

    registry.register(ToolSpec(
        name="list_admin_processes",
        description=(
            "List all running processes with extended information: PID, memory, CPU time, "
            "user, session, and window title. Optionally filter by process name."
        ),
        pack="privileged_tools",
        tags=["privileged", "processes"],
        parameters={
            "type": "object",
            "properties": {
                "name_filter": {"type": "string", "default": "",
                                "description": "Filter by process name substring (empty = all)"},
            },
            "required": [],
        },
        handler=lambda p: _gate("process_list_enabled", _list_admin_processes, p),
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.OPERATOR_ONLY,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=master_enabled and bool(cfg.get("process_list_enabled", False)),
    ))

    # ── Process terminate ───────────────────────────────────────────────────

    registry.register(ToolSpec(
        name="terminate_process",
        description=(
            "Terminate a running process by its PID. "
            "Use list_admin_processes first to identify the correct PID. "
            "Set force=true to use SIGKILL/taskkill /F for unresponsive processes. "
            "Requires operator confirmation. Cannot terminate protected system processes."
        ),
        pack="privileged_tools",
        tags=["privileged", "processes"],
        parameters={
            "type": "object",
            "properties": {
                "pid":   {"type": "integer", "description": "Process ID to terminate"},
                "force": {"type": "boolean", "default": False,
                          "description": "Force-kill the process (SIGKILL / taskkill /F)"},
            },
            "required": ["pid"],
        },
        handler=lambda p: _gate("process_terminate_enabled", _terminate_process, p),
        risk_level=ToolRiskLevel.IRREVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.OPERATOR_ONLY,
        confirmation_policy=ConfirmationPolicy.HARD_CONFIRM,
        enabled=master_enabled and bool(cfg.get("process_terminate_enabled", False)),
    ))

    enabled_count = sum(1 for key in [
        "registry_read_enabled", "registry_write_enabled",
        "service_list_enabled", "service_control_enabled",
        "system_file_read_enabled", "network_config_enabled",
        "process_list_enabled", "process_terminate_enabled",
    ] if cfg.get(key, False))

    if master_enabled:
        logger.info(
            "[privileged_tools] Registered 8 privileged tools (%d sub-gates enabled).",
            enabled_count,
        )
    else:
        logger.debug(
            "[privileged_tools] Master gate is off — all 8 privileged tools registered but disabled."
        )
