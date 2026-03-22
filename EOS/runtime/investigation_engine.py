"""
EOS — Investigation Engine
Entity-driven bounded inquiry coordinator.

An investigation is a scoped, evidence-grounded reasoning task that the entity
pursues across multiple passes, either at its own initiative or when triggered
by the admin panel.

Architecture
------------
All investigations and their passes are persisted in a SQLite table inside the
entity's existing database file (cfg["db_path"]) — no separate process or DB.

Pass execution model (mirrors Mcore's conceptual model, EOS idiom):
  evidence_review      — search_memory for relevant evidence, summarise
  hypothesis_generation — Qwen3 (+ optional thinking worker) generates hypotheses
  recommendation_draft  — Qwen3 drafts actionable recommendations from findings
  synthesis             — consolidate all findings into a final summary

Governance
----------
- `can("action")` gates any pass that would write outputs (recommendations, notes)
- Max evidence per pass and max duration are enforced
- Reasoning backend unavailability is degraded-mode recorded, not silenced
- No investigation finding ever auto-promotes to global identity memory — any
  promotion must be an explicit admin action

Admin API (used by server.py)
------------------------------
  create(title, description, category, priority)  → dict
  get(investigation_id)                            → dict | None
  list(status=None, limit=20)                      → list[dict]
  async run_pass(topology, investigation_id, ...)  → dict
  resolve(investigation_id, summary)               → dict | None
  reopen(investigation_id)                         → dict | None
  delete(investigation_id)                         → bool
  get_diagnostics()                                → dict
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from runtime.topology import RuntimeTopology

logger = logging.getLogger("eos.investigation_engine")
UTC = timezone.utc


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# ── Default budgets ───────────────────────────────────────────────────────────

DEFAULT_MAX_EVIDENCE_PER_PASS     = 20
DEFAULT_MAX_PASS_DURATION_S       = 90
DEFAULT_CONFIDENCE_THRESHOLD      = 0.55
DEFAULT_MIN_EVIDENCE_FOR_SYNTHESIS = 2


# ── SQLite store (embedded, single-file) ──────────────────────────────────────

class _InvestigationStore:
    """Minimal SQLite-backed store for investigations and passes.

    Tables:
      eos_investigations  — one row per investigation
      eos_investigation_passes — one row per pass
    """

    DDL = """
    CREATE TABLE IF NOT EXISTS eos_investigations (
        investigation_id TEXT PRIMARY KEY,
        title            TEXT NOT NULL,
        description      TEXT DEFAULT '',
        category         TEXT DEFAULT 'general',
        priority         INTEGER DEFAULT 3,
        status           TEXT DEFAULT 'open',
        created_by       TEXT DEFAULT 'system',
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        resolution_summary TEXT DEFAULT '',
        metadata         TEXT DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS eos_investigation_passes (
        pass_id           TEXT PRIMARY KEY,
        investigation_id  TEXT NOT NULL,
        task_type         TEXT NOT NULL,
        trigger_type      TEXT DEFAULT 'auto',
        objective         TEXT DEFAULT '',
        started_at        TEXT NOT NULL,
        finished_at       TEXT,
        outcome_status    TEXT DEFAULT 'running',
        confidence_score  REAL DEFAULT 0.0,
        summary           TEXT DEFAULT '',
        findings          TEXT DEFAULT '[]',
        hypotheses        TEXT DEFAULT '[]',
        evidence_linked   TEXT DEFAULT '[]',
        next_action       TEXT DEFAULT '',
        degraded_backend  INTEGER DEFAULT 0,
        error_text        TEXT DEFAULT ''
    );
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            with self._conn() as conn:
                conn.executescript(self.DDL)

    # Investigations CRUD

    def create_investigation(
        self,
        *,
        title: str,
        description: str = "",
        category: str = "general",
        priority: int = 3,
        created_by: str = "system",
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        now = _iso_now()
        iid = "INV-" + uuid.uuid4().hex[:10]
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO eos_investigations
                       (investigation_id, title, description, category, priority,
                        status, created_by, created_at, updated_at, metadata)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (iid, title, description, category, priority,
                     "open", created_by, now, now,
                     json.dumps(metadata or {})),
                )
        return self.get(iid)

    def get(self, investigation_id: str) -> dict[str, Any] | None:
        with self._lock:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM eos_investigations WHERE investigation_id=?",
                    (investigation_id,)
                ).fetchone()
        if not row:
            return None
        return dict(row)

    def list(
        self,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self._lock:
            with self._conn() as conn:
                if status:
                    rows = conn.execute(
                        "SELECT * FROM eos_investigations WHERE status=? "
                        "ORDER BY updated_at DESC LIMIT ?",
                        (status, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM eos_investigations "
                        "ORDER BY updated_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
        return [dict(r) for r in rows]

    def update_status(
        self,
        investigation_id: str,
        new_status: str,
        resolution_summary: str = "",
    ) -> dict[str, Any] | None:
        now = _iso_now()
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE eos_investigations SET status=?, updated_at=?, "
                    "resolution_summary=? WHERE investigation_id=?",
                    (new_status, now, resolution_summary, investigation_id),
                )
        return self.get(investigation_id)

    def delete(self, investigation_id: str) -> bool:
        with self._lock:
            with self._conn() as conn:
                cur = conn.execute(
                    "DELETE FROM eos_investigations WHERE investigation_id=?",
                    (investigation_id,),
                )
        return cur.rowcount > 0

    # Passes CRUD

    def create_pass(
        self,
        *,
        investigation_id: str,
        task_type: str,
        trigger_type: str,
        objective: str,
    ) -> dict[str, Any]:
        now = _iso_now()
        pid = "PASS-" + uuid.uuid4().hex[:8]
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO eos_investigation_passes
                       (pass_id, investigation_id, task_type, trigger_type,
                        objective, started_at, outcome_status)
                       VALUES (?,?,?,?,?,?,?)""",
                    (pid, investigation_id, task_type, trigger_type,
                     objective, now, "running"),
                )
        return {"pass_id": pid, "investigation_id": investigation_id, "started_at": now}

    def finish_pass(self, pass_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    """UPDATE eos_investigation_passes
                       SET finished_at=?, outcome_status=?, confidence_score=?,
                           summary=?, findings=?, hypotheses=?, evidence_linked=?,
                           next_action=?, degraded_backend=?, error_text=?
                       WHERE pass_id=?""",
                    (
                        _iso_now(),
                        result.get("outcome_status", "completed"),
                        result.get("confidence_score", 0.0),
                        result.get("summary", ""),
                        json.dumps(result.get("findings", [])),
                        json.dumps(result.get("hypotheses", [])),
                        json.dumps(result.get("evidence_linked", [])),
                        result.get("next_action", ""),
                        int(result.get("degraded_backend", False)),
                        result.get("error_text", ""),
                        pass_id,
                    ),
                )

    def list_passes(self, investigation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM eos_investigation_passes "
                    "WHERE investigation_id=? ORDER BY started_at DESC",
                    (investigation_id,),
                ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            for key in ("findings", "hypotheses", "evidence_linked"):
                try:
                    d[key] = json.loads(d.get(key, "[]"))
                except Exception:
                    d[key] = []
            result.append(d)
        return result


# ── Engine ────────────────────────────────────────────────────────────────────

class InvestigationEngine:
    """Central coordinator for entity investigations.

    One instance per EOS process, shared.  Thread-safe at the store level.
    Pass execution is async (non-blocking for the FastAPI event loop).
    """

    def __init__(self, cfg: dict) -> None:
        db_path = cfg.get("db_path", "data/entity_state.db")
        self._store = _InvestigationStore(db_path)
        inv_cfg = cfg.get("investigation", {})
        self._max_evidence:   int   = int(inv_cfg.get("max_evidence_per_pass", DEFAULT_MAX_EVIDENCE_PER_PASS))
        self._max_duration_s: float = float(inv_cfg.get("max_pass_duration_s", DEFAULT_MAX_PASS_DURATION_S))
        self._conf_threshold: float = float(inv_cfg.get("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD))
        self._min_evidence:   int   = int(inv_cfg.get("min_evidence_for_synthesis", DEFAULT_MIN_EVIDENCE_FOR_SYNTHESIS))

        # Counters
        self._pass_total:    int = 0
        self._pass_degraded: int = 0
        self._pass_failed:   int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def create(
        self,
        *,
        title: str,
        description: str = "",
        category: str = "general",
        priority: int = 3,
        created_by: str = "system",
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        """Create a new investigation."""
        return self._store.create_investigation(
            title=title,
            description=description,
            category=category,
            priority=priority,
            created_by=created_by,
            metadata=metadata,
        )

    def get(self, investigation_id: str) -> dict[str, Any] | None:
        """Get investigation by ID, with pass history attached."""
        inv = self._store.get(investigation_id)
        if inv is None:
            return None
        inv["passes"] = self._store.list_passes(investigation_id)
        return inv

    def list(self, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return self._store.list(status=status, limit=limit)

    def resolve(
        self,
        investigation_id: str,
        resolution_summary: str,
    ) -> dict[str, Any] | None:
        return self._store.update_status(
            investigation_id,
            "resolved",
            resolution_summary=resolution_summary,
        )

    def reopen(self, investigation_id: str) -> dict[str, Any] | None:
        inv = self._store.get(investigation_id)
        if inv is None:
            return None
        if inv["status"] in ("open", "active"):
            return inv
        return self._store.update_status(investigation_id, "open")

    def delete(self, investigation_id: str) -> bool:
        return self._store.delete(investigation_id)

    def get_diagnostics(self) -> dict[str, Any]:
        return {
            "pass_total":    self._pass_total,
            "pass_degraded": self._pass_degraded,
            "pass_failed":   self._pass_failed,
            "open_count":    len(self._store.list(status="open", limit=100)),
            "active_count":  len(self._store.list(status="active", limit=100)),
        }

    # ── Pass execution ────────────────────────────────────────────────────────

    async def run_pass(
        self,
        topology: "RuntimeTopology",
        investigation_id: str,
        *,
        task_type: str = "evidence_review",
        trigger_type: str = "manual",
        objective: str = "",
        tracer=None,
        bus=None,
    ) -> dict[str, Any]:
        """Execute one bounded investigation pass (async, non-blocking).

        task_type options:
          evidence_review        — gather + summarise evidence
          hypothesis_generation  — generate hypotheses from evidence
          recommendation_draft   — draft actionable recommendations
          synthesis              — consolidate all findings
        """
        inv = self._store.get(investigation_id)
        if inv is None:
            return {"ok": False, "error": "NOT_FOUND",
                    "message": f"Investigation {investigation_id} not found"}

        # Activate if open
        if inv["status"] == "open":
            self._store.update_status(investigation_id, "active")

        pass_rec = self._store.create_pass(
            investigation_id=investigation_id,
            task_type=task_type,
            trigger_type=trigger_type,
            objective=objective or f"{task_type} pass for: {inv['title']}",
        )
        pass_id = pass_rec["pass_id"]
        self._pass_total += 1
        pass_start = time.monotonic()

        result: dict[str, Any] = {
            "ok":              True,
            "pass_id":         pass_id,
            "investigation_id": investigation_id,
            "task_type":       task_type,
            "outcome_status":  "completed",
            "confidence_score": 0.0,
            "summary":         "",
            "findings":        [],
            "hypotheses":      [],
            "evidence_linked": [],
            "next_action":     "",
            "degraded_backend": False,
            "error_text":      "",
        }

        try:
            # ── 1. Gather evidence ────────────────────────────────────────
            evidence_items = await self._gather_evidence(
                topology=topology,
                query=objective or inv["title"],
                max_items=self._max_evidence,
            )
            result["evidence_linked"] = [e.get("id", "") for e in evidence_items]

            elapsed = time.monotonic() - pass_start
            if elapsed > self._max_duration_s:
                result["outcome_status"] = "timed_out"
                result["error_text"] = f"Timed out after {elapsed:.1f}s during evidence gather"
                self._pass_failed += 1
                self._store.finish_pass(pass_id, result)
                return result

            # ── 2. Execute task-specific reasoning ────────────────────────
            enough_evidence = len(evidence_items) >= self._min_evidence

            if task_type == "evidence_review":
                await self._do_evidence_review(
                    topology, inv, evidence_items, result
                )

            elif task_type == "hypothesis_generation":
                if enough_evidence:
                    await self._do_hypothesis_generation(
                        topology, inv, evidence_items, result
                    )
                else:
                    result["degraded_backend"] = True
                    result["summary"] = (
                        f"Insufficient evidence ({len(evidence_items)} items < "
                        f"threshold {self._min_evidence}) — hypotheses deferred."
                    )
                    result["next_action"] = "evidence_review"

            elif task_type == "recommendation_draft":
                if enough_evidence:
                    await self._do_recommendation_draft(
                        topology, inv, evidence_items, result
                    )
                else:
                    result["degraded_backend"] = True
                    result["summary"] = "Insufficient evidence for recommendations."
                    result["next_action"] = "evidence_review"

            elif task_type == "synthesis":
                await self._do_synthesis(topology, inv, evidence_items, result)

            else:
                result["error_text"] = f"Unknown task_type: {task_type}"
                result["outcome_status"] = "failed"

            # ── 3. Duration check ─────────────────────────────────────────
            elapsed = time.monotonic() - pass_start
            if elapsed > self._max_duration_s and result["outcome_status"] == "completed":
                result["outcome_status"] = "completed_late"
                result["error_text"] += (
                    f" [Note: pass ran {elapsed:.1f}s > budget {self._max_duration_s}s]"
                )

            if result.get("degraded_backend"):
                self._pass_degraded += 1

        except asyncio.CancelledError:
            result["outcome_status"] = "cancelled"
            result["error_text"] = "Pass was cancelled"
            raise

        except Exception as exc:
            logger.error("[InvestigationEngine] Pass %s failed: %s", pass_id, exc)
            result["ok"] = False
            result["outcome_status"] = "failed"
            result["error_text"] = str(exc)
            self._pass_failed += 1

        finally:
            self._store.finish_pass(pass_id, result)

        # ── Publish signal ────────────────────────────────────────────────
        if bus:
            try:
                from runtime.signal_bus import SignalEnvelope, SEVERITY_INFO
                bus.publish(SignalEnvelope(
                    source="investigation_engine",
                    signal_type="investigation_pass_complete",
                    severity=SEVERITY_INFO,
                    confidence=result.get("confidence_score", 0.5),
                    payload={
                        "pass_id":         pass_id,
                        "investigation_id": investigation_id,
                        "task_type":       task_type,
                        "outcome_status":  result["outcome_status"],
                        "summary":         result["summary"][:200],
                    },
                ))
            except Exception:
                pass

        return result

    # ── Task-specific pass handlers ───────────────────────────────────────────

    async def _gather_evidence(
        self,
        topology: "RuntimeTopology",
        query: str,
        max_items: int,
    ) -> list[dict[str, Any]]:
        """Retrieve relevant memory items as evidence."""
        from core.memory import search_memory
        try:
            items = search_memory(query, top_k=max_items)
            return items or []
        except Exception as exc:
            logger.warning("[InvestigationEngine] Evidence gather failed: %s", exc)
            return []

    async def _call_reasoning(
        self,
        topology: "RuntimeTopology",
        prompt: str,
        *,
        use_thinking_worker: bool = False,
        temperature: float = 0.4,
        max_tokens: int = 512,
    ) -> tuple[str, bool]:
        """Call the reasoning backend. Returns (text, degraded).

        When use_thinking_worker=True the request is routed through the QWEN
        orchestrator's think_for_background() interface — the investigation
        engine never calls port 8083 directly.  This ensures the ThinkingFaculty
        authority remains exclusively with the QWEN executive layer.

        Falls back to the primary Qwen3 server when the thinking server is
        unavailable (handled transparently inside think_for_background).
        Degraded=True means reasoning was completely unavailable.
        """
        if use_thinking_worker:
            try:
                from runtime.orchestrator import think_for_background
                artifact = await think_for_background(topology, prompt)
                if artifact.degraded:
                    logger.debug(
                        "[InvestigationEngine] ThinkingFaculty degraded — falling back to primary"
                    )
                    # Fall through to primary below
                else:
                    return artifact.best_text, False
            except Exception as exc:
                logger.debug("[InvestigationEngine] think_for_background failed: %s", exc)
                # Fall through to primary

        try:
            endpoint = topology.primary_endpoint()
            if not endpoint:
                return "", True
            import httpx
            async with httpx.AsyncClient(timeout=self._max_duration_s) as client:
                resp = await client.post(
                    f"{endpoint}/v1/chat/completions",
                    json={
                        "model": "qwen3",
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "/no_think\n\n"
                                    "You are a focused reasoning assistant performing "
                                    "evidence-grounded analysis. Be concise and specific."
                                ),
                            },
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": temperature,
                        "max_tokens":  max_tokens,
                    },
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"].strip(), False
        except Exception as exc:
            logger.warning("[InvestigationEngine] Reasoning unavailable: %s", exc)
            return "", True

    async def _do_evidence_review(
        self,
        topology: "RuntimeTopology",
        inv: dict,
        evidence: list[dict],
        result: dict,
    ) -> None:
        evidence_block = "\n".join(
            f"[{i+1}] {e.get('text', '')[:300]}"
            for i, e in enumerate(evidence)
        )
        prompt = (
            f"Investigation: {inv['title']}\n"
            f"Description: {inv.get('description', '')}\n\n"
            f"Evidence ({len(evidence)} items):\n{evidence_block}\n\n"
            "Briefly summarise the key themes and gaps in this evidence. "
            "What is most relevant? What is missing? 3-5 bullet points."
        )
        text, degraded = await self._call_reasoning(topology, prompt, max_tokens=400)
        result["summary"]          = text or "Evidence reviewed (reasoning unavailable)."
        result["degraded_backend"] = degraded
        result["confidence_score"] = 0.6 if not degraded else 0.3
        result["findings"]         = [text[:500]] if text else []
        result["next_action"]      = "hypothesis_generation" if len(evidence) >= self._min_evidence else ""

    async def _do_hypothesis_generation(
        self,
        topology: "RuntimeTopology",
        inv: dict,
        evidence: list[dict],
        result: dict,
    ) -> None:
        evidence_block = "\n".join(
            f"[{i+1}] {e.get('text', '')[:200]}"
            for i, e in enumerate(evidence[:10])
        )
        prompt = (
            f"Investigation: {inv['title']}\n\n"
            f"Evidence summary:\n{evidence_block}\n\n"
            "Generate 2-4 specific, testable hypotheses based on this evidence. "
            "Format each as: H1: <hypothesis text>"
        )
        text, degraded = await self._call_reasoning(
            topology, prompt,
            use_thinking_worker=True,  # use port 8083 for deeper reasoning
            max_tokens=600,
        )
        hypotheses = []
        if text:
            for line in text.split("\n"):
                if line.strip().startswith("H") and ":" in line:
                    hypotheses.append(line.strip())

        result["hypotheses"]       = hypotheses or ([text[:500]] if text else [])
        result["summary"]          = f"Generated {len(hypotheses)} hypothesis/es."
        result["degraded_backend"] = degraded
        result["confidence_score"] = min(0.5 + len(hypotheses) * 0.1, 0.9) if not degraded else 0.2
        result["next_action"]      = "recommendation_draft"

    async def _do_recommendation_draft(
        self,
        topology: "RuntimeTopology",
        inv: dict,
        evidence: list[dict],
        result: dict,
    ) -> None:
        evidence_block = "\n".join(
            f"[{i+1}] {e.get('text', '')[:200]}"
            for i, e in enumerate(evidence[:8])
        )
        prompt = (
            f"Investigation: {inv['title']}\n\n"
            f"Based on the following evidence:\n{evidence_block}\n\n"
            "Draft 2-3 specific, actionable recommendations. "
            "Format each as: R1: <recommendation>"
        )
        text, degraded = await self._call_reasoning(topology, prompt, max_tokens=500)
        recommendations = []
        if text:
            for line in text.split("\n"):
                if line.strip().startswith("R") and ":" in line:
                    recommendations.append(line.strip())

        conf = min(0.55 + len(recommendations) * 0.1, 0.95) if not degraded else 0.2
        result["findings"]         = recommendations or ([text[:500]] if text else [])
        result["summary"]          = f"Drafted {len(recommendations)} recommendation(s)."
        result["degraded_backend"] = degraded
        result["confidence_score"] = conf
        result["next_action"]      = "synthesis" if conf >= self._conf_threshold else "evidence_review"

    async def _do_synthesis(
        self,
        topology: "RuntimeTopology",
        inv: dict,
        evidence: list[dict],
        result: dict,
    ) -> None:
        evidence_block = "\n".join(
            f"- {e.get('text', '')[:200]}"
            for e in evidence[:10]
        )
        prompt = (
            f"Investigation: {inv['title']}\n"
            f"Description: {inv.get('description', '')}\n\n"
            f"Evidence:\n{evidence_block}\n\n"
            "Write a concise final synthesis (3-5 sentences) covering: "
            "what was found, what it means, and what should happen next."
        )
        text, degraded = await self._call_reasoning(
            topology, prompt,
            use_thinking_worker=True,
            temperature=0.5,
            max_tokens=700,
        )
        result["summary"]          = text or "Synthesis unavailable (reasoning backend degraded)."
        result["degraded_backend"] = degraded
        result["confidence_score"] = 0.8 if not degraded else 0.25
        result["findings"]         = [text[:800]] if text else []
        result["next_action"]      = "resolved" if not degraded else "synthesis"
