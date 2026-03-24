"""
EOS — QWEN ThinkingFaculty
Private internal deliberation faculty owned exclusively by the QWEN executive.

Architectural contract
----------------------
- This module is instantiated ONLY by runtime.orchestrator (the QWEN layer).
- Background engines (initiative, investigation, reflection) MUST NOT import
  this module directly.  They surface task proposals to the orchestrator and
  the orchestrator decides whether and how to invoke its thinking faculty.
- All outputs are structured internal artifacts (ThinkingArtifact), never
  conversational text delivered directly to the user.

Output contract
---------------
Every call to ThinkingFaculty.deliberate() returns a ThinkingArtifact with
four sections parsed from the model response:

  ANALYSIS       — what the task requires; known / unknown factors
  OPTIONS        — 2-3 numbered candidate approaches
  RECOMMENDATION — the single best action or conclusion
  CONFIDENCE     — float [0.0, 1.0] confidence in the recommendation

QWEN is responsible for interpreting the artifact and deciding what (if
anything) to include in the user-facing response.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from runtime.topology import RuntimeTopology

logger = logging.getLogger("eos.thinking_faculty")


# ── Structured system prompt ───────────────────────────────────────────────────

_FACULTY_SYSTEM_PROMPT = """\
You are an internal reasoning faculty. Your outputs are structured private \
artifacts consumed by the QWEN executive model and are never shown directly \
to users.

For every task return ONLY the following four sections in exactly this format. \
Do not add preamble, greetings, or conversational filler.

ANALYSIS:
<2-4 sentences: what the task requires, what is known, what remains uncertain>

OPTIONS:
1. <first candidate approach, one sentence>
2. <second candidate approach, one sentence>
3. <third candidate approach, one sentence — omit if only two are meaningful>

RECOMMENDATION:
<one sentence: the single best action or conclusion>

CONFIDENCE: <a decimal between 0.0 and 1.0>
"""


# ── ThinkingArtifact ──────────────────────────────────────────────────────────

@dataclass
class ThinkingArtifact:
    """
    Structured output from the ThinkingFaculty.

    Consumed only by the QWEN orchestrator layer.  Never forwarded verbatim
    to the user — QWEN interprets and decides what, if anything, to use.
    """
    analysis:       str        = ""
    options:        list[str]  = field(default_factory=list)
    recommendation: str        = ""
    confidence:     float      = 0.5
    raw:            str        = ""
    degraded:       bool       = False

    # ── Helpers ───────────────────────────────────────────────────────────────

    def as_context_block(self) -> str:
        """Format for optional injection into QWEN's internal reasoning context."""
        parts: list[str] = []
        if self.analysis:
            parts.append(f"[Internal Analysis]\n{self.analysis}")
        if self.options:
            opts = "\n".join(f"  {o}" for o in self.options)
            parts.append(f"[Options Considered]\n{opts}")
        if self.recommendation:
            parts.append(f"[Recommendation]\n{self.recommendation}")
        parts.append(f"[Confidence: {self.confidence:.2f}]")
        return "\n\n".join(parts)

    @property
    def best_text(self) -> str:
        """
        Single best text string for callers that need a plain summary.
        Prefers recommendation → analysis → raw.
        """
        return self.recommendation or self.analysis or self.raw


# ── Parser ────────────────────────────────────────────────────────────────────

def _parse_artifact(raw: str) -> ThinkingArtifact:
    """Parse structured sections from a thinking-worker response string."""
    artifact = ThinkingArtifact(raw=raw)

    # ANALYSIS
    m = re.search(
        r"ANALYSIS:\s*(.+?)(?=OPTIONS:|RECOMMENDATION:|CONFIDENCE:|$)",
        raw, re.S | re.I,
    )
    if m:
        artifact.analysis = m.group(1).strip()

    # OPTIONS (numbered lines)
    m = re.search(
        r"OPTIONS:\s*(.+?)(?=RECOMMENDATION:|CONFIDENCE:|$)",
        raw, re.S | re.I,
    )
    if m:
        opts_block = m.group(1).strip()
        artifact.options = [
            ln.strip()
            for ln in opts_block.splitlines()
            if ln.strip() and (ln.strip()[0].isdigit() or ln.strip().startswith("-"))
        ]

    # RECOMMENDATION
    m = re.search(
        r"RECOMMENDATION:\s*(.+?)(?=CONFIDENCE:|$)",
        raw, re.S | re.I,
    )
    if m:
        artifact.recommendation = m.group(1).strip()

    # CONFIDENCE
    m = re.search(r"CONFIDENCE:\s*([\d.]+)", raw, re.I)
    if m:
        try:
            artifact.confidence = min(1.0, max(0.0, float(m.group(1))))
        except ValueError:
            pass

    return artifact


# ── ThinkingFaculty ───────────────────────────────────────────────────────────

class ThinkingFaculty:
    """
    QWEN's private internal deliberation faculty.

    Wraps the LFM2.5-Thinking backend (port 8083) — or falls back to the
    primary Qwen3 server when the thinking server is absent — and enforces
    a structured output contract via ThinkingArtifact.

    Authority: only runtime.orchestrator may instantiate this class.
    Background subsystems access thinking capability through the orchestrator's
    think_for_background() coroutine, which routes here after QWEN authorises.
    """

    def __init__(self, topology: "RuntimeTopology") -> None:
        self._topology = topology

    # ── Core deliberation ─────────────────────────────────────────────────────

    async def deliberate(
        self,
        task: str,
        context: str = "",
        *,
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> ThinkingArtifact:
        """
        Invoke the faculty on a task.  Returns a ThinkingArtifact.

        Never raises — returns a degraded artifact on any failure so the
        QWEN caller can always continue with a safe fallback.

        Parameters
        ----------
        task        : The task or question to reason about.
        context     : Optional additional context (evidence, prior reasoning).
        temperature : Sampling temperature (default 0.3 for precision).
        max_tokens  : Response length budget.
        """
        from runtime.on_demand import get_on_demand_manager
        manager = get_on_demand_manager()
        if manager is not None:
            endpoint = await manager.ensure(
                "thinking",
                reason="executive deliberation requested auxiliary reasoning",
                task_type="deep_reasoning",
                escalation=True,
                requested_by="executive",
            )
        else:
            endpoint = self._topology.thinking_endpoint()

        if endpoint:
            model = "lfm25-thinking"
            _fallback_to_primary = False
        else:
            logger.debug("[ThinkingFaculty] Thinking server absent — routing to primary")
            endpoint = self._topology.primary_endpoint()
            model = "qwen3"
            _fallback_to_primary = True

        user_content = f"Context:\n{context}\n\nTask:\n{task}" if context else task

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{endpoint}/v1/chat/completions",
                    json={
                        "model":       model,
                        "messages":    [
                            {"role": "system", "content": _FACULTY_SYSTEM_PROMPT},
                            {"role": "user",   "content": user_content},
                        ],
                        "temperature": temperature,
                        "max_tokens":  max_tokens,
                    },
                )
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"].strip()

            artifact = _parse_artifact(raw)
            if _fallback_to_primary:
                artifact.degraded = True
                logger.debug("[ThinkingFaculty] Artifact produced by primary fallback — marked degraded")
            logger.debug(
                "[ThinkingFaculty] Artifact: conf=%.2f rec=%.60s…",
                artifact.confidence,
                artifact.recommendation,
            )
            return artifact

        except Exception as exc:
            logger.warning("[ThinkingFaculty] Deliberation failed: %s", exc)
            return ThinkingArtifact(
                raw=f"[ThinkingFaculty unavailable: {exc}]",
                degraded=True,
                confidence=0.0,
            )
