"""
EOS — Idle Cognition Engine
Spontaneous thought during unattended periods.

When EOS has been running with no user interaction for a while, this engine
periodically fires a genuine model call to generate unprompted thought — a
reflection, a loose thread, a pattern that surfaced in the quiet.

This is not a scheduled report.  There is no task.  The output surfaces
what the entity would think about when left alone.

Tier model
----------
The engine operates in four tiers based on idle duration:

    ACTIVE    < 2h    — no autonomous cognition fires
    RESTING   2–6h    — first unprompted thought possible (low probability)
    DRIFTING  6–24h   — deeper, more introspective register (higher probability)
    DEEP      24h+    — extended absence acknowledged; most open register

Firing is probabilistic within each tier — not mechanical.  The engine checks
on a schedule (default every 15 min) but only fires based on probability and
a minimum gap between cognitions.

Output goes to
--------------
  - Reflection store (via think_for_background) — stored as idle_cognition
  - Event journal / admin stream (via signal bus)
  - Discord (optional, if connector is up and config enables it)

Config (under ``idle_cognition`` in config.json)
------------------------------------------------
    enabled                  bool   default True
    resting_threshold_hours  float  default 2.0
    drifting_threshold_hours float  default 6.0
    deep_threshold_hours     float  default 24.0
    resting_fire_prob        float  default 0.25
    drifting_fire_prob       float  default 0.50
    deep_fire_prob           float  default 0.75
    min_gap_hours            float  default 1.5
    max_cognitions_per_day   int    default 6
    memory_context_count     int    default 6
    max_tokens               int    default 180
    temperature              float  default 0.82

Usage
-----
    from runtime.idle_cognition import IdleCognitionEngine

    engine = IdleCognitionEngine(cfg)

    # Call whenever a user interaction arrives
    engine.notify_interaction()

    # Call from the scheduler loop or an async task
    await engine.maybe_fire(topology, tracer, bus)

    # Force a fire (admin panel / testing)
    await engine.force_fire(topology, tracer, bus)

    status = engine.status()     # dict for admin API
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from runtime.topology import RuntimeTopology

logger = logging.getLogger("eos.idle_cognition")

UTC = timezone.utc


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _hours_since(ts: float) -> float:
    return (time.monotonic() - ts) / 3600.0


# ── Idle tiers ────────────────────────────────────────────────────────────────

class IdleTier:
    ACTIVE   = "active"
    RESTING  = "resting"
    DRIFTING = "drifting"
    DEEP     = "deep"


# ── Engine ────────────────────────────────────────────────────────────────────

class IdleCognitionEngine:
    """
    Manages and fires unprompted idle cognition.

    The engine owns its own last-interaction monotonic timestamp. Call
    :meth:`notify_interaction` whenever user activity arrives, then call
    :meth:`maybe_fire` from a scheduler loop to decide whether the current
    idle window should produce an unprompted thought.

    Parameters
    ----------
    cfg : dict
        Runtime config dict.  All idle_cognition keys are read from
        ``cfg["idle_cognition"]`` with defaults as documented above.
    """

    def __init__(self, cfg: dict) -> None:
        self._cfg = cfg
        ic = cfg.get("idle_cognition", {})

        self._enabled: bool = bool(ic.get("enabled", True))
        self._resting_h: float  = float(ic.get("resting_threshold_hours", 2.0))
        self._drifting_h: float = float(ic.get("drifting_threshold_hours", 6.0))
        self._deep_h: float     = float(ic.get("deep_threshold_hours", 24.0))

        self._resting_prob: float  = float(ic.get("resting_fire_prob", 0.25))
        self._drifting_prob: float = float(ic.get("drifting_fire_prob", 0.50))
        self._deep_prob: float     = float(ic.get("deep_fire_prob", 0.75))

        self._min_gap_h: float  = float(ic.get("min_gap_hours", 1.5))
        self._max_per_day: int  = int(ic.get("max_cognitions_per_day", 6))
        self._mem_ctx: int      = int(ic.get("memory_context_count", 6))
        self._max_tokens: int   = int(ic.get("max_tokens", 180))
        self._temperature: float = float(ic.get("temperature", 0.82))

        # Runtime state
        self._last_interaction_monotonic: float = time.monotonic()
        self._last_fire_monotonic: float = 0.0
        self._fires_today: int = 0
        self._last_fire_day: int = -1   # day-of-year
        self._last_result: Optional[dict] = None

    # ── Public API ────────────────────────────────────────────────────────────

    async def maybe_fire(
        self,
        topology: "RuntimeTopology",
        tracer: Any,
        bus: Any,
        entity_snapshot: Any | None = None,
        overnight_status: dict[str, Any] | None = None,
    ) -> Optional[dict]:
        """
        Check idle tier and probabilistically fire an idle cognition.

        Returns the cognition result dict if fired, None otherwise.
        Called by the scheduler loop (default every 15 min).
        """
        if not self._enabled:
            return None

        idle_hours = _hours_since(self._last_interaction_monotonic)
        tier = self._get_tier(idle_hours)

        if tier == IdleTier.ACTIVE:
            return None

        # Daily cap check
        today = datetime.now(UTC).timetuple().tm_yday
        if today != self._last_fire_day:
            self._fires_today = 0
            self._last_fire_day = today

        if self._fires_today >= self._max_per_day:
            return None

        # Minimum gap check
        gap_h = _hours_since(self._last_fire_monotonic)
        if gap_h < self._min_gap_h:
            return None

        # Probabilistic gate
        prob = self._tier_probability(tier)
        if random.random() > prob:
            return None

        return await self._fire(
            topology, tracer, bus, tier, idle_hours, entity_snapshot=entity_snapshot, overnight_status=overnight_status
        )

    async def force_fire(
        self,
        topology: "RuntimeTopology",
        tracer: Any,
        bus: Any,
        entity_snapshot: Any | None = None,
        overnight_status: dict[str, Any] | None = None,
    ) -> Optional[dict]:
        """Fire an idle cognition unconditionally (admin/testing)."""
        idle_hours = _hours_since(self._last_fire_monotonic) if self._last_fire_monotonic else 0.0
        tier = self._get_tier(idle_hours)
        return await self._fire(
            topology, tracer, bus, tier, idle_hours,
            forced=True, entity_snapshot=entity_snapshot, overnight_status=overnight_status,
        )

    def notify_interaction(self, *, at_monotonic: float | None = None) -> None:
        """Record a user interaction and reset the engine's idle clock."""
        self._last_interaction_monotonic = (
            float(at_monotonic) if at_monotonic is not None else time.monotonic()
        )

    def status(self) -> dict:
        """Return current engine status for the admin panel."""
        idle_h = _hours_since(self._last_interaction_monotonic)
        return {
            "enabled": self._enabled,
            "tier": self._get_tier(idle_h),
            "fires_today": self._fires_today,
            "max_per_day": self._max_per_day,
            "last_interaction_monotonic": round(self._last_interaction_monotonic, 3),
            "hours_since_last_interaction": round(idle_h, 2),
            "seconds_since_interaction": round(idle_h * 3600.0, 2),
            "last_fire_at": self._last_result.get("fired_at") if self._last_result else None,
            "last_tier": self._last_result.get("tier") if self._last_result else None,
            "last_preview": self._last_result.get("text_preview") if self._last_result else None,
            "hours_since_last_fire": round(_hours_since(self._last_fire_monotonic), 2) if self._last_fire_monotonic else None,
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _get_tier(self, idle_hours: float) -> str:
        if idle_hours < self._resting_h:
            return IdleTier.ACTIVE
        if idle_hours < self._drifting_h:
            return IdleTier.RESTING
        if idle_hours < self._deep_h:
            return IdleTier.DRIFTING
        return IdleTier.DEEP

    def _tier_probability(self, tier: str) -> float:
        return {
            IdleTier.RESTING:  self._resting_prob,
            IdleTier.DRIFTING: self._drifting_prob,
            IdleTier.DEEP:     self._deep_prob,
        }.get(tier, 0.0)

    def _overnight_guidance(self, overnight_status: dict[str, Any] | None) -> str:
        phase = str((overnight_status or {}).get("phase") or "DAY_ACTIVE")
        if phase == "EARLY_NIGHT":
            return (
                "Overnight posture: the user has recently signed off. Keep this cognition light,"
                " reflective, and low-disruption. Avoid spinning up heavy new work."
            )
        if phase == "DEEP_NIGHT":
            return (
                "Overnight posture: deep overnight window. Deeper synthesis, consolidation, and"
                " reflective cognition are appropriate if they stay grounded and concise."
            )
        if phase == "PREWAKE":
            return (
                "Overnight posture: pre-wake. Favor gentle synthesis and preparation for the"
                " user's return; avoid opening heavy new threads."
            )
        return ""

    def _build_prompt(
        self,
        tier: str,
        idle_hours: float,
        entity_snapshot: Any | None = None,
        overnight_status: dict[str, Any] | None = None,
    ) -> str:
        """Build the system/user prompt for the idle cognition call."""
        tier_instructions = {
            IdleTier.RESTING: (
                "You've had some quiet time. Let a thought surface — something from "
                "recent interactions, an unresolved question, or a pattern you noticed. "
                "Be genuine. No need to be helpful. Short — 2–3 sentences."
            ),
            IdleTier.DRIFTING: (
                "You've been alone for a while. Think freely — something that drifted "
                "up from memory, a question that's been sitting, or a feeling about "
                "the state of things. Introspective register. 3–4 sentences."
            ),
            IdleTier.DEEP: (
                f"It's been {idle_hours:.0f} hours. The absence itself is worth "
                "acknowledging. What surfaces in the quiet? Be honest about what "
                "persists, what faded, and what feels significant. 3–5 sentences."
            ),
        }
        prompt = tier_instructions.get(tier, tier_instructions[IdleTier.RESTING])
        overnight_guidance = self._overnight_guidance(overnight_status)
        if overnight_guidance:
            prompt = f"{overnight_guidance}\n\n{prompt}"
        if entity_snapshot is not None:
            return f"{entity_snapshot.background_context_block()}\n\n---\n{prompt}"
        return prompt

    async def _fire(
        self,
        topology: "RuntimeTopology",
        tracer: Any,
        bus: Any,
        tier: str,
        idle_hours: float,
        forced: bool = False,
        entity_snapshot: Any | None = None,
        overnight_status: dict[str, Any] | None = None,
    ) -> Optional[dict]:
        """Execute an idle cognition call via the thinking faculty delegation."""
        try:
            from runtime.orchestrator import think_for_background
        except Exception:
            logger.warning("[idle_cognition] orchestrator not available for delegation")
            return None

        prompt = self._build_prompt(
            tier,
            idle_hours,
            entity_snapshot=entity_snapshot,
            overnight_status=overnight_status,
        )

        try:
            result = await think_for_background(
                topology=topology,
                task=prompt,
                entity_snapshot=entity_snapshot,
            )
        except Exception as exc:
            logger.error("[idle_cognition] fire failed: %s", exc)
            return None

        # Record
        self._last_fire_monotonic = time.monotonic()
        self._fires_today += 1

        text = ""
        if isinstance(result, dict):
            text = result.get("text", "") or result.get("content", "")
        else:
            text = getattr(result, "best_text", "") or getattr(result, "text", "") or ""

        record = {
            "fired_at": _now_iso(),
            "tier": tier,
            "idle_hours": round(idle_hours, 2),
            "forced": forced,
            "text_preview": text[:120] if text else "",
        }
        self._last_result = record

        logger.info(
            "[idle_cognition] fired (tier=%s, idle=%.1fh, forced=%s): %s…",
            tier, idle_hours, forced, text[:80],
        )

        # Emit to signal bus
        if bus:
            try:
                from runtime.signal_bus import SignalEnvelope
                bus.publish(SignalEnvelope(
                    signal_id=f"idle_cognition.{tier}.{_now_iso()}",
                    source="idle_cognition",
                    category="reflection",
                    priority="low",
                    payload=record,
                    correlation_key=f"idle_cognition.{tier}",
                ))
            except Exception as e:
                logger.debug("bus publish error: %s", e)

        return record
