"""
EOS — Initiative Engine
Proactive signal generator for autonomous entity behavior.

The engine evaluates the entity's current state and queues initiative actions
when the autonomy dimension "initiative" is enabled.  It never blocks the
main cognitive loop — all heavy work is dispatched to the background thinking
worker (port 8083).

Signal collection
-----------------
Signals are collected from four sources:
  1. Idle time — no interaction in a while → self_reflection
  2. Turn count — periodic session checkpoints → session_checkpoint
  3. Memory pressure — high retrieval activity → memory_consolidation
  4. Identity stability — domains unsettled → identity_probe

Signal selection
----------------
Signals are ranked by priority (high → medium → low) and the top candidate
is queued, subject to:
  - Cooldown: same type cannot be queued twice within cooldown_seconds
  - Queue depth cap: at most max_queue_depth items waiting

Execution
---------
Queued initiatives are executed by calling
``execute_queued(topology, cfg, tracer, bus)`` which routes each ready
item through ``orchestrator.think_for_background`` (the QWEN layer),
never directly to the thinking worker.

Governance
----------
`can("initiative")` from core.autonomy is the master switch — the engine
will collect/queue only if this returns True.  Per-item consent can be
required via ``require_consent=True`` in the config.

Admin API (used by server.py)
------------------------------
  get_queue()           → list[dict]     current queue
  get_status()          → dict           engine health snapshot
  apply_feedback(id, f) → dict           accept / defer / dismiss
  trigger_eval(…)       → dict           manual evaluation cycle
  clear_queue()         → None           flush queue
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from runtime.exception_observability import observe_exception

if TYPE_CHECKING:
    from runtime.topology import RuntimeTopology

logger = logging.getLogger("eos.initiative_engine")
UTC = timezone.utc


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# ── Priority ranking ──────────────────────────────────────────────────────────

_PRIORITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


# ── Initiative signal dataclass ───────────────────────────────────────────────

class InitiativeSignal:
    __slots__ = ("source", "initiative_type", "priority", "rationale")

    def __init__(
        self,
        source: str,
        initiative_type: str,
        priority: str,
        rationale: str,
    ) -> None:
        self.source         = source
        self.initiative_type = initiative_type
        self.priority       = priority
        self.rationale      = rationale

    def as_dict(self) -> dict[str, str]:
        return {
            "source":           self.source,
            "initiative_type":  self.initiative_type,
            "priority":         self.priority,
            "rationale":        self.rationale,
        }


# ── Engine ────────────────────────────────────────────────────────────────────

class InitiativeEngine:
    """Deterministic initiative evaluator for the entity's proactive behavior.

    All state is in-memory (no external DB dependency).  The engine is
    intentionally simple — its role is candidate generation and queue
    management, not execution.
    """

    def __init__(self, cfg: dict) -> None:
        init_cfg = cfg.get("initiative", {})
        self._cooldown_seconds: int  = int(init_cfg.get("cooldown_seconds", 300))
        self._max_queue_depth:  int  = int(init_cfg.get("max_queue_depth", 10))
        self._require_consent:  bool = bool(init_cfg.get("require_consent", False))
        self._idle_threshold_s: float = float(init_cfg.get("idle_threshold_seconds", 300))

        # Runtime state
        self._queue: list[dict[str, Any]] = []
        self._last_queued_by_type: dict[str, str] = {}  # type → ISO timestamp
        self._turn_count:   int   = 0
        self._last_eval_at: float = 0.0
        self._last_interaction_at: float = time.time()
        self._eval_count:   int   = 0

    # ── Turn tracking ─────────────────────────────────────────────────────────

    def notify_turn(self) -> None:
        """Call after each completed conversation turn."""
        self._turn_count += 1
        self._last_interaction_at = time.time()

    # ── Public API ────────────────────────────────────────────────────────────

    def evaluate(
        self,
        *,
        memory_retrieved_count: int = 0,
        attention_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Collect signals, select top candidate, queue it if eligible.

        Returns the evaluation result dict.  Does NOT execute anything.
        Caller is responsible for checking the autonomy gate before calling.
        """
        signals = self._collect_signals(memory_retrieved_count=memory_retrieved_count)
        selected = self._select_signal(signals, attention_summary=attention_summary)
        now = _iso_now()
        self._last_eval_at = time.time()
        self._eval_count  += 1

        enacted = None
        suppression_reason = None

        if selected is not None:
            if len(self._queue) >= self._max_queue_depth:
                suppression_reason = "max_queue_depth_reached"
            elif self._is_cooldown_active(selected.initiative_type):
                suppression_reason = "cooldown_active"
            else:
                # Replace existing queued item of same type
                existing = self._find_pending_by_type(selected.initiative_type)
                if existing is not None:
                    existing.update({
                        "priority":           selected.priority,
                        "rationale":          selected.rationale,
                        "scheduled_at":       now,
                        "source":             selected.source,
                        "status":             "queued",
                        "last_replaced_at":   now,
                    })
                    enacted = existing
                else:
                    enacted = {
                        "initiative_id":     "INI-" + uuid.uuid4().hex[:8],
                        "initiative_type":   selected.initiative_type,
                        "priority":          selected.priority,
                        "rationale":         selected.rationale,
                        "scheduled_at":      now,
                        "source":            selected.source,
                        "status": "awaiting_consent" if self._require_consent else "queued",
                    }
                    self._queue.append(enacted)

                if enacted is not None:
                    self._last_queued_by_type[selected.initiative_type] = now

        return {
            "evaluated_at":     now,
            "eval_count":       self._eval_count,
            "signal_count":     len(signals),
            "signals":          [s.as_dict() for s in signals],
            "attention_bias":   dict((attention_summary or {}).get("initiative_bias") or {}),
            "selected":         enacted,
            "suppression_reason": suppression_reason,
            "queue_depth":      len(self._queue),
        }

    async def execute_queued(
        self,
        topology: "RuntimeTopology",
        cfg: dict,
        *,
        tracer=None,
        bus=None,
        entity_snapshot: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Execute all 'queued' (or 'ready_for_execution') items non-blockingly.

        Each item is proposed to the QWEN orchestrator layer, which routes the
        request through its ThinkingFaculty.  The engine never calls the
        thinking worker directly — authority remains with QWEN.
        Returns list of dispatched item dicts.
        """
        from runtime.orchestrator import think_for_background
        from core.autonomy import can

        if not can("initiative"):
            return []

        dispatched = []
        for item in list(self._queue):
            if item.get("status") not in ("queued", "ready_for_execution"):
                continue

            item["status"] = "dispatched"
            item["dispatched_at"] = _iso_now()

            task = (
                f"Entity initiative task: {item['initiative_type']}\n"
                f"Rationale: {item['rationale']}\n"
                f"Priority: {item['priority']}\n\n"
                "Perform this initiative task thoughtfully. "
                "If it's a reflection task, reflect on recent interactions. "
                "If it's a consolidation task, synthesize key memories. "
                "If it's a checkpoint, assess current engagement and goals. "
                "Be concise and purposeful."
            )
            if entity_snapshot is not None:
                task = f"{entity_snapshot.background_context_block()}\n\n---\n{task}"

            def _on_complete(result: str, _item: dict = item) -> None:
                _item["status"] = "completed"
                _item["completed_at"] = _iso_now()
                _item["result_summary"] = result[:300]
                logger.info(
                    "[InitiativeEngine] Item %s complete: %s…",
                    _item.get("initiative_id", "?"),
                    result[:80],
                )
                if bus:
                    try:
                        from runtime.signal_bus import SignalEnvelope, SEVERITY_INFO
                        bus.publish(SignalEnvelope(
                            source="initiative_engine",
                            signal_type="initiative_completed",
                            severity=SEVERITY_INFO,
                            confidence=0.9,
                            payload={
                                "initiative_id":   _item.get("initiative_id"),
                                "initiative_type": _item.get("initiative_type"),
                                "result_summary":  result[:200],
                            },
                        ))
                    except Exception as exc:
                        observe_exception(
                            logger=logger,
                            subsystem="initiative_engine",
                            operation="publish initiative completion signal",
                            exc=exc,
                            level=logging.WARNING,
                            context={
                                "initiative_id": _item.get("initiative_id"),
                                "initiative_type": _item.get("initiative_type"),
                            },
                        )

            asyncio.create_task(
                think_for_background(
                    topology,
                    task,
                    on_complete=_on_complete,
                    entity_snapshot=entity_snapshot,
                )
            )
            dispatched.append(item)

        return dispatched

    def get_queue(self) -> list[dict[str, Any]]:
        """Return a copy of the current initiative queue."""
        return [dict(item) for item in self._queue]

    def current_focus(self) -> dict[str, Any] | None:
        """Return the most relevant initiative item as a current-focus record."""
        status_rank = {
            "dispatched": 0,
            "ready_for_execution": 1,
            "queued": 2,
            "awaiting_consent": 3,
            "deferred": 4,
        }
        eligible = [
            dict(item)
            for item in self._queue
            if item.get("status") in status_rank
        ]
        if not eligible:
            return None

        eligible.sort(
            key=lambda item: (
                status_rank.get(str(item.get("status")), 99),
                _PRIORITY_RANK.get(str(item.get("priority")), 99),
                str(item.get("scheduled_at", "")),
            )
        )
        item = eligible[0]
        raw_status = str(item.get("status") or "queued")
        status_map = {
            "dispatched": "active",
            "ready_for_execution": "active",
            "queued": "active",
            "awaiting_consent": "waiting",
            "deferred": "waiting",
        }
        next_action_map = {
            "dispatched": "Finish the initiative task and record its result.",
            "ready_for_execution": "Execute the approved initiative task.",
            "queued": "Execute the queued initiative task.",
            "awaiting_consent": "Wait for consent or admin feedback before execution.",
            "deferred": "Revisit this initiative when conditions improve.",
        }
        title = str(item.get("initiative_type") or "initiative").replace("_", " ").strip().title()
        return {
            "focus_id": item.get("initiative_id") or "initiative-current",
            "title": title,
            "why_now": str(item.get("rationale") or "An initiative candidate is currently queued."),
            "next_action": next_action_map.get(raw_status, "Review the initiative queue."),
            "status": status_map.get(raw_status, "waiting"),
            "source": "initiative",
            "updated_at": str(
                item.get("dispatched_at")
                or item.get("feedback_at")
                or item.get("scheduled_at")
                or _iso_now()
            ),
            "metadata": item,
        }

    def get_status(self) -> dict[str, Any]:
        """Return engine health snapshot for admin API."""
        return {
            "enabled":            True,  # caller checks can("initiative")
            "turn_count":         self._turn_count,
            "eval_count":         self._eval_count,
            "queue_depth":        len(self._queue),
            "last_eval_at":       self._last_eval_at,
            "last_interaction_at": self._last_interaction_at,
            "idle_seconds":       round(time.time() - self._last_interaction_at, 1),
            "cooldown_seconds":   self._cooldown_seconds,
            "require_consent":    self._require_consent,
            "queue": self.get_queue(),
        }

    def apply_feedback(
        self,
        initiative_id: str,
        feedback: str,
    ) -> dict[str, Any]:
        """Apply accept / defer / dismiss feedback to a queued item.

        accept  → ready_for_execution
        defer   → deferred  (remains in queue, skipped on next execute pass)
        dismiss → dismissed (removed from queue)
        """
        valid = {"accept", "defer", "dismiss"}
        if feedback not in valid:
            return {"ok": False, "error": f"feedback must be one of {sorted(valid)}"}

        status_map = {
            "accept":  "ready_for_execution",
            "defer":   "deferred",
            "dismiss": "dismissed",
        }

        for item in self._queue:
            if item.get("initiative_id") == initiative_id:
                if feedback == "dismiss":
                    self._queue.remove(item)
                else:
                    item["status"] = status_map[feedback]
                    item["user_feedback"] = feedback
                    item["feedback_at"] = _iso_now()
                return {"ok": True, "initiative_id": initiative_id, "feedback": feedback}

        return {"ok": False, "error": f"initiative '{initiative_id}' not found"}

    def trigger_eval(
        self,
        *,
        memory_retrieved_count: int = 0,
        rationale: str = "manual admin trigger",
    ) -> dict[str, Any]:
        """Manually trigger an evaluation cycle (admin use)."""
        # Inject a manual signal with high priority
        manual = InitiativeSignal(
            source="admin",
            initiative_type="admin_triggered",
            priority="high",
            rationale=rationale,
        )
        now = _iso_now()
        self._eval_count += 1
        item = {
            "initiative_id":   "INI-" + uuid.uuid4().hex[:8],
            "initiative_type": "admin_triggered",
            "priority":        "high",
            "rationale":       rationale,
            "scheduled_at":    now,
            "source":          "admin",
            "status":          "queued",
        }
        self._queue.append(item)
        return {
            "ok":         True,
            "enacted":    item,
            "queue_depth": len(self._queue),
        }

    def clear_queue(self) -> None:
        """Flush all items from the queue."""
        self._queue.clear()

    # ── Signal collection ─────────────────────────────────────────────────────

    def _collect_signals(
        self,
        *,
        memory_retrieved_count: int,
    ) -> list[InitiativeSignal]:
        signals: list[InitiativeSignal] = []
        idle_s = time.time() - self._last_interaction_at

        # 1. Idle self-reflection (entity wants to think when quiet)
        if idle_s >= self._idle_threshold_s:
            signals.append(InitiativeSignal(
                source="idle_timer",
                initiative_type="self_reflection",
                priority="medium",
                rationale=(
                    f"No interaction for {idle_s:.0f}s — "
                    "idle cognition opportunity for self-reflection."
                ),
            ))

        # 2. Periodic session checkpoint
        if self._turn_count >= 5 and self._turn_count % 5 == 0:
            signals.append(InitiativeSignal(
                source="turn_counter",
                initiative_type="session_checkpoint",
                priority="low",
                rationale=(
                    f"Turn {self._turn_count}: periodic checkpoint "
                    "to assess goals and engagement."
                ),
            ))

        # 3. Memory consolidation (high retrieval volume)
        if memory_retrieved_count >= 3:
            signals.append(InitiativeSignal(
                source="memory_pressure",
                initiative_type="memory_consolidation",
                priority="medium",
                rationale=(
                    f"High retrieval volume ({memory_retrieved_count} items) "
                    "suggests consolidation or thematic synthesis is valuable."
                ),
            ))

        # 4. Identity probe (after significant interaction volume)
        if self._turn_count > 0 and self._turn_count % 15 == 0:
            signals.append(InitiativeSignal(
                source="identity_monitor",
                initiative_type="identity_probe",
                priority="high",
                rationale=(
                    f"Turn {self._turn_count}: prompt identity coherence check "
                    "— does behavior align with self-model?"
                ),
            ))

        return signals

    def _select_signal(
        self,
        signals: list[InitiativeSignal],
        *,
        attention_summary: dict[str, Any] | None = None,
    ) -> InitiativeSignal | None:
        if not signals:
            return None
        initiative_bias = dict((attention_summary or {}).get("initiative_bias") or {})
        return sorted(
            signals,
            key=lambda s: (
                _PRIORITY_RANK.get(s.priority, 99) - (float(initiative_bias.get(s.initiative_type, 0.0) or 0.0) * 2.0),
                s.initiative_type,
            ),
        )[0]

    def _is_cooldown_active(self, initiative_type: str) -> bool:
        if self._cooldown_seconds == 0:
            return False
        last = self._last_queued_by_type.get(initiative_type)
        if not last:
            return False
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            elapsed = (datetime.now(UTC) - last_dt).total_seconds()
            return elapsed < self._cooldown_seconds
        except Exception:
            return False

    def _find_pending_by_type(
        self, initiative_type: str
    ) -> dict[str, Any] | None:
        for item in self._queue:
            if (
                item.get("status") in ("queued", "awaiting_consent")
                and item.get("initiative_type") == initiative_type
            ):
                return item
        return None
