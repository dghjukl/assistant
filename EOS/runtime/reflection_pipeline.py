"""
EOS — Reflection Pipeline
Scheduled runner that dispatches identity evaluation cycles to the background
thinking worker and records results into the CognitionTracer + SignalBus.

Design rules (from RUNTIME_INVARIANTS):
  - Non-blocking: never holds the event loop.
  - Qwen3 is the only reasoner for identity eval (routes through primary).
  - Port 8083 (LFM2.5-Thinking) may be used for background prep tasks, but
    the identity eval conclusion always comes from Qwen3 on the primary.
  - Config is the single source of truth; all intervals read from cfg.

Usage (called by server.py startup):
    from runtime.reflection_pipeline import ReflectionPipeline

    pipeline = ReflectionPipeline(cfg)
    task = asyncio.create_task(pipeline.run_loop(topology, tracer=tracer, bus=bus))
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from runtime.topology import RuntimeTopology

logger = logging.getLogger("eos.reflection_pipeline")


class ReflectionPipeline:
    """Runs identity and relational evaluation cycles on a configurable schedule.

    Two scheduling modes (configured via cfg["cognition"]):
      - interval_seconds (float): time-based schedule (default 900 = 15 min)
      - interval_turns   (int):   turn-count-based schedule (default 20)

    Whichever threshold fires first triggers an identity eval cycle. After each
    eval, both counters reset.

    Relational cycles run at a separate (lower) frequency:
      - relational_interval_turns   (default 40)
      - relational_interval_seconds (default 1800)
    """

    def __init__(self, cfg: dict) -> None:
        cognition_cfg = cfg.get("cognition", {})
        self._interval_seconds: float = float(
            cognition_cfg.get("reflection_interval_seconds", 900)
        )
        self._interval_turns: int = int(
            cognition_cfg.get("reflection_interval_turns", 20)
        )
        # Relational eval runs less often
        self._relational_interval_seconds: float = float(
            cognition_cfg.get("relational_interval_seconds", 1800)
        )
        self._relational_interval_turns: int = int(
            cognition_cfg.get("relational_interval_turns", 40)
        )
        self._turn_counter: int = 0
        self._last_run_time: float = 0.0
        self._last_relational_run_time: float = 0.0
        self._running: bool = False
        self._cfg = cfg

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def notify_turn(self) -> None:
        """Call once per completed conversation turn to track turn count."""
        self._turn_counter += 1

    async def run_once(
        self,
        topology: "RuntimeTopology",
        tracer=None,
        bus=None,
        entity_snapshot=None,
    ) -> dict:
        """Run a single identity evaluation cycle and record results.

        Returns the raw eval results dict from identity.run_evaluation_cycle(),
        or an error dict if the eval failed.
        """
        from core.identity import run_evaluation_cycle, request_self_name
        from core.memory import get_relational_model, get_entity_name

        logger.info("[ReflectionPipeline] Starting identity eval cycle...")
        reflection_id = "RF-" + uuid.uuid4().hex[:8]

        try:
            # Attach continuity monitor if available (lazy import to avoid cycles)
            continuity_monitor = None
            try:
                import webui.server as _srv  # type: ignore
                continuity_monitor = getattr(_srv, "_identity_continuity", None)
            except Exception:
                pass

            results = await run_evaluation_cycle(
                primary_endpoint=topology.primary_endpoint(),
                signal_bus=bus,
                cfg=self._cfg,
                continuity_monitor=continuity_monitor,
                snapshot_context=(
                    entity_snapshot.background_context_block()
                    if entity_snapshot is not None else ""
                ),
            )
            logger.info(
                "[ReflectionPipeline] Cycle %d complete (id=%s)",
                results.get("cycle", 0),
                reflection_id,
            )

            # ── Record in tracer ───────────────────────────────────────────
            if tracer:
                try:
                    domain_conclusions = [
                        f"{domain}: confidence={data.get('confidence', 0):.2f}"
                        for domain, data in results.get("domains", {}).items()
                    ]
                    tracer.record_reflection({
                        "reflection_id":   reflection_id,
                        "trigger":         "scheduled",
                        "inputs_reviewed": [
                            f"domain:{d}" for d in results.get("domains", {}).keys()
                        ],
                        "conclusions":  domain_conclusions,
                        "suggestions":  results.get("suggestions", []),
                        "similar_before": results.get("cycle", 0) > 1,
                        "cycle":        results.get("cycle", 0),
                        "stable_domains": results.get("stable_domains", []),
                    })
                except Exception as exc:
                    logger.debug("Tracer record failed: %s", exc)

            # ── Publish to signal bus ──────────────────────────────────────
            if bus:
                try:
                    from runtime.signal_bus import (
                        SignalEnvelope,
                        STYPE_REFLECTION,
                        SEVERITY_INFO,
                    )
                    bus.publish(SignalEnvelope(
                        source="reflection_pipeline",
                        signal_type=STYPE_REFLECTION,
                        severity=SEVERITY_INFO,
                        confidence=0.8,
                        correlation_key=f"reflection:{reflection_id}",
                        payload={
                            "reflection_id": reflection_id,
                            "cycle":         results.get("cycle", 0),
                            "domains": {
                                d: v.get("confidence", 0)
                                for d, v in results.get("domains", {}).items()
                            },
                            "stable_domains": results.get("stable_domains", []),
                        },
                    ))
                except Exception as exc:
                    logger.debug("Bus publish failed: %s", exc)

            # ── Check naming condition ─────────────────────────────────────
            try:
                rel = get_relational_model()
                if rel.get("naming_condition_met") and not get_entity_name():
                    name = await request_self_name(topology.primary_endpoint())
                    if name:
                        logger.info(
                            "[ReflectionPipeline] Entity chose name: %s", name
                        )
                        if bus:
                            from runtime.signal_bus import SignalEnvelope, SEVERITY_HIGH
                            bus.publish(SignalEnvelope(
                                source="reflection_pipeline",
                                signal_type="entity_named",
                                severity=SEVERITY_HIGH,
                                confidence=1.0,
                                payload={"name": name},
                            ))
            except Exception as exc:
                logger.debug("Naming check failed: %s", exc)

            # ── Inject focus signal on name review ────────────────────────
            # (also done via signal bus subscriber, but belt-and-suspenders
            #  in case the bus subscriber fires after this turn's prompt)
            try:
                if results.get("name_review_warranted"):
                    from runtime.orchestrator import _focus
                    if _focus is not None:
                        from runtime.focus_engine import FocusEngine as _FE
                        _focus.update([
                            _FE.signal_from_initiative(
                                "identity review: reflect on who I have become — "
                                "does my current name still fit?"
                            )
                        ])
            except Exception as exc:
                logger.debug("Focus name-review injection failed: %s", exc)

            return results

        except Exception as exc:
            logger.error("[ReflectionPipeline] Eval failed: %s", exc)
            return {"error": str(exc), "reflection_id": reflection_id}
        finally:
            # Reset counters regardless of success
            import time
            self._turn_counter = 0
            self._last_run_time = __import__("time").time()

    async def run_relational_once(
        self,
        topology: "RuntimeTopology",
        bus=None,
        entity_snapshot=None,
    ) -> dict:
        """Run a single relational evaluation cycle and return results."""
        import time
        logger.info("[ReflectionPipeline] Starting relational eval cycle...")
        try:
            from core.relational import run_relational_cycle
            results = await run_relational_cycle(
                primary_endpoint=topology.primary_endpoint(),
                cfg=self._cfg,
                signal_bus=bus,
            )
            logger.info(
                "[ReflectionPipeline] Relational cycle complete (%s).",
                "skipped" if results.get("skipped") else f"cycle {results.get('cycle', '?')}",
            )
            return results
        except Exception as exc:
            logger.error("[ReflectionPipeline] Relational eval failed: %s", exc)
            return {"error": str(exc)}
        finally:
            import time as _t
            self._last_relational_run_time = _t.time()

    async def run_loop(
        self,
        topology: "RuntimeTopology",
        tracer=None,
        bus=None,
        entity_state_service=None,
    ) -> None:
        """Long-running coroutine: wakes periodically and fires evals as needed.

        Checks every 60 seconds. Identity eval fires when either:
          - interval_seconds has elapsed since the last eval, OR
          - interval_turns turns have been completed since the last eval.

        Relational eval fires on its own lower-frequency schedule
        (relational_interval_seconds / relational_interval_turns).

        Safe to cancel: uses asyncio.CancelledError for clean exit.
        """
        import time

        self._running = True
        now = time.time()
        self._last_run_time = now
        self._last_relational_run_time = now
        logger.info(
            "[ReflectionPipeline] Loop started "
            "(identity: %.0fs/%d turns | relational: %.0fs/%d turns)",
            self._interval_seconds, self._interval_turns,
            self._relational_interval_seconds, self._relational_interval_turns,
        )

        try:
            while True:
                await asyncio.sleep(60)  # check every 60 s

                if not self._running:
                    break

                now    = time.time()
                turns  = self._turn_counter

                # ── Identity eval ────────────────────────────────────────
                id_time_due  = (now - self._last_run_time) >= self._interval_seconds
                id_turns_due = turns >= self._interval_turns

                if id_time_due or id_turns_due:
                    reason = (
                        f"time ({now - self._last_run_time:.0f}s)" if id_time_due
                        else f"turns ({turns})"
                    )
                    logger.info(
                        "[ReflectionPipeline] Triggering identity eval (reason: %s)", reason
                    )
                    snapshot = (
                        entity_state_service.build_snapshot(
                            scope="background",
                            source="reflection.identity",
                            metadata={"reason": reason},
                        )
                        if entity_state_service is not None else None
                    )
                    await self.run_once(
                        topology, tracer=tracer, bus=bus, entity_snapshot=snapshot
                    )

                # ── Relational eval (lower frequency) ────────────────────
                rel_time_due  = (now - self._last_relational_run_time) >= self._relational_interval_seconds
                rel_turns_due = turns >= self._relational_interval_turns

                if rel_time_due or rel_turns_due:
                    rel_reason = (
                        f"time ({now - self._last_relational_run_time:.0f}s)" if rel_time_due
                        else f"turns ({turns})"
                    )
                    logger.info(
                        "[ReflectionPipeline] Triggering relational eval (reason: %s)", rel_reason
                    )
                    snapshot = (
                        entity_state_service.build_snapshot(
                            scope="background",
                            source="reflection.relational",
                            metadata={"reason": rel_reason},
                        )
                        if entity_state_service is not None else None
                    )
                    await self.run_relational_once(
                        topology, bus=bus, entity_snapshot=snapshot
                    )

        except asyncio.CancelledError:
            logger.info("[ReflectionPipeline] Loop cancelled — shutting down.")
            self._running = False
            raise

        except Exception as exc:
            logger.error("[ReflectionPipeline] Unexpected loop error: %s", exc)
            self._running = False

    def stop(self) -> None:
        """Signal the loop to stop on its next wake cycle."""
        self._running = False
