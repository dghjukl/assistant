"""
EOS — Tool Dispatcher
Qwen3 expresses tool intent in natural language.
The Tool Server (LFM2-1.2B-Tool, port 8082) extracts it to structured JSON.
This module executes the resulting tool call.

Autonomy gate: action-class tools require can("action") == True.
Perception-class tools require can("perception") == True.
Cognition-class tools require can("cognition") == True.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import httpx

from core.autonomy import can

if TYPE_CHECKING:
    from runtime.topology import RuntimeTopology

logger = logging.getLogger("eos.tools")


# ── Tool registry ─────────────────────────────────────────────────────────────
# Single source of truth: every tool's name, permission class, description, args.
# Used by dispatcher (gating + extraction schema) and entity.py (system prompt).

TOOL_REGISTRY: dict[str, dict] = {
    "web_search": {
        "permission":  "perception",
        "description": "search the web for current information or look up a topic",
        "args":        {"query": "str"},
    },
    "read_file": {
        "permission":  "perception",
        "description": "read the contents of a file by path",
        "args":        {"path": "str"},
    },
    "list_dir": {
        "permission":  "perception",
        "description": "list the files in a directory",
        "args":        {"path": "str"},
    },
    "screen_capture": {
        "permission":  "perception",
        "description": "capture the screen and describe what is visible",
        "args":        {"prompt": "str?"},
    },
    "webcam_capture": {
        "permission":  "perception",
        "description": "capture from the webcam and describe what is visible",
        "args":        {"prompt": "str?"},
    },
    "query_memory": {
        "permission":  "cognition",
        "description": "search your long-term memory for relevant past context",
        "args":        {"question": "str"},
    },
    "save_memory": {
        "permission":  "cognition",
        "description": "save something important to long-term memory",
        "args":        {"text": "str"},
    },
    "list_events": {
        "permission":  "action",
        "description": "list upcoming calendar events",
        "args":        {"days_ahead": "int?"},
    },
    "write_file": {
        "permission":  "action",
        "description": "write or create a file at a given path",
        "args":        {"path": "str", "content": "str"},
    },
    "create_event": {
        "permission":  "action",
        "description": "create a calendar event",
        "args":        {
            "title": "str",
            "start_iso": "str",
            "duration_minutes": "int?",
            "description": "str?",
        },
    },
    "send_discord": {
        "permission":  "action",
        "description": "send a Discord message to a channel",
        "args":        {"content": "str", "channel_id": "int?"},
    },
}

# Extraction schema for the Tool Server (args only, as before)
TOOL_SCHEMA = {name: entry["args"] for name, entry in TOOL_REGISTRY.items()}
SCHEMA_DOC  = json.dumps(TOOL_SCHEMA, indent=2)


# ── Permission gate helper ────────────────────────────────────────────────────

def _permission_error(tool: str) -> str:
    """
    Return a rich error string when a tool is permission-blocked.
    The model can read this and accurately explain the situation and fix to the partner.
    """
    perm = TOOL_REGISTRY.get(tool, {}).get("permission", "unknown")
    return (
        f"[Tool '{tool}' requires '{perm}' permission, which is currently DISABLED. "
        f"To enable it: Admin UI → Autonomy → {perm.capitalize()} → turn ON. "
        f"Or ask your partner to enable it.]"
    )


# ── Tool availability status (for system prompt injection) ────────────────────

def get_tool_status(topology: "RuntimeTopology | None" = None) -> list[dict]:
    """
    Return a list of tool status dicts for every tool in the registry.
    Each dict: {name, permission, description, available, reason}
    Used by core/entity.py to build the runtime status block.
    """
    from runtime.on_demand import get_on_demand_manager
    manager = get_on_demand_manager()
    if manager is not None:
        tool_server_ready = True  # on-demand: will start when needed
    elif topology is not None:
        tool_server_ready = topology.tool_endpoint() is not None
    else:
        tool_server_ready = True

    result = []
    for name, entry in TOOL_REGISTRY.items():
        perm = entry["permission"]
        perm_ok = can(perm)
        if perm_ok:
            if not tool_server_ready and name not in ("query_memory", "save_memory"):
                # Tool server down means extraction is broken for most tools.
                # Memory tools are internal — they don't go through tool extraction.
                status = "degraded"
                reason = "tool extraction server offline"
            else:
                status = "available"
                reason = ""
        else:
            status = "blocked"
            reason = f"{perm} permission is DISABLED"
        result.append({
            "name":        name,
            "permission":  perm,
            "description": entry["description"],
            "status":      status,   # "available" | "blocked" | "degraded"
            "reason":      reason,
        })
    return result


# ── Tool extraction ───────────────────────────────────────────────────────────

async def extract_tool_call(
    tool_intent: str,
    topology: "RuntimeTopology",
    available_tools: dict[str, dict] | None = None,
    environment_context: str = "",
) -> dict | None:
    """
    Send Qwen3's natural language tool intent to the Tool Server.
    Returns structured {tool, args} dict or None if extraction fails or no tool needed.
    Falls back to None (no tool) if the tool server is unavailable.
    """
    from runtime.on_demand import get_on_demand_manager
    manager = get_on_demand_manager()
    if manager is not None:
        endpoint = await manager.ensure("tool")
    else:
        endpoint = topology.tool_endpoint()
    if not endpoint:
        logger.debug("Tool server unavailable — skipping extraction")
        return None

    schema_doc = json.dumps(available_tools or TOOL_SCHEMA, indent=2)

    environment_block = f"Current environment model:\n{environment_context}\n\n" if environment_context else ""
    system = (
        "You are a tool-call extractor. Given a natural language description of an action, "
        "output a JSON object with 'tool' (tool name) and 'args' (argument dict).\n"
        f"{environment_block}"
        f"Available tools and their argument schemas:\n{schema_doc}\n"
        "Choose tools that match the known environment surfaces and connected services. "
        "Do not route to desktop/browser/calendar/discord actions unless the environment model shows that surface exists or is reachable.\n"
        "If no tool applies, output: {\"tool\": null, \"args\": {}}\n"
        "Output only valid JSON. No explanation. No markdown fences."
    )

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(
                f"{endpoint}/v1/chat/completions",
                json={
                    "model": "lfm2-tool",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": tool_intent},
                    ],
                    "temperature": 0.0,
                    "max_tokens":  256,
                },
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip()

            # Strip markdown fences if present
            if "```" in raw:
                raw = raw.split("```")[1].lstrip("json\n").strip()
            return json.loads(raw)

        except json.JSONDecodeError:
            logger.debug("Tool extraction returned non-JSON: %s", raw[:100])
            return None
        except Exception as exc:
            logger.debug("Tool extraction error: %s", exc)
            return None


# ── Tool execution ─────────────────────────────────────────────────────────────

async def execute(
    tool: str,
    args: dict,
    topology: "RuntimeTopology",
    cfg: dict,
) -> str:
    """Execute a tool by name with given args. Returns string result."""

    # Autonomy gates — check permission first, return rich diagnostic if blocked
    entry = TOOL_REGISTRY.get(tool)
    if entry:
        perm = entry["permission"]
        if not can(perm):
            return _permission_error(tool)

    try:
        if tool == "web_search":
            from tools.web_search import search
            return await search(args.get("query", ""))

        elif tool == "read_file":
            from tools.file_ops import read_file
            return read_file(args.get("path", ""), cfg)

        elif tool == "write_file":
            from tools.file_ops import write_file
            return write_file(args.get("path", ""), args.get("content", ""), cfg)

        elif tool == "list_dir":
            from tools.file_ops import list_dir
            return list_dir(args.get("path", "."), cfg)

        elif tool == "screen_capture":
            from tools.screen_capture import get_screen_description
            return await get_screen_description(args.get("prompt"), topology)

        elif tool == "webcam_capture":
            from tools.webcam_capture import get_webcam_description
            return await get_webcam_description(args.get("prompt"), topology)

        elif tool == "query_memory":
            from tools.memory_query import query_memory
            return await query_memory(args.get("question", ""))

        elif tool == "save_memory":
            from tools.memory_query import save_memory
            return await save_memory(args.get("text", ""))

        elif tool == "list_events":
            from tools.calendar import list_events
            return await list_events(args.get("days_ahead", 7), cfg)

        elif tool == "create_event":
            from tools.calendar import create_event
            return await create_event(
                args.get("title", ""),
                args.get("start_iso", ""),
                args.get("duration_minutes", 60),
                args.get("description", ""),
                cfg,
            )

        elif tool == "send_discord":
            from tools.discord_send import send_message
            return await send_message(args.get("content", ""), args.get("channel_id"))

        else:
            return f"[Unknown tool: '{tool}' — not in the tool registry]"

    except Exception as exc:
        logger.error("Tool '%s' failed: %s", tool, exc)
        return f"[Tool '{tool}' failed during execution: {exc}]"


async def run_tool_intent(
    tool_intent: str,
    topology: "RuntimeTopology",
    cfg: dict,
) -> str | None:
    """Full pipeline: intent string → extraction → execution → result string."""
    call = await extract_tool_call(tool_intent, topology)
    if not call or not call.get("tool"):
        return None
    return await execute(call["tool"], call.get("args", {}), topology, cfg)
