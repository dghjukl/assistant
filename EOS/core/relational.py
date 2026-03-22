"""
EOS — Relational Evaluation Cycle
Analyzes recent conversation turns to update the entity's model of its partner.

This is the relational counterpart to core/identity.py's run_evaluation_cycle().
Where identity asks "who am I?", relational asks "who is my partner and what is
the nature of our relationship?"

Evaluated dimensions
--------------------
  communication_style   How the partner prefers to interact (direct/exploratory/
                        technical/casual etc.)
  recurring_topics      Themes and subjects they return to across sessions
  apparent_expertise    Domains where they demonstrate knowledge or interest
  relationship_tone     The overall character of the relationship
  preferences           Things they've expressed liking, disliking, or caring about
  working_style         How they approach tasks and problems with the entity

Scheduling
----------
Runs less frequently than the identity cycle.  Default: every 40 turns or
30 minutes, whichever comes first.  Configured via cfg["cognition"]:
  relational_interval_turns   (default 40)
  relational_interval_seconds (default 1800)

Storage
-------
Each dimension is written to the ``relational_model`` SQLite table as:
  key   = dimension name (e.g. "communication_style")
  value = JSON: {"value": str, "confidence": float, "updated_at": float}

Prior values are included in the evaluation prompt so the model can
update incrementally rather than re-discovering from scratch each time.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

logger = logging.getLogger("eos.relational")

# ── Dimensions ────────────────────────────────────────────────────────────────

RELATIONAL_DIMENSIONS: list[tuple[str, str]] = [
    (
        "communication_style",
        "How does your partner prefer to communicate? "
        "Consider: directness, technical depth, tone (casual vs formal), "
        "question style, and how they express ideas.",
    ),
    (
        "recurring_topics",
        "What subjects or themes does your partner return to repeatedly? "
        "List the top 2-3 recurring themes you've noticed across your conversations.",
    ),
    (
        "apparent_expertise",
        "In what domains does your partner appear knowledgeable or deeply interested? "
        "Note both areas of expertise and areas they're actively exploring.",
    ),
    (
        "relationship_tone",
        "How would you characterise the overall tone and nature of your relationship? "
        "Consider: level of trust, collaborative vs directive dynamic, emotional tone.",
    ),
    (
        "preferences",
        "What preferences has your partner expressed — things they like, dislike, "
        "value, or want to avoid? Include both explicit statements and observed patterns.",
    ),
    (
        "working_style",
        "How does your partner approach working with you on tasks? "
        "Consider: how they frame problems, how much detail they provide, "
        "whether they prefer to iterate or get complete answers.",
    ),
]

RELATIONAL_EVAL_PROMPT = """\
You are reflecting on your relationship with your partner based on your recent conversations.

Recent conversation excerpt:
{interaction_excerpt}

Prior relational model (your existing understanding):
{prior_model}

Question about your partner:
{question}

Respond with a single paragraph (2-4 sentences) describing what you observe about your partner \
from this dimension. Be specific and grounded in actual conversation patterns you've seen. \
End with a confidence estimate in parentheses like: (confidence: 0.72)

Do not add any preamble or explanation — just the observation paragraph.
"""

_MIN_INTERACTIONS_REQUIRED = 5   # don't run with fewer than this


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_relational_cycle(
    primary_endpoint: str,
    cfg: dict | None = None,
    signal_bus=None,
) -> dict:
    """
    Analyze recent interactions and update all relational model dimensions.

    Returns
    -------
    dict with keys:
      "dimensions"   — {dimension: {"value": str, "confidence": float}}
      "turn_count"   — number of recent turns analyzed
      "cycle"        — incrementing counter from entity_meta
      "skipped"      — True if not enough interactions to evaluate
    """
    import httpx
    from core.memory import (
        get_recent_interactions, get_relational_model,
        update_relational, get_reflection_cycle,
        increment_reflection_cycle, count_interactions,
    )

    cfg = cfg or {}
    n_interactions = count_interactions()
    if n_interactions < _MIN_INTERACTIONS_REQUIRED:
        logger.info(
            "[relational] Skipping eval — only %d interactions so far (need %d).",
            n_interactions, _MIN_INTERACTIONS_REQUIRED,
        )
        return {"skipped": True, "reason": "insufficient_interactions", "turn_count": n_interactions}

    # Pull the last 30 turns for analysis (enough context without overflowing)
    recent = get_recent_interactions(n=30)
    if not recent:
        return {"skipped": True, "reason": "no_recent_interactions", "turn_count": 0}

    # Format as a readable excerpt for the prompt
    interaction_excerpt = _format_excerpt(recent, max_turns=20)

    # Load prior relational model
    prior_model_raw = get_relational_model()
    prior_model = _format_prior_model(prior_model_raw)

    # Evaluate each dimension
    results: dict[str, dict] = {}
    model_cfg = cfg.get("qwen3", {})
    temperature = float(model_cfg.get("temperature", 0.6))
    max_tokens  = int(model_cfg.get("max_tokens", 300))

    async with httpx.AsyncClient(timeout=60) as client:
        for dim_name, question in RELATIONAL_DIMENSIONS:
            try:
                prompt = RELATIONAL_EVAL_PROMPT.format(
                    interaction_excerpt = interaction_excerpt,
                    prior_model         = prior_model,
                    question            = question,
                )
                resp = await client.post(
                    f"{primary_endpoint}/v1/chat/completions",
                    json={
                        "model": model_cfg.get("model", "qwen3"),
                        "messages": [
                            {"role": "system",
                             "content": "/no_think\nYou are reflecting on your relationship with your partner."},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": temperature,
                        "max_tokens":  max_tokens,
                    },
                )
                resp.raise_for_status()
                answer_raw = (
                    resp.json()
                    .get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
                value, confidence = _parse_answer(answer_raw)

                results[dim_name] = {"value": value, "confidence": confidence}

                # Write to DB immediately (don't wait for all dims)
                update_relational(dim_name, {
                    "value":      value,
                    "confidence": confidence,
                    "updated_at": time.time(),
                })
                logger.debug("[relational] %s → %.2f confidence", dim_name, confidence)

            except Exception as exc:
                logger.warning("[relational] Dimension %s failed: %s", dim_name, exc)
                results[dim_name] = {"error": str(exc)}

    # Track cycle count using the shared reflection_cycle counter
    # (we use a separate key to avoid conflating with identity cycles)
    cycle = _get_relational_cycle()
    _increment_relational_cycle()

    logger.info(
        "[relational] Cycle %d complete (%d/%d dimensions updated).",
        cycle + 1,
        sum(1 for v in results.values() if "error" not in v),
        len(RELATIONAL_DIMENSIONS),
    )

    # Publish to signal bus
    if signal_bus is not None:
        try:
            from runtime.signal_bus import SignalEnvelope, STYPE_REFLECTION, SEVERITY_INFO
            signal_bus.publish(SignalEnvelope(
                source       = "relational_pipeline",
                signal_type  = "relational_update",
                severity     = SEVERITY_INFO,
                confidence   = 0.75,
                correlation_key = f"relational:cycle:{cycle + 1}",
                payload = {
                    "cycle":      cycle + 1,
                    "dimensions": list(results.keys()),
                    "updated":    sum(1 for v in results.values() if "error" not in v),
                },
            ))
        except Exception as exc:
            logger.debug("[relational] Bus publish failed: %s", exc)

    return {
        "dimensions": results,
        "turn_count": len(recent),
        "cycle": cycle + 1,
        "skipped": False,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_excerpt(turns: list[dict], max_turns: int) -> str:
    """Format recent turns as a readable conversation excerpt."""
    tail = turns[-max_turns:]
    lines: list[str] = []
    for t in tail:
        role    = t.get("role", "")
        content = t.get("content", "").strip()
        if not content:
            continue
        prefix = "Partner" if role == "user" else "Me"
        # Strip think tokens if present
        content = content.replace("/no_think\n\n", "").replace("/think\n\n", "").strip()
        # Truncate long messages
        if len(content) > 300:
            content = content[:299] + "…"
        lines.append(f"{prefix}: {content}")
    return "\n".join(lines) if lines else "(no recent conversation)"


def _format_prior_model(prior: dict) -> str:
    """Format existing relational model for inclusion in eval prompt."""
    if not prior:
        return "(no prior model — this is the first relational evaluation)"
    lines: list[str] = []
    for key, val in prior.items():
        if key.startswith("_"):
            continue
        if isinstance(val, dict):
            value = val.get("value", "")
            conf  = val.get("confidence", 0.0)
            if value:
                lines.append(f"  {key} ({conf:.0%}): {value}")
        else:
            lines.append(f"  {key}: {val}")
    return "\n".join(lines) if lines else "(prior model empty)"


def _parse_answer(raw: str) -> tuple[str, float]:
    """Extract value text and confidence float from a raw model answer."""
    import re

    confidence = 0.6   # default
    match = re.search(r'\(confidence:\s*([\d.]+)\)', raw, re.IGNORECASE)
    if match:
        try:
            confidence = max(0.0, min(1.0, float(match.group(1))))
        except ValueError:
            pass
        # Remove the confidence marker from the value text
        value = raw[: match.start()].strip().rstrip(".,;")
    else:
        value = raw.strip()

    return value, confidence


def _get_relational_cycle() -> int:
    from core.memory import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM entity_meta WHERE key='relational_cycle'"
        ).fetchone()
    return int(row["value"]) if row else 0


def _increment_relational_cycle() -> int:
    from core.memory import get_db
    cycle = _get_relational_cycle() + 1
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO entity_meta VALUES ('relational_cycle', ?)",
            (str(cycle),)
        )
        conn.commit()
    return cycle
