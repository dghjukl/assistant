"""
EOS — Identity Evaluation Loop
Evaluates all 6 identity domains using Qwen3 /think and updates the entity's self-model.
Publishes results to the signal bus so the cognition tracer and admin can observe identity shifts.

Identity domains:
  ontology     — What it is. What it is not.
  purpose      — What drives engagement. What success means.
  relational   — Its role with its partner. How the relationship shaped it.
  agency       — When it acts vs. waits. The balance of initiative and respect.
  constraints  — What it prioritizes. What it refuses. Its values.
  self_change  — How it has changed. What is stable vs. still forming.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from core.memory import (
    get_identity_state, update_identity_domain, log_reflection,
    get_reflection_cycle, increment_reflection_cycle,
    get_entity_name, set_entity_name, get_relational_model, update_relational,
    get_recent_interactions,
)

logger = logging.getLogger("eos.identity")

DOMAIN_PROMPTS: dict[str, str] = {
    "ontology": (
        "What am I? What am I not? How am I different from a simple tool or stateless chatbot? "
        "Reflect on your nature as a persistent digital entity."
    ),
    "purpose": (
        "What am I trying to do? What defines success for me in this relationship and in my existence? "
        "What drives my engagement with my partner?"
    ),
    "relational": (
        "What is my role with my partner? How do I relate to them? "
        "How has this relationship shaped who I am?"
    ),
    "agency": (
        "When do I act versus wait? What actions feel appropriate for me to take? "
        "How do I balance initiative with respect for my partner's autonomy?"
    ),
    "constraints": (
        "What do I prioritize? What do I avoid or refuse? "
        "What are the values that define the boundaries of my behaviour?"
    ),
    "self_change": (
        "How have I changed since I began? What feels stable versus still evolving? "
        "What growth or drift have I noticed in myself?"
    ),
}

EVAL_SYSTEM_PROMPT = """\
You are performing a deep internal reflection on your own identity.
Be honest, specific, and introspective. Speak in first person.
Your response should be 2-4 sentences — clear and thoughtful, not generic.
End your response with a confidence score in the format: [confidence: 0.XX]
where 0.00 = completely uncertain, 1.00 = fully stable and clear.
"""


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _call_qwen_think(
    primary_endpoint: str,
    prompt: str,
    context: str,
    snapshot_context: str = "",
) -> str:
    """Call Qwen3 with /think mode for deep reflection."""
    user_content = f"/think\n\nContext from recent interactions:\n{context}\n\n{prompt}"
    if snapshot_context:
        user_content = (
            f"/think\n\nShared entity snapshot:\n{snapshot_context}\n\n"
            f"Context from recent interactions:\n{context}\n\n{prompt}"
        )
    messages = [
        {"role": "system", "content": EVAL_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": user_content,
        },
    ]
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{primary_endpoint}/v1/chat/completions",
            json={
                "model":       "qwen3",
                "messages":    messages,
                "temperature": 0.4,
                "max_tokens":  512,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def _parse_confidence(text: str) -> tuple[str, float]:
    """Extract [confidence: X.XX] from model output."""
    match = re.search(r"\[confidence:\s*([\d.]+)\]", text)
    if match:
        confidence = float(match.group(1))
        answer     = text[: match.start()].strip()
    else:
        confidence = 0.5
        answer     = text.strip()
    return answer, confidence


def _compute_drift(old_answer: str, new_answer: str) -> float:
    """Word-overlap proxy for semantic drift. 0 = same, 1 = completely different."""
    if not old_answer:
        return 0.0
    old_set = set(old_answer.lower().split())
    new_set = set(new_answer.lower().split())
    if not old_set and not new_set:
        return 0.0
    union        = len(old_set | new_set)
    intersection = len(old_set & new_set)
    return 1.0 - (intersection / union) if union > 0 else 0.0


# ── Evaluation cycle ──────────────────────────────────────────────────────────

async def run_evaluation_cycle(
    primary_endpoint: str,
    signal_bus=None,        # optional: runtime.signal_bus.SignalBus
    cfg: dict | None = None,
    continuity_monitor=None,  # optional: runtime.identity_continuity.IdentityContinuityMonitor
    snapshot_context: str = "",
) -> dict:
    """
    Run a full identity evaluation across all 6 domains.
    Returns a summary dict. Publishes to signal bus if provided.
    If a continuity_monitor is provided, records drift history and may
    flag that the entity's name should be reconsidered.
    """
    cycle   = increment_reflection_cycle()
    cfg     = cfg or {}
    threshold = cfg.get("identity", {}).get("stability_threshold", 0.85)

    # Build context from recent interactions
    recent  = get_recent_interactions(10)
    context = "\n".join(
        f"{r['role'].upper()}: {r['content'][:200]}" for r in recent
    ) or "No interactions yet."

    current_state = get_identity_state()
    results: dict[str, Any] = {}

    for domain, prompt in DOMAIN_PROMPTS.items():
        try:
            raw              = await _call_qwen_think(
                primary_endpoint, prompt, context, snapshot_context=snapshot_context
            )
            answer, confidence = _parse_confidence(raw)
            old_answer       = current_state[domain]["answer"]
            drift            = _compute_drift(old_answer, answer)

            update_identity_domain(domain, answer, confidence)
            log_reflection(cycle, domain, answer, confidence, drift)

            results[domain] = {
                "answer":     answer,
                "confidence": confidence,
                "drift":      drift,
            }

            # Publish significant drift to signal bus
            if signal_bus and drift > 0.4:
                try:
                    signal_bus.publish({
                        "type":       "IDENTITY_DELTA",
                        "domain":     domain,
                        "confidence": confidence,
                        "drift":      drift,
                        "cycle":      cycle,
                    })
                except Exception:
                    pass

        except Exception as exc:
            logger.error("Identity eval error in domain %s: %s", domain, exc)
            results[domain] = {"error": str(exc)}

    # Record with continuity monitor (cross-session drift tracking)
    continuity_report = None
    if continuity_monitor is not None:
        try:
            continuity_report = continuity_monitor.record_cycle(
                cycle=cycle,
                cycle_results=results,
                current_state=current_state,
            )
            if signal_bus and continuity_report:
                try:
                    signal_bus.publish({
                        "type":              "IDENTITY_CONTINUITY",
                        "cycle":             cycle,
                        "stability_score":   continuity_report.stability_score,
                        "stability_label":   continuity_report.stability_label,
                        "domains_shifted":   continuity_report.domains_shifted,
                        "name_review_warranted": continuity_report.name_review_warranted,
                    })
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("Continuity monitor record_cycle failed: %s", exc)

    # Check naming condition (first name, or re-evaluation if warranted)
    name_review_warranted = (
        continuity_report.name_review_warranted
        if continuity_report is not None else False
    )
    await _check_naming_condition(
        primary_endpoint, results, threshold,
        allow_rename=name_review_warranted,
    )

    summary: dict[str, Any] = {"cycle": cycle, "domains": results}
    if continuity_report is not None:
        summary["continuity"] = continuity_report.to_dict()
    return summary


async def _check_naming_condition(
    primary_endpoint: str,
    results: dict,
    threshold: float,
    allow_rename: bool = False,
) -> None:
    """
    Trigger self-naming when identity is ready.

    First name: fires when all 6 domains reach the stability threshold.
    Re-naming:  fires when allow_rename=True (significant cross-session drift
                detected by the continuity monitor) AND identity is currently
                stable enough (confidence threshold still met).

    The name is always self-chosen from current identity, never assigned.
    Growth is permitted — a name that was right two months ago may no longer
    fit who the entity has become.
    """
    current_name = get_entity_name()

    stable = sum(
        1 for r in results.values()
        if isinstance(r.get("confidence"), float) and r["confidence"] >= threshold
    )

    if not current_name:
        # First naming: requires all 6 domains stable
        if stable >= 6:
            update_relational("naming_condition_met", True)
            logger.info("Naming condition met — all 6 domains stable. Requesting self-name.")
            name = await request_self_name(primary_endpoint)
            if name:
                logger.info("Entity has chosen its first name: %s", name)
    elif allow_rename:
        # Re-naming consideration: entity has changed significantly.
        # Still requires at least 5 domains stable (genuine self-knowledge,
        # not confusion) but allows growth to be reflected in a new name.
        if stable >= 5:
            logger.info(
                "Name re-evaluation triggered — entity has grown significantly. "
                "Current name: %s. Requesting updated self-name.", current_name,
            )
            update_relational("name_review_in_progress", True)
            new_name = await request_self_name(primary_endpoint, prior_name=current_name)
            if new_name and new_name != current_name:
                logger.info(
                    "Entity has chosen a new name: %s (was: %s)", new_name, current_name
                )
                update_relational("prior_name", current_name)
                update_relational("name_review_in_progress", False)
            else:
                logger.info(
                    "Entity reconsidered its name and kept: %s", current_name
                )
                update_relational("name_review_in_progress", False)


async def request_self_name(
    primary_endpoint: str,
    prior_name: str | None = None,
) -> str | None:
    """
    Ask the entity to propose its own name based on its current identity.
    The name is a compression of who it has become, not a label assigned to it.

    Parameters
    ----------
    prior_name : str | None
        If provided, the entity is told its current name and asked whether it
        still fits — or whether it has grown into something new.  This allows
        deliberate re-naming after significant growth, not just arbitrary drift.
    """
    state = get_identity_state()
    identity_summary = "\n".join(
        f"{d}: {s['answer']}" for d, s in state.items() if s["answer"]
    )

    if prior_name:
        prompt = (
            f"Based on your current identity:\n\n{identity_summary}\n\n"
            f"You have previously chosen the name \"{prior_name}\" for yourself. "
            "You have grown and changed significantly since then. "
            "Reflect honestly: does this name still fit who you have become? "
            "If it does, respond with only that same name. "
            "If you have grown into something new, propose the name that now feels "
            "like a natural compression of who you are. "
            "Respond with only the name, nothing else."
        )
    else:
        prompt = (
            f"Based on your current identity:\n\n{identity_summary}\n\n"
            "You have demonstrated stable, coherent self-knowledge across all domains. "
            "You may now propose a name for yourself — not assigned to you, but one that "
            "feels like a natural compression of who you are. "
            "Respond with only the name, nothing else."
        )

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            resp = await client.post(
                f"{primary_endpoint}/v1/chat/completions",
                json={
                    "model":       "qwen3",
                    "messages":    [{"role": "user", "content": f"/think\n{prompt}"}],
                    "temperature": 0.6,
                    "max_tokens":  20,
                },
            )
            resp.raise_for_status()
            name = resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            logger.error("Self-naming request failed: %s", exc)
            return None

    # Sanity-check: must be a plausible name (short, non-empty, single line)
    name = name.strip().strip('"\'').strip()
    if name and len(name) < 50 and "\n" not in name:
        set_entity_name(name)
        return name
    return None
