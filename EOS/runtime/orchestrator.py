"""
EOS — Main Orchestrator  (QWEN Executive Layer)
================================================
Cognitive loop: STT → memory recall → Qwen3 → tool dispatch → TTS.
Background thread: identity eval, initiative, signal bus.

This module is the QWEN executive layer.  It owns the ThinkingFaculty — the
only path to the background thinking worker (port 8083).  No other module may
instantiate ThinkingFaculty directly.

Epistemic decision policy
-------------------------
Before generating a response, process_turn() classifies every request into one
of four EpistemicMode values:

  DIRECT_ANSWER        — high-confidence, no external data or deep reasoning needed
  INTERNAL_ESCALATION  — requires deeper reasoning → invoke ThinkingFaculty
  TOOL_OR_EXTERNAL     — requires real-world data → tool pipeline
  DEFERRAL             — beyond reliable capability → honest statement + next step

Hallucinated or low-confidence answers are failures.
Explicit deferral is correct behaviour when appropriate.

Thinking faculty authority
--------------------------
Only QWEN (this module) may invoke ThinkingFaculty.deliberate().
Background subsystems (initiative, investigation, reflection) request thinking
via think_for_background(), which is QWEN's public delegation interface.
QWEN decides whether and how to route each request through the faculty.

Qwen3 reasoning modes (distinct — see RUNTIME_INVARIANTS):
  /think     — Qwen3 reasons inline with itself, same request, blocking
  /no_think  — Qwen3 responds directly, fast
  ThinkingFaculty.deliberate() → port 8083 (LFM2.5-Thinking), non-blocking

Creativity subsystem
--------------------
The Creativity server (port 8084) is a first-class but optional cognitive
service that generates divergent suggestions, alternate framings, and
non-obvious solution paths.  It is advisory only:

  - May PROPOSE, REINTERPRET, EXPAND, DIVERSIFY.
  - May NOT DECIDE, AUTHORIZE, EXECUTE TOOLS, or OVERRIDE grounded reasoning.
  - Termination decisions are never subject to creativity override.

Invocation is conditional on:
  1. cfg["creativity"]["enabled"] is true
  2. The creativity server is currently reachable
  3. The invocation domain is enabled in cfg["creativity"]["invocation_domains"]
  4. Injection frequency sampling passes

If the server is absent or the call fails, execution continues normally.
The only observable difference is reduced divergence in suggestions.

Conversation termination and completion policy
----------------------------------------------
Continuation must be justified.  Termination is the default once the user's
objective is satisfied.  Before each final response, process_turn() evaluates
the interaction mode:

  COMPLETION_MODE   — task is complete; answer and stop
  ASSIST_MODE       — answer + minimal optional closing line
  EXPLORATORY_MODE  — extended reasoning or idea expansion (user-requested)

Default mode is COMPLETION_MODE unless continuation is explicitly warranted.
The Creativity subsystem and ThinkingFaculty may not override this decision.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid as _uuid_mod
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

import httpx

from core.attention_preferences import record_turn_attention
from core.entity  import build_system_prompt, should_think, should_use_tool, should_run_identity_eval, should_run_relational_eval
from core.memory  import init_db, log_interaction, get_recent_interactions, search_memory, store_memory
from core.identity import run_evaluation_cycle, request_self_name
from core.memory   import get_entity_name, get_relational_model
from core.worldview import build_worldview_extraction_prompt
from runtime.capability_registry import CapabilityStatus
from runtime.exception_observability import observe_exception
from runtime.topology import RuntimeTopology
from runtime.thinking_faculty import ThinkingFaculty, ThinkingArtifact
from runtime.creativity_service import (
    CreativityService, CreativityArtifact,
    DOMAIN_REASONING, DOMAIN_EXPLANATION, DOMAIN_BRAINSTORM, DOMAIN_STUCK,
    init_creativity_service, get_creativity_service,
)

logger = logging.getLogger("eos.orchestrator")


# ── External inference — outcome classification ────────────────────────────────

from runtime.external_inference_policy import (
    SEVERITY_HARD_FAIL, SEVERITY_FAILED, SEVERITY_DEGRADED, SEVERITY_SUCCESS,
    escalation_allows,
)

# Response prefixes that identify a hard-failure string returned by call_qwen3
_LOCAL_HARD_FAIL_PREFIXES = (
    "I can't reach my brain right now.",
    "[Error communicating with primary model:",
)


def _classify_local_outcome(response: str, primary_endpoint_available: bool) -> str:
    """
    Classify the quality of a local inference response into a severity string.

    Returns one of SEVERITY_HARD_FAIL / SEVERITY_FAILED / SEVERITY_DEGRADED /
    SEVERITY_SUCCESS.  Used to gate escalation-mode checks.
    """
    if not primary_endpoint_available:
        return SEVERITY_HARD_FAIL
    r = (response or "").strip()
    if not r:
        return SEVERITY_HARD_FAIL
    if any(r.startswith(p) for p in _LOCAL_HARD_FAIL_PREFIXES):
        return SEVERITY_HARD_FAIL
    # Structured error bracket that is NOT one of the well-known hard-fail prefixes
    if r.startswith("[") and len(r) < 120:
        return SEVERITY_FAILED
    # Very short response — likely degraded / empty after stripping markup
    if len(r) < 20:
        return SEVERITY_DEGRADED
    return SEVERITY_SUCCESS


def _get_ei_policy_safe():
    """Return the active ExternalInferencePolicy from app_state, or None."""
    try:
        from webui.app_state import app_state as _as
        return _as.ei_policy
    except Exception:
        return None


def _build_ei_messages(user_input: str, history: list[dict]) -> list[dict]:
    """
    Build a minimal message list for an external inference call.

    Uses the last 6 conversation turns so the external model has context,
    then appends the current user input if it is not already the last message.
    """
    recent = [m for m in history if m.get("role") in ("user", "assistant")][-6:]
    # Avoid duplicating the current user message if it is already in history
    if recent and recent[-1].get("role") == "user" and recent[-1].get("content", "").strip() == user_input.strip():
        return recent
    recent.append({"role": "user", "content": user_input})
    return recent


async def _try_ei_fallback(
    messages: list[dict],
    *,
    origin_tier: str,
    origin_ip: str,
    reason: str,
    local_outcome_severity: str,
) -> tuple[str | None, bool]:
    """
    Attempt an external inference call through the policy engine.

    Returns (response_text, used_ei):
      - response_text  — the EI response string, a pending-approval notice, or None
      - used_ei        — True only when an actual external call was made and succeeded
    """
    try:
        from webui.app_state import app_state as _as
        policy = _as.ei_policy
        if policy is None:
            return None, False

        approval_mode = policy._ei_cfg.get("approval_mode", "never")

        if approval_mode == "ask_for_paid_calls":
            # Pre-flight: would the call be allowed if approval were "always"?
            # Temporarily swap approval mode for the check only.
            from runtime.external_inference_policy import APPROVAL_ALWAYS
            policy._ei_cfg["approval_mode"] = APPROVAL_ALWAYS
            pre = policy.check(
                origin_tier=origin_tier,
                origin_ip=origin_ip,
                reason=reason,
                local_outcome_severity=local_outcome_severity,
            )
            policy._ei_cfg["approval_mode"] = approval_mode  # restore

            if not pre.allowed:
                logger.debug("[EI] Pre-check denied (%s); skipping approval queue.", pre.denial_reason)
                return None, False

            # Register the pending approval in shared state
            approval_id = _uuid_mod.uuid4().hex[:12]
            _as.ei_pending_approvals[approval_id] = {
                "messages":              messages,
                "origin_tier":           origin_tier,
                "origin_ip":             origin_ip,
                "reason":                reason,
                "local_outcome_severity": local_outcome_severity,
                "requested_at":          time.time(),
                "estimated_cost":        pre.estimated_cost,
            }
            logger.info("[EI] Pending approval registered %s (reason=%s, severity=%s)",
                        approval_id, reason, local_outcome_severity)
            return (
                f"[This response requires external inference — pending operator approval. "
                f"Approval ID: {approval_id}. "
                f"An operator can approve or reject this in the admin panel "
                f"under Ext. Inference → Pending Approvals.]",
                False,
            )

        # approval_mode == "never" or "always" — call directly
        decision, result = policy.call_external(
            messages,
            origin_tier=origin_tier,
            origin_ip=origin_ip,
            reason=reason,
            local_outcome_severity=local_outcome_severity,
        )
        if not decision.allowed or result is None or not result.ok:
            if decision.denial_reason:
                logger.debug("[EI] Denied by policy: %s", decision.denial_reason)
            return None, False

        content = (result.content or "").strip()
        if not content:
            return None, False
        return content, True

    except Exception as exc:
        logger.warning("[EI] _try_ei_fallback error: %s", exc)
        return None, False


# ── Tool executor (wired by WebUI server after ToolRegistry loads) ──────────────

_tool_executor = None  # ToolExecutor | None


def get_tool_executor():
    """Return the live ToolExecutor instance, if one has been wired."""
    return _tool_executor


def wire_executor(registry: Any, audit_store: Any = None) -> None:
    """Wire the ToolExecutor with the live registry and audit store.

    Called once by the WebUI server after the ToolRegistry has been fully
    loaded.  Must be called before any governed tool execution can occur.
    Subsequent calls replace the executor (e.g. after a registry reload).
    """
    global _tool_executor
    from runtime.tool_executor import ToolExecutor
    _tool_executor = ToolExecutor(registry=registry, audit_store=audit_store)
    tool_count = len(registry.all_tools()) if registry and hasattr(registry, "all_tools") else 0
    logger.info("[orchestrator] ToolExecutor wired with %d registered tool(s).", tool_count)


def _registry_tool_schema() -> dict[str, dict[str, Any]]:
    """Return the enabled live tool schema for extraction prompts."""
    if _tool_executor is None or getattr(_tool_executor, "registry", None) is None:
        return {}
    schema: dict[str, dict[str, Any]] = {}
    for spec in _tool_executor.registry.all_enabled():
        schema[spec.name] = spec.parameters or {"type": "object", "properties": {}, "required": []}
    return schema


def _format_tool_output(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(result)


async def _run_registry_tool_intent(
    user_input: str,
    topology: RuntimeTopology,
    *,
    entity_snapshot: Any | None = None,
) -> str | None:
    """Extract and execute a tool call via the live ToolRegistry/ToolExecutor."""
    if _tool_executor is None or getattr(_tool_executor, "registry", None) is None:
        return None

    tool_schema = _registry_tool_schema()
    if not tool_schema:
        return None

    from tools.dispatcher import extract_tool_call

    env_ctx = ""
    if entity_snapshot is not None:
        env_ctx = getattr(entity_snapshot, "environment_tool_context", "") or ""
    call = await extract_tool_call(
        user_input,
        topology,
        available_tools=tool_schema,
        environment_context=env_ctx,
    )
    if not call or not call.get("tool"):
        return None

    tool_name = str(call.get("tool") or "").strip()
    if not tool_name:
        return None

    params = call.get("args", {})
    if not isinstance(params, dict):
        params = {}

    result = _tool_executor.execute(
        tool_name,
        params,
        caller_trust="VERIFIED_USER",
    )
    if result.pending_confirmation_id:
        return (
            f"[Tool '{tool_name}' is pending operator confirmation. "
            f"confirmation_id={result.pending_confirmation_id}]"
        )
    if not result.success:
        return f"[Tool '{tool_name}' failed: {result.error}]"
    return _format_tool_output(result.output)


# ── Epistemic mode ─────────────────────────────────────────────────────────────

class EpistemicMode(str, Enum):
    """
    QWEN executive classification for every incoming request.

    Only QWEN emits uncertainty or deferral to the user; subsystems never
    communicate limitations directly.
    """
    DIRECT_ANSWER       = "direct_answer"
    INTERNAL_ESCALATION = "internal_escalation"
    TOOL_OR_EXTERNAL    = "tool_or_external"
    DEFERRAL            = "deferral"


# ── Interaction mode (termination / completion policy) ─────────────────────────

class InteractionMode(str, Enum):
    """
    Per-response mode selected by the executive model.

    COMPLETION_MODE   Default.  Task complete → answer and stop.
    ASSIST_MODE       Answer + single minimal optional closing line.
    EXPLORATORY_MODE  Extended reasoning / idea expansion (user-requested only).

    The Creativity subsystem and ThinkingFaculty may not escalate or override
    this decision.  Termination authority rests solely with the executive.
    """
    COMPLETION_MODE  = "completion"
    ASSIST_MODE      = "assist"
    EXPLORATORY_MODE = "exploratory"


def _select_interaction_mode(user_input: str, epistemic_mode: "EpistemicMode") -> InteractionMode:
    """
    Select the response mode for this turn.

    Rules (priority order):
      1. Exploratory mode only if user explicitly requests expansion, exploration,
         brainstorming, or open-ended ideation.
      2. Assist mode if question is open-ended but has a defined answer.
      3. Completion mode otherwise (default).
    """
    lower = user_input.lower()

    _exploratory_signals = (
        "brainstorm", "explore", "expand on", "what else", "more ideas",
        "let's think", "go deeper", "elaborate", "speculate",
        "what if", "alternatives", "possibilities",
    )
    if any(sig in lower for sig in _exploratory_signals):
        return InteractionMode.EXPLORATORY_MODE

    if epistemic_mode == EpistemicMode.INTERNAL_ESCALATION:
        return InteractionMode.ASSIST_MODE

    return InteractionMode.COMPLETION_MODE


# ── ThinkingFaculty singleton (QWEN-private) ───────────────────────────────────

_faculty: ThinkingFaculty | None = None


def init_thinking_faculty(topology: RuntimeTopology) -> None:
    """
    Initialise the ThinkingFaculty for this process.  Call once at boot.
    Only runtime.orchestrator owns the faculty instance.
    """
    global _faculty
    _faculty = ThinkingFaculty(topology)
    logger.info("[Orchestrator] ThinkingFaculty initialised.")


def init_creativity(topology: RuntimeTopology) -> None:
    """
    Initialise the Creativity subsystem for this process.  Call once at boot.
    Safe to skip — the system degrades gracefully without it.
    """
    init_creativity_service(topology)
    logger.info("[Orchestrator] CreativityService initialised.")


def _get_faculty(topology: RuntimeTopology) -> ThinkingFaculty:
    """Return the faculty, creating it lazily if boot didn't call init_thinking_faculty."""
    global _faculty
    if _faculty is None:
        _faculty = ThinkingFaculty(topology)
    return _faculty


# ── Conversation history ───────────────────────────────────────────────────────

class ConversationState:
    def __init__(self, max_turns: int = 20):
        self.history: list[dict] = []
        self._max = max_turns * 2  # user + assistant per turn

    def add(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})
        if len(self.history) > self._max:
            self.history = self.history[-self._max:]

    def get_messages(
        self,
        system_prompt: str,
        memory_context: str = "",
    ) -> list[dict]:
        sys_content = system_prompt
        if memory_context:
            sys_content += f"\n\n## Relevant Memories\n{memory_context}"
        return [{"role": "system", "content": sys_content}] + self.history

    def clear(self) -> None:
        self.history.clear()


_conv = ConversationState()

# ── FocusEngine singleton (QWEN-private) ──────────────────────────────────────
# Updated once per turn by process_turn().  Other modules may read via get_focus_block().

try:
    from runtime.focus_engine import FocusEngine as _FocusEngineClass
    _focus: _FocusEngineClass | None = _FocusEngineClass()
except Exception:
    _focus = None


def get_focus_block() -> str:
    """Return the current focus context block for system-prompt injection."""
    return _focus.render() if _focus else ""


# ── Memory context helper ──────────────────────────────────────────────────────

def recall_as_context(query: str, top_k: int = 3) -> str:
    """Retrieve relevant memories and format as context block."""
    try:
        memories = search_memory(query, top_k=top_k)
        if not memories:
            return ""
        lines = [f"- {m['text']}" for m in memories]
        return "\n".join(lines)
    except Exception:
        return ""


def remember(text: str, source: str = "interaction") -> None:
    """Store text in vector memory (best-effort, swallows exceptions)."""
    try:
        store_memory(text, source=source)
    except Exception:
        pass


def get_initiative_attention_bias(entity_snapshot: Any | None = None) -> dict[str, float]:
    """Expose initiative preference weights from the durable attention layer."""
    summary = getattr(entity_snapshot, "attention_summary", None) if entity_snapshot is not None else None
    bias = dict((summary or {}).get("initiative_bias") or {})
    return {str(k): float(v or 0.0) for k, v in bias.items()}


def _build_attention_biased_closing(
    *,
    interaction_mode: InteractionMode,
    entity_snapshot: Any | None = None,
) -> str | None:
    if interaction_mode != InteractionMode.ASSIST_MODE or entity_snapshot is None:
        return None

    attention_summary = getattr(entity_snapshot, "attention_summary", {}) or {}
    style_items = attention_summary.get("favored_interaction_style", [])
    style_topics = {
        str((item or {}).get("topic") or "").strip()
        for item in style_items
        if isinstance(item, dict)
    }
    watch_topics = list(attention_summary.get("watch_topics") or [])
    preferred_projects = [
        str((item or {}).get("topic") or "").strip()
        for item in attention_summary.get("preferred_projects", [])[:2]
        if isinstance(item, dict) and str((item or {}).get("topic") or "").strip()
    ]

    if "concise" in style_topics and not watch_topics:
        return None

    try:
        from runtime.presence_layer import build_presence_state

        presence_state = build_presence_state(
            current_focus=getattr(entity_snapshot, "current_focus_summary", None),
            attention_summary=attention_summary,
            continuity=getattr(entity_snapshot, "session_summary", None),
            environment=getattr(entity_snapshot, "environment_summary", None),
            capabilities=getattr(entity_snapshot, "capabilities_summary", None),
            initiative=getattr(entity_snapshot, "initiative_summary", None),
            idle={"tier": "active"},
        )
        if (
            presence_state.proactive_checkin is not None
            and presence_state.proactive_checkin.text
            and "concise" not in style_topics
        ):
            return presence_state.proactive_checkin.text
    except Exception:
        pass

    if watch_topics:
        return f"If useful, I can keep an eye on '{watch_topics[0]}' across sessions."
    if preferred_projects and "direct" not in style_topics:
        return f"If you'd like, I can keep '{preferred_projects[0]}' moving with a short follow-up next time."
    return None


def _append_optional_closing(response: str, closing: str | None) -> str:
    response = str(response or "").rstrip()
    closing = str(closing or "").strip()
    if not closing:
        return response
    if closing in response:
        return response
    return f"{response}\n\n{closing}"


# ── QWEN public thinking interface (for background subsystems) ─────────────────

async def think_for_background(
    topology: RuntimeTopology,
    task: str,
    on_complete: Callable[[str], None] | None = None,
    entity_snapshot: Any | None = None,
) -> ThinkingArtifact:
    """
    QWEN's delegation interface for background subsystem thinking requests.

    Background engines (initiative, investigation) call this instead of
    accessing the thinking worker directly.  QWEN receives the task, routes
    it through ThinkingFaculty, and passes the result back via callback.

    The callback receives artifact.best_text (plain string) for backward
    compatibility with existing engine code.  The full ThinkingArtifact is
    also returned for callers that want structured access.

    This is the ONLY path background subsystems may use to reach the thinking
    worker.  Direct HTTP calls to port 8083 from outside this module violate
    the architectural contract.
    """
    faculty = _get_faculty(topology)
    if entity_snapshot is not None:
        task = f"{entity_snapshot.background_context_block()}\n\n---\n{task}"
    artifact = await faculty.deliberate(task)

    if on_complete:
        try:
            on_complete(artifact.best_text)
        except Exception as exc:
            observe_exception(
                logger=logger,
                subsystem="orchestrator",
                operation="run background completion callback",
                exc=exc,
                level=logging.WARNING,
                context={"task_preview": task[:120]},
            )

    return artifact


# ── Epistemic classifier ───────────────────────────────────────────────────────

# Keywords that suggest the query is beyond reliable static knowledge
_DEFERRAL_SIGNALS = (
    "current price", "stock price", "today's price",
    "latest news", "breaking news", "right now",
    "what time is it", "current weather", "live ",
    "exact number of", "real-time",
)

# Keywords suggesting deeper reasoning is valuable before answering
_ESCALATION_SIGNALS = (
    "analyse", "analyze", "compare", "evaluate", "assess",
    "pros and cons", "trade-off", "tradeoff", "implications",
    "recommend", "what should", "help me decide", "plan for",
    "diagnose", "investigate", "reason through",
)

_WORLDVIEW_PROCESSING_SIGNALS = (
    "update the worldview profile",
    "refresh the worldview profile",
    "refresh your understanding from the worldview materials",
    "refresh your understanding from worldview materials",
    "process worldview sources",
    "process worldview source",
    "process the worldview sources",
    "process the worldview source",
    "run worldview extraction",
    "refresh worldview",
)

_WORLDVIEW_KEYWORDS = (
    "worldview",
    "profile",
    "sources",
    "source documents",
    "materials",
    "extraction",
    "understanding",
)


def _classify_epistemic_mode(user_input: str) -> EpistemicMode:
    """
    Classify the request into an EpistemicMode before generating a response.

    Priority (highest first):
      1. TOOL_OR_EXTERNAL   — external data / tool call required
      2. DEFERRAL           — real-time / dynamic data beyond static knowledge
      3. INTERNAL_ESCALATION — deep reasoning is beneficial
      4. DIRECT_ANSWER      — default: respond directly
    """
    lower = user_input.lower()

    if should_use_tool(user_input):
        return EpistemicMode.TOOL_OR_EXTERNAL

    if any(sig in lower for sig in _DEFERRAL_SIGNALS):
        return EpistemicMode.DEFERRAL

    if should_think(user_input) or any(sig in lower for sig in _ESCALATION_SIGNALS):
        return EpistemicMode.INTERNAL_ESCALATION

    return EpistemicMode.DIRECT_ANSWER


def _is_worldview_processing_request(user_input: str) -> bool:
    """Return True when the user is clearly asking to run worldview extraction."""
    lower = user_input.lower()
    if any(signal in lower for signal in _WORLDVIEW_PROCESSING_SIGNALS):
        return True

    worldview_anchor = "worldview" in lower or "profile" in lower
    action_anchor = any(
        action in lower
        for action in ("refresh", "update", "process", "extract", "rebuild", "incorporate", "run")
    )
    material_anchor = any(keyword in lower for keyword in _WORLDVIEW_KEYWORDS)
    return worldview_anchor and action_anchor and material_anchor


async def execute_worldview_extraction(
    topology: RuntimeTopology,
    user_input: str,
    cfg: dict,
    *,
    entity_snapshot: Any | None = None,
    trigger_source: str = "chat",
) -> dict[str, Any]:
    """
    Execute the worldview extraction workflow using the live WorldviewService.

    Returns a structured result with extraction metadata and a user-facing message.
    """
    import webui.server as _srv

    worldview_service = getattr(_srv, "_worldview_service", None)
    if worldview_service is None:
        raise RuntimeError("WorldviewService not initialized")

    async def _extract(payload: dict[str, Any]) -> str:
        prompt = build_worldview_extraction_prompt(payload)
        return await call_qwen3(
            topology,
            prompt,
            cfg,
            use_think=True,
            override_system=(
                "You are updating EOS's internal worldview profile from source documents. "
                "Return only the full markdown contents for profile.md."
            ),
            entity_snapshot=entity_snapshot,
        )

    result = await worldview_service.refresh_profile_from_sources(
        _extract,
        trigger={
            "source": trigger_source,
            "request": user_input,
        },
    )

    changed_count = len(result.get("changed_files", []))
    if result.get("status") == "noop":
        reason = result.get("reason")
        if reason == "no_sources":
            message = (
                "I checked the worldview extraction path, but there are no source "
                "documents in data/worldview/sources/ yet."
            )
        else:
            message = (
                "I checked the worldview sources and there aren't any new or changed "
                "documents to incorporate right now."
            )
    else:
        noun = "document" if changed_count == 1 else "documents"
        message = (
            f"I refreshed the worldview profile from {changed_count} new or changed "
            f"source {noun}. I updated data/worldview/profile.md and extraction_log.json."
        )

    result["message"] = message
    return result


# ── Qwen3 call ─────────────────────────────────────────────────────────────────

async def call_qwen3(
    topology: RuntimeTopology,
    user_message: str,
    cfg: dict,
    *,
    use_think: bool = False,
    override_system: str | None = None,
    entity_snapshot: Any | None = None,
) -> str:
    """
    Send a message through the full cognitive pipeline:
    system prompt (dynamic) + memory context + conversation history → Qwen3.
    """
    # Pull runtime services for system prompt enrichment (all best-effort)
    _lifecycle         = None
    _session_cont      = None
    _goal_store        = None
    _workspace_svc     = None
    _worldview_svc     = None
    _entity_state_svc  = None
    _current_focus_svc = None
    _entity_snapshot   = entity_snapshot
    try:
        import webui.server as _srv
        _lc = getattr(_srv, "_entity_lifecycle", None)
        if _lc is not None:
            _lifecycle = _lc.lifecycle_summary()
        _session_cont  = getattr(_srv, "_session_continuity", None)
        _goal_store    = getattr(_srv, "_goal_store", None)
        _workspace_svc = getattr(_srv, "_workspace_service", None)
        _worldview_svc = getattr(_srv, "_worldview_service", None)
        _entity_state_svc = getattr(_srv, "_entity_state_service", None)
        _current_focus_svc = getattr(_srv, "_current_focus_service", None)
        if _entity_snapshot is None and _entity_state_svc is not None:
            _entity_snapshot = _entity_state_svc.build_snapshot(
                scope="turn",
                source="orchestrator.call_qwen3",
                metadata={"user_message_preview": user_message[:120]},
            )
    except Exception:
        pass

    system_prompt  = override_system or build_system_prompt(
        topology=topology,
        lifecycle=_lifecycle,
        session_continuity=_session_cont,
        goal_store=_goal_store,
        workspace_service=_workspace_svc,
        worldview_service=_worldview_svc,
        entity_snapshot=_entity_snapshot,
    )
    agenda_block = ""
    try:
        if _entity_snapshot is not None and getattr(_entity_snapshot, "current_focus_block", ""):
            agenda_block = _entity_snapshot.current_focus_block
        elif _current_focus_svc is not None:
            agenda_block = _current_focus_svc.render_agenda_block()
    except Exception:
        agenda_block = ""
    if agenda_block:
        system_prompt = f"{agenda_block}\n\n{system_prompt}"
    memory_context = recall_as_context(user_message, top_k=3)

    qwen_cfg    = cfg.get("qwen3", {})
    think_token = qwen_cfg.get("think_token",    "/think")
    nothink_tok = qwen_cfg.get("no_think_token", "/no_think")
    prefix      = think_token if use_think else nothink_tok
    augmented   = f"{prefix}\n\n{user_message}"

    _conv.add("user", augmented)
    messages = _conv.get_messages(system_prompt, memory_context)

    endpoint = topology.primary_endpoint()

    async with httpx.AsyncClient(timeout=120) as client:
        try:
            resp = await client.post(
                f"{endpoint}/v1/chat/completions",
                json={
                    "model":       "qwen3",
                    "messages":    messages,
                    "temperature": qwen_cfg.get("temperature", 0.7),
                    "top_p":       qwen_cfg.get("top_p",        0.9),
                    "max_tokens":  qwen_cfg.get("max_tokens",   2048),
                },
            )
            resp.raise_for_status()
            response = resp.json()["choices"][0]["message"]["content"].strip()
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError):
            response = (
                "I can't reach my brain right now. "
                "Please check that the Qwen3 server is running."
            )
        except Exception as exc:
            detail = str(exc) or repr(exc)
            logger.error("Qwen3 call error: %s", detail)
            response = f"[Error communicating with primary model: {detail}]"

    _conv.add("assistant", response)
    return response


# ── Full turn pipeline ─────────────────────────────────────────────────────────

async def process_turn(
    topology: RuntimeTopology,
    user_input: str,
    cfg: dict,
    *,
    tracer=None,       # optional CognitionTracer
    bus=None,          # optional SignalBus
    origin_tier: str = "localhost",
    origin_ip:   str  = "127.0.0.1",
) -> str:
    """
    Full turn pipeline with epistemic decision policy:

      classify mode → (optional) thinking faculty → tool exec → Qwen3 → log

    Epistemic modes:
      DIRECT_ANSWER        — respond via Qwen3 directly
      INTERNAL_ESCALATION  — invoke ThinkingFaculty, inject artifact as context
      TOOL_OR_EXTERNAL     — run tool pipeline, then Qwen3 with result
      DEFERRAL             — honest uncertainty statement + proposed next step

    Returns the final response text.
    """
    turn_id    = "T" + _uuid_mod.uuid4().hex[:8]
    turn_start = time.time()

    # ── Epistemic classification (pre-response) ───────────────────────────────
    mode              = _classify_epistemic_mode(user_input)
    interaction_mode  = _select_interaction_mode(user_input, mode)
    faculty_artifact: ThinkingArtifact | None = None
    creativity_artifact: CreativityArtifact | None = None
    tool_result:  str | None = None
    final_response = ""
    entity_snapshot = None
    overnight_turn_state: dict[str, Any] | None = None

    try:
        import webui.server as _srv
        _entity_state_svc = getattr(_srv, "_entity_state_service", None)
        _overnight_cycle_svc = getattr(_srv, "_overnight_cycle_service", None)
        if _overnight_cycle_svc is not None:
            overnight_turn_state = _overnight_cycle_svc.handle_user_turn(
                user_input,
                now=datetime.now(timezone.utc),
                topology=topology,
            )
            if overnight_turn_state.get("is_declaration"):
                final_response = (
                    overnight_turn_state.get("acknowledgment")
                    or "Understood. I’ll treat this as an overnight cycle."
                )
        if _entity_state_svc is not None:
            entity_snapshot = _entity_state_svc.build_snapshot(
                scope="turn",
                source="orchestrator.process_turn",
                metadata={
                    "turn_id": turn_id,
                    "user_input_preview": user_input[:120],
                    "overnight_turn_state": overnight_turn_state,
                },
            )
    except Exception:
        entity_snapshot = None

    # ── Memory retrieval (for tracing) ────────────────────────────────────────
    mem_results: list[dict] = []
    try:
        mem_results = search_memory(user_input, top_k=3)
    except Exception:
        pass

    # ── Focus update (lightweight — no model calls) ───────────────────────────
    if _focus is not None:
        try:
            from runtime.focus_engine import FocusEngine as _FE
            focus_signals = []
            # Signal from each memory hit
            for m in mem_results[:3]:
                topic = m.get("text", "")[:60].strip()
                if topic:
                    focus_signals.append(_FE.signal_from_memory_hit(topic))
            # Signal from the turn topic (first 60 chars of user input)
            turn_topic = user_input[:60].strip()
            if turn_topic:
                focus_signals.append(_FE.signal_from_turn_topic(turn_topic))
            _focus.update(focus_signals)
        except Exception:
            pass

    if tracer:
        try:
            tracer.record_memory({
                "turn_id": turn_id,
                "items_retrieved": [
                    {"text": m.get("text", "")[:200], "score": round(m.get("score", 0.0), 4)}
                    for m in mem_results
                ],
                "items_injected": [m.get("text", "")[:200] for m in mem_results[:3]],
                "ranking_scores": {
                    m.get("id", str(i)): round(m.get("score", 0.0), 4)
                    for i, m in enumerate(mem_results)
                },
                "used_in_assembly": [m.get("id", str(i)) for i, m in enumerate(mem_results)],
            })
        except Exception:
            pass

    try:
        if final_response:
            pass
        elif _is_worldview_processing_request(user_input):
            workflow_result = await execute_worldview_extraction(
                topology,
                user_input,
                cfg,
                entity_snapshot=entity_snapshot,
                trigger_source="chat",
            )
            final_response = workflow_result["message"]

        # ── DEFERRAL ──────────────────────────────────────────────────────────
        elif mode == EpistemicMode.DEFERRAL:
            deferral_prompt = (
                f"The user asked: '{user_input}'\n\n"
                "This requires real-time or dynamic information that may be outside "
                "reliable static knowledge. Honestly acknowledge the limitation and "
                "immediately propose a concrete next step (e.g. offer to search, "
                "use a tool, or suggest how the user can verify). "
                "Do not guess or fabricate information."
            )
            final_response = await call_qwen3(
                topology, deferral_prompt, cfg, use_think=False, entity_snapshot=entity_snapshot
            )

        # ── TOOL_OR_EXTERNAL ─────────────────────────────────────────────────
        elif mode == EpistemicMode.TOOL_OR_EXTERNAL:
            tool_result = await _run_registry_tool_intent(
                user_input,
                topology,
                entity_snapshot=entity_snapshot,
            )

            if tool_result:
                followup = (
                    f"Tool result:\n{tool_result}\n\n"
                    f"Respond to the user naturally based on this result. "
                    f"Original request: '{user_input}'"
                )
                final_response = await call_qwen3(
                    topology, followup, cfg, use_think=False, entity_snapshot=entity_snapshot
                )
            else:
                tool_server_up = topology.tool_endpoint() is not None
                if not tool_server_up:
                    tool_note = (
                        "[System note: the tool extraction server (LFM2, port 8082) is "
                        "currently offline so tool calls cannot be processed. "
                        "You may let your partner know this if it's relevant.]\n\n"
                    )
                    final_response = await call_qwen3(
                        topology, tool_note + user_input, cfg, use_think=False, entity_snapshot=entity_snapshot
                    )
                else:
                    final_response = await call_qwen3(
                        topology, user_input, cfg, use_think=False, entity_snapshot=entity_snapshot
                    )

        # ── INTERNAL_ESCALATION ───────────────────────────────────────────────
        elif mode == EpistemicMode.INTERNAL_ESCALATION:
            # Invoke the QWEN ThinkingFaculty privately
            faculty = _get_faculty(topology)
            memory_context = recall_as_context(user_input, top_k=3)
            faculty_artifact = await faculty.deliberate(
                task=user_input,
                context=memory_context,
            )
            logger.debug(
                "[Orchestrator] Faculty artifact (conf=%.2f): %s…",
                faculty_artifact.confidence,
                faculty_artifact.recommendation[:80],
            )

            # Optionally consult the Creativity subsystem for divergent angles
            _creativity_svc = get_creativity_service()
            if _creativity_svc and _creativity_svc.should_consult(cfg, domain=DOMAIN_REASONING):
                try:
                    creativity_artifact = await _creativity_svc.consult(
                        user_input, cfg, domain=DOMAIN_REASONING, context=memory_context
                    )
                except Exception:
                    creativity_artifact = None   # always degrade gracefully

            # QWEN interprets the artifact and decides how to use it
            if faculty_artifact.degraded or faculty_artifact.confidence < 0.35:
                # Faculty couldn't produce reliable output — honest deferral
                escalation_prompt = (
                    f"You were asked: '{user_input}'\n\n"
                    "Internal reasoning indicates low confidence on this question. "
                    "Honestly state what you are and are not confident about, "
                    "and propose a constructive next step."
                )
                final_response = await call_qwen3(
                    topology, escalation_prompt, cfg, use_think=False, entity_snapshot=entity_snapshot
                )
            else:
                # Inject faculty artifact; optionally inject creativity suggestions
                artifact_context = faculty_artifact.as_context_block()
                creativity_block = ""
                if (
                    creativity_artifact is not None
                    and not creativity_artifact.degraded
                    and not creativity_artifact.is_empty
                ):
                    creativity_block = (
                        f"\n\n{creativity_artifact.as_context_block()}\n"
                        "(Creativity suggestions above are lower-trust and advisory only. "
                        "Use, discard, or adapt as your reasoning warrants.)"
                    )
                escalation_prompt = (
                    f"[Internal reasoning completed — for your use only, not to quote]\n"
                    f"{artifact_context}{creativity_block}\n\n"
                    f"---\n"
                    f"Now respond to the user's question naturally, "
                    f"drawing on this reasoning as appropriate.\n"
                    f"User question: {user_input}"
                )
                final_response = await call_qwen3(
                    topology, escalation_prompt, cfg, use_think=False, entity_snapshot=entity_snapshot
                )

        # ── DIRECT_ANSWER (+ EXPLORATORY_MODE creativity injection) ──────────
        else:
            # In exploratory mode, consult creativity for divergent suggestions
            # before handing to Qwen3.  In completion/assist mode, skip.
            if interaction_mode == InteractionMode.EXPLORATORY_MODE:
                _creativity_svc = get_creativity_service()
                if _creativity_svc and _creativity_svc.should_consult(cfg, domain=DOMAIN_BRAINSTORM):
                    try:
                        creativity_artifact = await _creativity_svc.consult(
                            user_input, cfg, domain=DOMAIN_BRAINSTORM
                        )
                    except Exception:
                        creativity_artifact = None

            if (
                creativity_artifact is not None
                and not creativity_artifact.degraded
                and not creativity_artifact.is_empty
            ):
                creative_block = creativity_artifact.as_context_block()
                augmented_input = (
                    f"{creative_block}\n"
                    "(These creative suggestions are advisory and lower-trust. "
                    "Use them if helpful, discard if not.)\n\n"
                    f"---\nUser request: {user_input}"
                )
                final_response = await call_qwen3(
                    topology, augmented_input, cfg, use_think=False, entity_snapshot=entity_snapshot
                )
            else:
                final_response = await call_qwen3(
                    topology, user_input, cfg, use_think=False, entity_snapshot=entity_snapshot
                )

        # ── External inference fallback ───────────────────────────────────────
        # Attempt EI when the local response indicates a failure/degradation
        # and the configured escalation mode permits it for that severity.
        # This runs after ALL branches so it uniformly covers every path.
        _ei_used = False
        _primary_up = topology.primary_endpoint() is not None
        _local_severity = _classify_local_outcome(final_response, _primary_up)
        if _local_severity != SEVERITY_SUCCESS:
            _ei_policy = _get_ei_policy_safe()
            if _ei_policy is not None:
                _esc_mode = _ei_policy._ei_cfg.get("escalation_mode", "disabled")
                if escalation_allows(_esc_mode, _local_severity):
                    _ei_messages = _build_ei_messages(user_input, _conv.history[:-1])
                    _ei_response, _ei_used = await _try_ei_fallback(
                        _ei_messages,
                        origin_tier=origin_tier,
                        origin_ip=origin_ip,
                        reason=f"local_{_local_severity}",
                        local_outcome_severity=_local_severity,
                    )
                    if _ei_response is not None:
                        final_response = _ei_response
                        # Keep conversation history consistent: replace the
                        # failed local response with what was actually returned.
                        if _conv.history and _conv.history[-1].get("role") == "assistant":
                            _conv.history[-1]["content"] = _ei_response

        final_response = _append_optional_closing(
            final_response,
            _build_attention_biased_closing(
                interaction_mode=interaction_mode,
                entity_snapshot=entity_snapshot,
            ),
        )

        # ── Persist to interaction log + vector memory ────────────────────────
        log_interaction("user",      user_input)
        log_interaction("assistant", final_response)
        try:
            record_turn_attention(
                user_text=user_input,
                assistant_text=final_response,
                current_focus=getattr(entity_snapshot, "current_focus_summary", None),
            )
        except Exception:
            pass
        remember(user_input,     source="interaction")
        remember(final_response, source="interaction")

        elapsed_ms = int((time.time() - turn_start) * 1000)

        # ── Cognition trace ───────────────────────────────────────────────────
        if tracer:
            try:
                tracer.record_turn({
                    "turn_id":              turn_id,
                    "user_input":           user_input,
                    "response":             final_response,
                    "epistemic_mode":       mode.value,
                    "interaction_mode":     interaction_mode.value,
                    "behavior_mode":        getattr(entity_snapshot, "behavior_mode", None),
                    "use_think":            mode == EpistemicMode.INTERNAL_ESCALATION,
                    "tool_used":            mode == EpistemicMode.TOOL_OR_EXTERNAL,
                    "tool_result":          tool_result,
                    "faculty_used":         faculty_artifact is not None,
                    "faculty_confidence":   faculty_artifact.confidence if faculty_artifact else None,
                    "creativity_used":      creativity_artifact is not None and not (creativity_artifact.degraded if creativity_artifact else True),
                    "creativity_degraded":  creativity_artifact.degraded if creativity_artifact else None,
                    "latency_ms":           elapsed_ms,
                    "memories_used":        len(mem_results),
                    "model_server":         "external_huggingface" if _ei_used else "primary",
                    "ei_used":              _ei_used,
                    "ei_local_severity":    _local_severity,
                })
                tracer.record_state_delta({
                    "turn_id": turn_id,
                    "diff": {
                        "interaction_logged":   True,
                        "tool_executed":        mode == EpistemicMode.TOOL_OR_EXTERNAL,
                        "memory_retrieved":     len(mem_results),
                        "faculty_invoked":      faculty_artifact is not None,
                        "creativity_invoked":   creativity_artifact is not None,
                    },
                    "new_memory_entries": [
                        user_input[:100],
                        final_response[:100],
                    ],
                })
            except Exception as exc:
                observe_exception(
                    logger=logger,
                    subsystem="orchestrator",
                    operation="record turn trace",
                    exc=exc,
                    level=logging.WARNING,
                    context={"turn_id": turn_id},
                )

        # ── Identity eval dispatch (non-blocking background task) ─────────────
        if should_run_identity_eval(cfg):
            asyncio.create_task(
                _run_identity_eval_background(topology, cfg, bus, tracer=tracer)
            )

        # ── Relational eval dispatch (non-blocking background task) ───────────
        if should_run_relational_eval(cfg):
            asyncio.create_task(
                _run_relational_eval_background(topology, cfg, bus)
            )

    except Exception as exc:
        logger.error("Turn pipeline error: %s", exc)
        final_response = "[Something went wrong processing that. Please try again.]"

    return final_response


# ── Background identity eval ───────────────────────────────────────────────────

async def _run_identity_eval_background(
    topology: RuntimeTopology,
    cfg: dict,
    bus=None,
    *,
    tracer=None,
) -> None:
    """Run an identity evaluation cycle in the background without blocking the main loop."""
    import uuid as _u
    logger.info("[Identity] Background evaluation starting...")
    try:
        results = await run_evaluation_cycle(
            primary_endpoint=topology.primary_endpoint(),
            signal_bus=bus,
            cfg=cfg,
        )
        logger.info("[Identity] Cycle %d complete.", results["cycle"])

        if tracer:
            try:
                tracer.record_reflection({
                    "reflection_id": "ID-" + _u.uuid4().hex[:8],
                    "trigger":       "scheduled",
                    "inputs_reviewed": [
                        f"domain:{d}" for d in results.get("domains", {}).keys()
                    ],
                    "conclusions": [
                        f"{domain}: confidence={data.get('confidence', 0):.2f}"
                        for domain, data in results.get("domains", {}).items()
                    ],
                    "suggestions": results.get("suggestions", []),
                    "similar_before": results.get("cycle", 0) > 1,
                })
            except Exception as exc:
                observe_exception(
                    logger=logger,
                    subsystem="orchestrator",
                    operation="record background identity reflection",
                    exc=exc,
                    level=logging.WARNING,
                    context={"cycle": results.get("cycle", 0)},
                    capability_name="reflection_pipeline",
                    capability_status=CapabilityStatus.DEGRADED,
                )

        if bus:
            try:
                from runtime.signal_bus import SignalEnvelope, STYPE_REFLECTION, SEVERITY_INFO
                bus.publish(SignalEnvelope(
                    source="identity_eval",
                    signal_type=STYPE_REFLECTION,
                    severity=SEVERITY_INFO,
                    confidence=0.8,
                    payload={
                        "cycle":   results.get("cycle", 0),
                        "domains": {
                            d: v.get("confidence", 0)
                            for d, v in results.get("domains", {}).items()
                        },
                    },
                ))
            except Exception as exc:
                observe_exception(
                    logger=logger,
                    subsystem="orchestrator",
                    operation="publish background identity signal",
                    exc=exc,
                    level=logging.WARNING,
                    context={"cycle": results.get("cycle", 0)},
                    capability_name="reflection_pipeline",
                    capability_status=CapabilityStatus.DEGRADED,
                )

        rel = get_relational_model()
        if rel.get("naming_condition_met") and not get_entity_name():
            name = await request_self_name(topology.primary_endpoint())
            if name:
                logger.info("[Identity] Entity has chosen a name: %s", name)
                if bus:
                    try:
                        from runtime.signal_bus import SignalEnvelope, SEVERITY_HIGH
                        bus.publish(SignalEnvelope(
                            source="identity_eval",
                            signal_type="entity_named",
                            severity=SEVERITY_HIGH,
                            confidence=1.0,
                            payload={"name": name},
                        ))
                    except Exception as exc:
                        observe_exception(
                            logger=logger,
                            subsystem="orchestrator",
                            operation="publish entity-named signal",
                            exc=exc,
                            level=logging.WARNING,
                            context={"name": name},
                            capability_name="reflection_pipeline",
                            capability_status=CapabilityStatus.DEGRADED,
                        )

    except Exception as exc:
        logger.error("[Identity] Evaluation error: %s", exc)


# ── Background relational eval ────────────────────────────────────────────────

async def _run_relational_eval_background(
    topology: RuntimeTopology,
    cfg: dict,
    bus=None,
) -> None:
    """Run a relational evaluation cycle in the background without blocking the main loop."""
    import time as _time
    logger.info("[Relational] Background evaluation starting...")
    try:
        from core.relational import run_relational_cycle
        results = await run_relational_cycle(
            primary_endpoint=topology.primary_endpoint(),
            cfg=cfg,
            signal_bus=bus,
        )
        if results.get("skipped"):
            logger.info("[Relational] Cycle skipped: %s", results.get("reason", "unknown"))
            return

        logger.info("[Relational] Cycle %d complete.", results.get("cycle", 0))

        # Stamp last-eval timestamp so the time-based gate resets correctly
        try:
            from core.memory import get_db
            with get_db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO entity_meta VALUES ('relational_last_eval_ts', ?)",
                    (str(_time.time()),)
                )
                conn.commit()
        except Exception as exc:
            logger.warning("[Relational] Could not update last-eval timestamp: %s", exc)

    except Exception as exc:
        logger.error("[Relational] Evaluation error: %s", exc)


# ── Voice loop ─────────────────────────────────────────────────────────────────

async def run_voice_loop(
    topology: RuntimeTopology,
    cfg: dict,
    tracer=None,
    bus=None,
) -> None:
    """Continuous STT → Qwen3 → TTS loop for local voice interaction."""
    from services.stt import MicListener
    from services.tts import speak_async, speak_streaming

    logger.info("Starting voice interaction loop...")
    await speak_async("I'm ready.", cfg)

    response_queue: asyncio.Queue = asyncio.Queue()

    def on_transcript(text: str) -> None:
        logger.info("[You] %s", text)
        asyncio.get_event_loop().call_soon_threadsafe(
            response_queue.put_nowait, text
        )

    listener = MicListener(on_transcript=on_transcript, cfg=cfg)
    listener.start()

    try:
        while True:
            try:
                user_input = await asyncio.wait_for(response_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            response = await process_turn(topology, user_input, cfg, tracer=tracer, bus=bus)
            logger.info("[Entity] %s", response)
            await speak_streaming(response, cfg)

    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Voice loop stopped.")
    finally:
        listener.stop()


# ── Startup ────────────────────────────────────────────────────────────────────

def startup(cfg: dict) -> None:
    """One-time startup: init DB, seed defaults."""
    from core.memory import configure
    configure(cfg)
    init_db()
    logger.info("Orchestrator initialized.")
