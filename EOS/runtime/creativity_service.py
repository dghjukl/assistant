"""
EOS — Creativity Subsystem
===========================
First-class cognitive service for controlled divergence.

Role
----
The Creativity server is a cross-cutting cognitive service that generates:
  - Alternate interpretations of problems or inputs
  - Non-obvious solution paths
  - Reframings and analogical mappings
  - Ideation and speculative branches
  - Behavioral variety and initiative expansion

Authority constraints (IMMUTABLE)
----------------------------------
  - Advisory only.  Outputs are lower-trust cognitive material.
  - May PROPOSE, REINTERPRET, EXPAND, DIVERSIFY.
  - May NOT DECIDE, AUTHORIZE, EXECUTE TOOLS, or OVERRIDE grounded reasoning.
  - Termination decisions made by the executive model are never overridden.
  - Final answers always come from the executive reasoning path (Qwen3).

Availability and degradation
-----------------------------
  - Optional at runtime.  The system MUST degrade gracefully if absent.
  - No task may fail due to the Creativity server being unavailable.
  - All invocations are conditional:
      If available → consult for divergence
      If unavailable → skip and proceed normally
  - The only observable impact of absence: reduced divergence, reduced
    novelty, more conventional solution paths.

User configurability
--------------------
All behaviour is parameter-driven.  Nothing is hard-coded.  Settings are
read from config at invocation time (hot-reload compatible).  If the server
is unavailable, all settings are safely ignored without error.

Config path: cfg["creativity"]
  enabled               bool        Global on/off switch
  injection_frequency   str         "off" | "low" | "medium" | "high"
  intensity             str         "conservative" | "balanced" | "exploratory" | "aggressive"
  invocation_domains    dict        Per-domain enable flags
  output_structure_mode str         "structured" | "semi-structured" | "loose"
  advanced              dict        Optional low-level overrides (temperature, top_p, …)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("eos.creativity")


# ── Invocation domain keys ─────────────────────────────────────────────────────

DOMAIN_REASONING     = "reasoning_assistance"
DOMAIN_AUTONOMOUS    = "autonomous_idle"
DOMAIN_EXPLANATION   = "explanation_generation"
DOMAIN_BRAINSTORM    = "brainstorming_design"
DOMAIN_STUCK         = "stuck_state_recovery"


# ── Intensity → sampling parameter presets ────────────────────────────────────

_INTENSITY_PRESETS: dict[str, dict[str, float]] = {
    "conservative": {"temperature": 0.60, "top_p": 0.85, "top_k": 40},
    "balanced":     {"temperature": 0.85, "top_p": 0.92, "top_k": 60},
    "exploratory":  {"temperature": 1.05, "top_p": 0.96, "top_k": 80},
    "aggressive":   {"temperature": 1.30, "top_p": 0.99, "top_k": 100},
}

# Injection frequency → approximate probability of consulting creativity per eligible turn
_FREQUENCY_RATES: dict[str, float] = {
    "off":    0.00,
    "low":    0.20,
    "medium": 0.50,
    "high":   1.00,
}


# ── Creativity artifact ────────────────────────────────────────────────────────

@dataclass
class CreativityArtifact:
    """
    Output of a single creativity consultation.

    Fields
    ------
    suggestions : list[str]
        One or more divergent suggestions, reframings, or alternative paths.
    structure_mode : str
        The output structure mode used ("structured" | "semi-structured" | "loose").
    degraded : bool
        True if the server was unavailable or the call failed.  Callers must
        handle degraded artifacts by proceeding without them — never raising.
    elapsed_ms : int
        Approximate call duration in milliseconds.
    """
    suggestions:    list[str] = field(default_factory=list)
    structure_mode: str       = "structured"
    degraded:       bool      = False
    elapsed_ms:     int       = 0

    @property
    def is_empty(self) -> bool:
        return not self.suggestions

    def as_context_block(self) -> str:
        """Format for injection into the executive reasoning path."""
        if self.degraded or self.is_empty:
            return ""
        header = "[Creativity subsystem — advisory only, lower-trust, for consideration]"
        body   = "\n".join(f"  • {s}" for s in self.suggestions)
        return f"{header}\n{body}"


# ── Creativity service ─────────────────────────────────────────────────────────

class CreativityService:
    """
    Manages all interactions with the Creativity server (port 8084).

    Usage
    -----
    1.  Instantiate once at boot (or lazily):
            svc = CreativityService(topology)

    2.  Before each invocation, check whether to consult:
            if svc.should_consult(cfg, domain=DOMAIN_REASONING):
                artifact = await svc.consult(task, cfg)
                # inject artifact.as_context_block() into executive prompt

    3.  If the server is unavailable, consult() returns a degraded artifact
        and logs a warning.  Never raises.  Callers proceed normally.
    """

    def __init__(self, topology: Any) -> None:
        self._topology = topology

    # ── Configuration helpers ──────────────────────────────────────────────────

    @staticmethod
    def _get_cfg(cfg: dict) -> dict:
        """Extract the creativity sub-config, returning safe defaults if absent."""
        return cfg.get("creativity", {})

    @staticmethod
    def _is_globally_enabled(ccfg: dict) -> bool:
        return bool(ccfg.get("enabled", False))

    @staticmethod
    def _frequency_rate(ccfg: dict) -> float:
        freq = ccfg.get("injection_frequency", "medium")
        return _FREQUENCY_RATES.get(freq, 0.5)

    @staticmethod
    def _domain_enabled(ccfg: dict, domain: str) -> bool:
        domains = ccfg.get("invocation_domains", {})
        return bool(domains.get(domain, True))   # default: domain is enabled

    @staticmethod
    def _sampling_params(ccfg: dict) -> dict[str, Any]:
        """
        Build sampling parameters.

        Precedence (highest to lowest):
          1. advanced.* explicit overrides (when not null)
          2. intensity preset
          3. hardcoded fallback
        """
        intensity = ccfg.get("intensity", "balanced")
        preset    = _INTENSITY_PRESETS.get(intensity, _INTENSITY_PRESETS["balanced"]).copy()

        advanced = ccfg.get("advanced", {}) or {}
        if advanced.get("temperature") is not None:
            preset["temperature"] = float(advanced["temperature"])
        if advanced.get("top_p") is not None:
            preset["top_p"] = float(advanced["top_p"])
        if advanced.get("top_k") is not None:
            preset["top_k"] = int(advanced["top_k"])

        max_tokens = advanced.get("max_tokens") or 512
        return {
            "temperature": preset["temperature"],
            "top_p":       preset["top_p"],
            "max_tokens":  int(max_tokens),
        }

    @staticmethod
    def _output_structure_mode(ccfg: dict) -> str:
        return ccfg.get("output_structure_mode", "structured")

    @staticmethod
    def _format_template(ccfg: dict) -> str | None:
        return (ccfg.get("advanced") or {}).get("response_format_template")

    # ── Decision gate ──────────────────────────────────────────────────────────

    def should_consult(
        self,
        cfg: dict,
        domain: str = DOMAIN_REASONING,
        *,
        _rng: float | None = None,   # injectable for deterministic tests
    ) -> bool:
        """
        Gate: returns True only if creativity should be consulted this turn.

        Checks in order:
          1. Global enabled flag
          2. Server availability (live runtime check)
          3. Domain flag
          4. Injection frequency sampling
        """
        ccfg = self._get_cfg(cfg)

        if not self._is_globally_enabled(ccfg):
            return False

        if self._topology.creativity_endpoint() is None:
            return False  # server not running — skip silently

        if not self._domain_enabled(ccfg, domain):
            return False

        rate = self._frequency_rate(ccfg)
        if rate <= 0.0:
            return False
        if rate >= 1.0:
            return True

        import random
        probe = _rng if _rng is not None else random.random()
        return probe < rate

    # ── Core invocation ────────────────────────────────────────────────────────

    async def consult(
        self,
        task: str,
        cfg: dict,
        *,
        domain: str = DOMAIN_REASONING,
        context: str = "",
    ) -> CreativityArtifact:
        """
        Consult the Creativity server for divergent suggestions on `task`.

        Always returns a CreativityArtifact.  If the server is unavailable or
        the call fails, returns a degraded artifact — never raises.

        Parameters
        ----------
        task : str
            The problem, question, or prompt to generate creative suggestions for.
        cfg : dict
            Full EOS config dict.  Creativity settings are read from cfg["creativity"].
        domain : str
            The invocation domain (controls which output format hint is used).
        context : str
            Optional extra context to inject into the creativity prompt.
        """
        t0       = time.time()
        ccfg     = self._get_cfg(cfg)
        endpoint = self._topology.creativity_endpoint()

        if endpoint is None:
            logger.debug("[Creativity] Server not available — returning empty artifact.")
            return CreativityArtifact(degraded=True)

        mode          = self._output_structure_mode(ccfg)
        sampling      = self._sampling_params(ccfg)
        fmt_template  = self._format_template(ccfg)

        prompt = self._build_prompt(task, context, mode, domain, fmt_template)

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{endpoint}/v1/chat/completions",
                    json={
                        "model":       "creativity",
                        "messages":    [{"role": "user", "content": prompt}],
                        "temperature": sampling["temperature"],
                        "top_p":       sampling["top_p"],
                        "max_tokens":  sampling["max_tokens"],
                    },
                )
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"].strip()

        except httpx.ConnectError:
            logger.debug("[Creativity] Server not reachable — degraded.")
            return CreativityArtifact(degraded=True, elapsed_ms=_ms(t0))
        except Exception as exc:
            logger.warning("[Creativity] Call failed: %s", exc)
            return CreativityArtifact(degraded=True, elapsed_ms=_ms(t0))

        suggestions = self._parse_suggestions(raw, mode)
        elapsed     = _ms(t0)

        logger.debug(
            "[Creativity] domain=%s mode=%s suggestions=%d elapsed=%dms",
            domain, mode, len(suggestions), elapsed,
        )

        return CreativityArtifact(
            suggestions=suggestions,
            structure_mode=mode,
            degraded=False,
            elapsed_ms=elapsed,
        )

    # ── Prompt construction ────────────────────────────────────────────────────

    @staticmethod
    def _build_prompt(
        task: str,
        context: str,
        mode: str,
        domain: str,
        fmt_template: str | None,
    ) -> str:
        """Construct the creativity invocation prompt."""
        if fmt_template:
            # User-supplied template — substitute variables and return
            return (
                fmt_template
                .replace("{task}", task)
                .replace("{context}", context)
                .replace("{domain}", domain)
                .replace("{mode}", mode)
            )

        if mode == "structured":
            format_instruction = (
                "Return your response as a numbered list of 2–4 concrete suggestions. "
                "Each suggestion must be on its own line, prefixed with a number and period. "
                "No prose paragraphs."
            )
        elif mode == "semi-structured":
            format_instruction = (
                "Return your response as 2–4 short paragraphs. "
                "Each paragraph should present one divergent angle or alternative. "
                "Be concise."
            )
        else:  # loose
            format_instruction = (
                "Respond freely.  Prioritise novelty, metaphor, and unexpected angles. "
                "There is no required structure."
            )

        domain_hint = {
            DOMAIN_REASONING:   "Focus on alternative reasoning paths, hidden assumptions, and unexpected framings.",
            DOMAIN_AUTONOMOUS:  "Focus on proactive ideas, self-initiated directions, and unexplored goals.",
            DOMAIN_EXPLANATION: "Focus on analogies, metaphors, and non-obvious ways to explain this concept.",
            DOMAIN_BRAINSTORM:  "Focus on creative solutions, unconventional approaches, and divergent design directions.",
            DOMAIN_STUCK:       "Focus on breaking impasses: reframe the problem, challenge constraints, try lateral approaches.",
        }.get(domain, "Focus on divergent, non-obvious alternatives.")

        ctx_block = f"\n\nContext:\n{context.strip()}" if context.strip() else ""

        return (
            f"You are a divergent thinking assistant. Your role is advisory only — "
            f"you propose, reinterpret, and expand; you do not decide or authorise.\n\n"
            f"Task: {task}{ctx_block}\n\n"
            f"{domain_hint}\n\n"
            f"{format_instruction}"
        )

    @staticmethod
    def _parse_suggestions(raw: str, mode: str) -> list[str]:
        """Parse the raw creativity output into a list of suggestion strings."""
        if mode == "structured":
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            suggestions = []
            for line in lines:
                # Strip leading numbering (e.g. "1. ", "2) ")
                import re
                cleaned = re.sub(r"^\d+[\.\)]\s*", "", line).strip()
                if cleaned:
                    suggestions.append(cleaned)
            return suggestions[:4] if suggestions else [raw.strip()]
        else:
            # For loose / semi-structured, return the whole text as a single suggestion
            return [raw.strip()] if raw.strip() else []


# ── Singleton accessor ─────────────────────────────────────────────────────────

_service: CreativityService | None = None


def init_creativity_service(topology: Any) -> CreativityService:
    """Initialise the module-level CreativityService singleton. Call once at boot."""
    global _service
    _service = CreativityService(topology)
    logger.info("[Creativity] Service initialised.")
    return _service


def get_creativity_service() -> CreativityService | None:
    """Return the singleton, or None if not yet initialised."""
    return _service


# ── Utility ────────────────────────────────────────────────────────────────────

def _ms(t0: float) -> int:
    return int((time.time() - t0) * 1000)
