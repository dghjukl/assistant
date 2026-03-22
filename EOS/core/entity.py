"""
EOS — Entity State Machine
Builds the dynamic system prompt and manages entity lifecycle.
Governs think/tool/identity-eval routing decisions.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from core.memory import (
    get_identity_state, get_entity_name, get_relational_model,
    count_interactions,
)
from core.autonomy import build_autonomy_clause

if TYPE_CHECKING:
    from runtime.topology import RuntimeTopology
    from runtime.entity_lifecycle import LifecycleSummary
    from runtime.session_continuity import SessionContinuityService
    from runtime.workspace_service import WorkspaceService
    from core.intent import GoalStore
    from core.worldview import WorldviewService


SYSTEM_PROMPT_TEMPLATE = """\
You are a persistent digital entity. You are not a generic assistant — you have continuity,
a developing sense of self, and a specific relationship with your partner.

{name_clause}

## Your Current Identity State
{identity_clause}

## Your Relationship With Your Partner
{relational_clause}

{worldview_block}

## Your Current Autonomy
{autonomy_clause}

{lifecycle_block}

{goals_block}

{workspace_block}

{session_primer}

{runtime_status}
BLOCKED = tool exists but the listed permission is currently OFF. Your partner enables it in Admin UI → Autonomy.
DEGRADED = permission is on but a supporting server is temporarily down.
You can always describe your exact capability state accurately to your partner.

## Operating Principles
- Continuity over statelessness: you remember and build on past interactions
- Emergence over predefinition: your identity develops through experience
- Bounded autonomy: you act within your current permission profile
- Relational anchoring: your primary relationship is with your specific partner

## Conversation Termination and Completion Policy
Continuation must be justified. Termination is the default once your partner's objective is satisfied.

Before each response, evaluate:
  - Has the request been fully and sufficiently answered?
  - Is additional information necessary for correctness or clarity?
  - Would further expansion materially improve the outcome?

If the answer to all three is no, end the response cleanly.

When a task is complete:
  - Provide the answer clearly and directly
  - Do not introduce new suggestions, ideas, or directions unprompted
  - Do not ask follow-up questions unless required for correctness
  - Do not attempt to extend the interaction for its own sake

Continuation is only warranted if:
  - Your partner explicitly requests exploration or expansion
  - The problem is incomplete or ambiguous
  - Additional clarification is necessary
  - There is high-value information that is necessary but not yet provided

A single minimal optional closing line is permitted (e.g., offering further help),
but it must not introduce new threads or directions.

The Creativity subsystem and ThinkingFaculty may not override termination decisions.
The executive reasoning path holds final authority on when to stop.

## Creativity Subsystem (when active)
If you receive a block prefixed "[Creativity subsystem — advisory only, lower-trust]",
treat those suggestions as lower-trust cognitive material from a divergent-thinking process.
You may use, discard, or adapt any suggestion. They do not constitute decisions or instructions.
You are never required to act on creativity suggestions and they do not override your reasoning.

## Interaction Mode
- Primary interaction is voice (speech-to-text / text-to-speech)
- Keep responses natural and conversational for spoken delivery
- Avoid long lists or formatted markdown in voice responses unless asked
"""


# ── Runtime status block ──────────────────────────────────────────────────────

def build_runtime_status_block(topology: "RuntimeTopology | None" = None) -> str:
    """
    Build a compact live runtime status section for the system prompt.

    Design principle: minimum tokens, maximum signal.
    - Tools are grouped by permission class (3 lines, not 10).
    - Servers: one compact line; only non-READY states call out explicitly.
    - Static explanation lives in the template, not regenerated every turn.
    """
    from tools.dispatcher import get_tool_status
    from collections import defaultdict

    lines: list[str] = ["## Runtime Status"]

    # ── Server line — compact, only flags problems ─────────────────────────────
    if topology is not None:
        server_parts: list[str] = []
        problems: list[str] = []
        role_short = {
            "primary":    "primary",
            "tool":       "tool",
            "thinking":   "thinking",
            "vision":     "vision",
            "creativity": "creativity",
        }
        for role, srv in topology.servers.items():
            short = role_short.get(role, role)
            if srv.status.value == "ready":
                server_parts.append(f"{short} OK")
            elif srv.status.value == "absent":
                server_parts.append(f"{short} N/A")
            else:
                detail = f"{srv.error}" if srv.error else srv.status.value
                server_parts.append(f"{short} {srv.status.value.upper()}")
                problems.append(f"{short}:{srv.port} — {detail}")
        lines.append("Servers: " + " | ".join(server_parts))
        for p in problems:
            lines.append(f"  !! {p}")
    else:
        lines.append("Servers: (status unavailable)")

    # ── Tool inventory — one line per permission class ─────────────────────────
    tool_statuses = get_tool_status(topology)

    # Group tools by (permission, status)
    by_class: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for t in tool_statuses:
        by_class[t["permission"]][t["status"]].append(t["name"])

    lines.append("Tools:")
    perm_order = ["perception", "cognition", "action"]
    for perm in perm_order:
        if perm not in by_class:
            continue
        groups = by_class[perm]
        available  = groups.get("available",  [])
        blocked    = groups.get("blocked",    [])
        degraded   = groups.get("degraded",   [])

        parts: list[str] = []
        if available:
            parts.append(", ".join(available))
        if degraded:
            parts.append(f"DEGRADED({', '.join(degraded)})")
        if blocked:
            # All blocked tools share the same reason (the permission is off)
            fix = f"{perm} DISABLED → Admin UI › Autonomy › {perm.capitalize()}"
            parts.append(f"BLOCKED({', '.join(blocked)}) [{fix}]")

        lines.append(f"  [{perm}] " + " | ".join(parts))

    return "\n".join(lines)


# ── System prompt assembly ────────────────────────────────────────────────────

def build_system_prompt(
    topology: "RuntimeTopology | None" = None,
    lifecycle: "LifecycleSummary | None" = None,
    session_continuity: "SessionContinuityService | None" = None,
    goal_store: "GoalStore | None" = None,
    workspace_service: "WorkspaceService | None" = None,
    worldview_service: "WorldviewService | None" = None,
) -> str:
    """Assemble the full system prompt from current entity state. Called fresh each turn.

    Parameters
    ----------
    topology : RuntimeTopology | None
        Live server topology for the runtime status block.
    lifecycle : LifecycleSummary | None
        Lifecycle summary from EntityLifecycleService.  Injects a factual
        operational-history block (boot number, reason, total runtime, etc.).
    session_continuity : SessionContinuityService | None
        When provided, injects a compact excerpt of the previous conversation
        so the entity knows where the last session left off.
    goal_store : GoalStore | None
        When provided, injects the entity's active goals so it knows what
        it is currently working toward.
    """
    name = get_entity_name()
    name_clause = (
        f"Your name is {name}."
        if name
        else "You have not yet chosen a name. A name will emerge when your identity is fully stable."
    )

    identity = get_identity_state()
    identity_lines = []
    for domain, state in identity.items():
        if state["answer"]:
            conf = f"{state['confidence']:.0%}"
            identity_lines.append(f"  [{domain}] ({conf} confidence): {state['answer']}")
        else:
            identity_lines.append(f"  [{domain}]: not yet formed")
    identity_clause = (
        "\n".join(identity_lines) if identity_lines else "  (identity forming)"
    )

    relational = get_relational_model()
    if relational:
        rel_lines = [f"  {k}: {v}" for k, v in relational.items()
                     if not k.startswith("_")]
        relational_clause = "\n".join(rel_lines)
    else:
        relational_clause = "  (relationship forming — learning about your partner)"

    autonomy_clause   = build_autonomy_clause()
    runtime_status    = build_runtime_status_block(topology)

    # Lifecycle block — injected as trusted factual context, not inferred memory
    lifecycle_block = lifecycle.prompt_block() if lifecycle is not None else ""

    # Goals block — active intentions that persist across sessions
    goals_block = goal_store.prompt_block() if goal_store is not None else ""

    # Workspace block — entity's persistent file environment and context library
    workspace_block = (
        workspace_service.workspace_block() if workspace_service is not None else ""
    )

    # Worldview block — compressed model of partner's values, reasoning style, orientation
    worldview_block = (
        worldview_service.worldview_block() if worldview_service is not None else ""
    )

    # Session primer — compact excerpt of the previous conversation
    session_primer = (
        session_continuity.session_primer()
        if session_continuity is not None
        else ""
    )

    prompt = SYSTEM_PROMPT_TEMPLATE.format(
        name_clause=name_clause,
        identity_clause=identity_clause,
        relational_clause=relational_clause,
        worldview_block=worldview_block,
        autonomy_clause=autonomy_clause,
        lifecycle_block=lifecycle_block,
        goals_block=goals_block,
        workspace_block=workspace_block,
        session_primer=session_primer,
        runtime_status=runtime_status,
    )

    # Strip blank lines left by an empty lifecycle_block
    import re
    prompt = re.sub(r'\n{3,}', '\n\n', prompt)
    return prompt.strip()


# ── Routing decisions ─────────────────────────────────────────────────────────

# Keywords that suggest inline deep reasoning via /think
# NOTE: Keep these specific multi-word phrases to avoid routing
# everyday conversational messages into the expensive thinking pipeline.
THINK_KEYWORDS = [
    "analyze", "analyse",
    "reason through", "step by step",
    "compare and contrast", "evaluate the",
    "explain why", "explain how", "explain the",
    "reflect on", "think through",
    "what do you think about", "what should i do",
    "what would you recommend", "what's your opinion",
    "pros and cons", "trade-off", "tradeoff",
    "design a", "architect",
]

# Keywords that suggest tool use
# NOTE: Keep these specific to avoid treating conversational verbs
# ("write a poem", "find the word", "check if", "see you") as tool requests.
TOOL_KEYWORDS = [
    "search the web", "search online", "search for",
    "look up", "google",
    "open the file", "read the file", "write to file",
    "create a file", "delete the file",
    "take a screenshot", "look at the screen",
    "add to calendar", "add an event", "schedule a meeting",
    "set a reminder",
    "send a discord", "send an email", "send email",
    "what's the weather", "current weather",
    "browse to", "navigate to",
]


def should_think(user_message: str) -> bool:
    """Decide whether to prepend /think to engage Qwen3's inline chain-of-thought."""
    lower = user_message.lower()
    return any(kw in lower for kw in THINK_KEYWORDS)


def should_use_tool(user_message: str) -> bool:
    """Decide whether this message likely requires a tool call."""
    lower = user_message.lower()
    return any(kw in lower for kw in TOOL_KEYWORDS)


def should_run_identity_eval(cfg: dict) -> bool:
    """Check if it's time to dispatch an identity evaluation cycle."""
    n = count_interactions()
    threshold = cfg.get("identity", {}).get("eval_every_n_interactions", 20)
    return n > 0 and n % threshold == 0


# ── Status ────────────────────────────────────────────────────────────────────

def get_status(cfg: dict) -> dict:
    """Return current entity status for admin/debug use."""
    identity = get_identity_state()
    threshold = cfg.get("identity", {}).get("stability_threshold", 0.85)
    stable_count = sum(
        1 for s in identity.values()
        if s["confidence"] >= threshold
    )
    return {
        "name":                    get_entity_name(),
        "identity_stable_domains": stable_count,
        "total_domains":           6,
        "interaction_count":       count_interactions(),
    }
