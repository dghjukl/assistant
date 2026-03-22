"""
EOS — Memory Maintenance
Scheduled cleanup, pruning, and consolidation for the dual-store memory layer.

Two stores are maintained:
  SQLite   — interaction_log (interaction_log table in entity_state.db)
  ChromaDB — vector store (chroma_path from config)

Maintenance operations
----------------------
prune_interactions(days)
    Delete interaction_log entries older than `days` days.
    Preserves the last 1000 entries regardless of age.

prune_vector_store(max_items, min_score)
    If the vector store exceeds max_items entries, delete the lowest-scored
    (salience + recency combined) items until under the cap.
    min_score threshold removes entries whose combined quality is below floor.

consolidate(topology, cfg)
    Group interaction_log entries into thematic clusters and ask Qwen3 to
    summarise each cluster into a single high-quality memory entry.
    The source entries are NOT deleted — consolidation only adds.
    Runs at most once per consolidation_interval_hours.

health_check()
    Return a dict describing store health: file sizes, entry counts, errors.

run_maintenance(topology, cfg, tracer, bus)
    Orchestrator: calls all operations in the correct order.
    Safe to call repeatedly — each operation is idempotent.

Schedule (driven by server.py's _memory_maintenance_loop)
---------
  maintenance_interval_hours  (default 6)   — how often run_maintenance fires
  prune_age_days              (default 30)   — interaction log retention window
  max_vector_items            (default 5000) — hard cap on vector store entries
  min_vector_score            (default 0.1)  — prune entries below this quality
  consolidate_batch_size      (default 20)   — entries per consolidation prompt
  consolidation_interval_hours (default 24)  — minimum gap between consolidations
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from runtime.topology import RuntimeTopology

logger = logging.getLogger("eos.memory_maintenance")
UTC = timezone.utc


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# ── Interaction log pruning ───────────────────────────────────────────────────

def prune_interactions(db_path: str | Path, *, days: int = 30) -> dict[str, Any]:
    """Delete interaction_log entries older than `days` days, keeping last 1000.

    Returns: {"pruned": int, "kept": int, "error": str|None}
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        return {"pruned": 0, "kept": 0, "error": f"DB not found: {db_path}"}

    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    pruned = 0
    kept   = 0

    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            # Count total entries
            total = conn.execute(
                "SELECT COUNT(*) FROM interaction_log"
            ).fetchone()[0]

            if total == 0:
                return {"pruned": 0, "kept": 0, "error": None}

            # Keep the most recent 1000 regardless of age
            keep_ids_row = conn.execute(
                "SELECT id FROM interaction_log ORDER BY id DESC LIMIT 1000"
            ).fetchall()
            keep_ids = {r["id"] for r in keep_ids_row}

            # Delete old entries that aren't in the keep set
            cur = conn.execute(
                "DELETE FROM interaction_log WHERE timestamp < ? AND id NOT IN ({})".format(
                    ",".join("?" * len(keep_ids))
                ),
                [cutoff] + list(keep_ids),
            )
            pruned = cur.rowcount
            kept   = total - pruned

        logger.info(
            "[MemMaint] Interaction log pruned: %d removed, %d kept (age > %d days)",
            pruned, kept, days,
        )
        return {"pruned": pruned, "kept": kept, "error": None}

    except sqlite3.OperationalError as exc:
        # Table might not exist yet (fresh install)
        if "no such table" in str(exc).lower():
            return {"pruned": 0, "kept": 0, "error": None}
        logger.warning("[MemMaint] Prune interactions error: %s", exc)
        return {"pruned": 0, "kept": 0, "error": str(exc)}
    except Exception as exc:
        logger.error("[MemMaint] Prune interactions failed: %s", exc)
        return {"pruned": 0, "kept": 0, "error": str(exc)}


# ── Vector store pruning ──────────────────────────────────────────────────────

def prune_vector_store(
    *,
    chroma_path: str,
    max_items: int = 5000,
    min_score: float = 0.1,
) -> dict[str, Any]:
    """Prune the ChromaDB vector store.

    - Removes items whose combined salience+recency score is below min_score.
    - If still over max_items, removes lowest-scored items until under cap.

    Returns: {"pruned": int, "remaining": int, "error": str|None}
    """
    try:
        import chromadb
        client     = chromadb.PersistentClient(path=chroma_path)
        collection = client.get_or_create_collection("eos_memory")
        total      = collection.count()

        if total == 0:
            return {"pruned": 0, "remaining": 0, "error": None}

        # Retrieve all items with metadata
        result = collection.get(
            include=["metadatas", "ids"],
            limit=min(total, 50000),
        )
        ids       = result.get("ids", [])
        metadatas = result.get("metadatas", [{}] * len(ids))

        # Score each item: salience + recency_weight
        now_ts   = time.time()
        import math
        scored = []
        for item_id, meta in zip(ids, metadatas):
            salience   = float(meta.get("salience_score", 0.5))
            emotional  = float(meta.get("emotional_weight", 0.0))
            ts         = float(meta.get("timestamp", now_ts))
            days_old   = (now_ts - ts) / 86400.0
            recency    = math.exp(-0.1 * days_old)
            combined   = salience * 0.50 + recency * 0.30 + emotional * 0.20
            scored.append((item_id, combined))

        # Mark below-floor items for deletion
        to_delete = [iid for iid, score in scored if score < min_score]

        # If still over cap, add lowest-scored items to delete set
        remaining_after_floor = total - len(to_delete)
        if remaining_after_floor > max_items:
            keep_ids_set = {iid for iid, _ in scored if iid not in {d for d in to_delete}}
            sorted_by_score = sorted(
                [(iid, s) for iid, s in scored if iid in keep_ids_set],
                key=lambda x: x[1],
            )
            overflow = remaining_after_floor - max_items
            to_delete.extend(iid for iid, _ in sorted_by_score[:overflow])

        if not to_delete:
            return {"pruned": 0, "remaining": total, "error": None}

        # Delete in batches of 500 (ChromaDB limit)
        batch_size = 500
        for i in range(0, len(to_delete), batch_size):
            collection.delete(ids=to_delete[i : i + batch_size])

        remaining = collection.count()
        logger.info(
            "[MemMaint] Vector store pruned: %d removed, %d remaining",
            len(to_delete), remaining,
        )
        return {"pruned": len(to_delete), "remaining": remaining, "error": None}

    except Exception as exc:
        logger.error("[MemMaint] Vector store prune failed: %s", exc)
        return {"pruned": 0, "remaining": -1, "error": str(exc)}


# ── Memory consolidation ──────────────────────────────────────────────────────

_last_consolidation: float = 0.0


async def consolidate(
    topology: "RuntimeTopology",
    cfg: dict,
    *,
    batch_size: int = 20,
    min_gap_hours: float = 24.0,
) -> dict[str, Any]:
    """Cluster recent interactions and consolidate each cluster into a single
    high-quality memory entry via Qwen3.

    Does NOT delete source entries — consolidation is additive only.
    Returns: {"batches_processed": int, "entries_created": int, "error": str|None}
    """
    global _last_consolidation

    now = time.time()
    gap_secs = min_gap_hours * 3600
    if now - _last_consolidation < gap_secs:
        remaining = gap_secs - (now - _last_consolidation)
        logger.debug(
            "[MemMaint] Consolidation skipped — next run in %.0fmin", remaining / 60
        )
        return {"batches_processed": 0, "entries_created": 0,
                "error": None, "skipped": True}

    from core.memory import get_recent_interactions, store_memory
    import httpx

    try:
        entries = get_recent_interactions(limit=batch_size * 3)
    except Exception as exc:
        return {"batches_processed": 0, "entries_created": 0, "error": str(exc)}

    if not entries:
        return {"batches_processed": 0, "entries_created": 0, "error": None}

    # Process in batches
    primary_endpoint = topology.primary_endpoint()
    batches_done  = 0
    entries_added = 0

    for i in range(0, len(entries), batch_size):
        batch = entries[i : i + batch_size]
        if len(batch) < 5:
            continue  # Too small to be worth consolidating

        # Format batch for Qwen3
        convo_block = "\n".join(
            f"[{e.get('role','?')}] {e.get('content','')[:200]}"
            for e in batch
        )
        prompt = (
            f"/no_think\n\n"
            f"The following is a batch of {len(batch)} conversation turns. "
            "Write a concise 2-4 sentence summary capturing the key topics, "
            "decisions, and important context. This will be stored as a long-term memory.\n\n"
            f"Batch:\n{convo_block}\n\n"
            "Summary:"
        )

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{primary_endpoint}/v1/chat/completions",
                    json={
                        "model":       "qwen3",
                        "messages":    [{"role": "user", "content": prompt}],
                        "temperature": 0.3,
                        "max_tokens":  300,
                    },
                )
                resp.raise_for_status()
                summary = resp.json()["choices"][0]["message"]["content"].strip()

            if summary:
                store_memory(
                    summary,
                    source="consolidation",
                    salience_score=0.8,
                    emotional_weight=0.1,
                    tags="consolidated,maintenance",
                )
                entries_added += 1

            batches_done += 1

        except Exception as exc:
            logger.warning("[MemMaint] Consolidation batch %d failed: %s", i // batch_size, exc)
            continue

    _last_consolidation = time.time()
    logger.info(
        "[MemMaint] Consolidation complete: %d batches, %d entries created",
        batches_done, entries_added,
    )
    return {
        "batches_processed": batches_done,
        "entries_created":   entries_added,
        "error":             None,
    }


# ── Health check ──────────────────────────────────────────────────────────────

def health_check(cfg: dict) -> dict[str, Any]:
    """Return a dict describing memory store health."""
    result: dict[str, Any] = {
        "sqlite": {},
        "chroma": {},
        "errors": [],
    }

    # SQLite
    db_path = Path(cfg.get("db_path", "data/entity_state.db"))
    if db_path.is_file():
        size_kb = db_path.stat().st_size // 1024
        try:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                tables = {}
                for (table_name,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall():
                    count = conn.execute(
                        f"SELECT COUNT(*) FROM {table_name}"  # noqa: S608
                    ).fetchone()[0]
                    tables[table_name] = count
            result["sqlite"] = {
                "path":    str(db_path),
                "size_kb": size_kb,
                "tables":  tables,
                "ok":      True,
            }
        except Exception as exc:
            result["sqlite"] = {"ok": False, "error": str(exc)}
            result["errors"].append(f"sqlite: {exc}")
    else:
        result["sqlite"] = {"ok": False, "error": "DB file not found"}

    # ChromaDB
    chroma_path = cfg.get("retrieval", {}).get("chroma_path", "data/memory_store")
    chroma_dir  = Path(chroma_path)
    if chroma_dir.is_dir():
        try:
            import chromadb
            client     = chromadb.PersistentClient(path=chroma_path)
            collection = client.get_or_create_collection("eos_memory")
            count      = collection.count()
            size_kb    = sum(
                f.stat().st_size
                for f in chroma_dir.rglob("*") if f.is_file()
            ) // 1024
            result["chroma"] = {
                "path":    chroma_path,
                "size_kb": size_kb,
                "entries": count,
                "ok":      True,
            }
        except Exception as exc:
            result["chroma"] = {"ok": False, "error": str(exc)}
            result["errors"].append(f"chroma: {exc}")
    else:
        result["chroma"] = {"ok": False, "error": "Chroma directory not found"}

    return result


# ── Full maintenance run ──────────────────────────────────────────────────────

async def run_maintenance(
    topology: "RuntimeTopology",
    cfg: dict,
    *,
    tracer=None,
    bus=None,
) -> dict[str, Any]:
    """Run all maintenance operations in order.

    Order:
      1. Health check (read-only, always safe)
      2. Prune interaction log
      3. Prune vector store
      4. Consolidate (if enough time has passed)

    Returns a combined result dict.
    """
    maint_cfg = cfg.get("memory_maintenance", {})
    db_path   = cfg.get("db_path", "data/entity_state.db")
    chroma_path = cfg.get("retrieval", {}).get("chroma_path", "data/memory_store")

    prune_age_days    = int(maint_cfg.get("prune_age_days",     30))
    max_vector_items  = int(maint_cfg.get("max_vector_items", 5000))
    min_vector_score  = float(maint_cfg.get("min_vector_score", 0.1))
    batch_size        = int(maint_cfg.get("consolidate_batch_size", 20))
    min_gap_hours     = float(maint_cfg.get("consolidation_interval_hours", 24.0))

    logger.info("[MemMaint] Starting maintenance run...")
    started_at = time.time()

    # 1. Health check
    health = health_check(cfg)

    # 2. Prune interaction log
    prune_result = prune_interactions(db_path, days=prune_age_days)

    # 3. Prune vector store
    vector_result = prune_vector_store(
        chroma_path=chroma_path,
        max_items=max_vector_items,
        min_score=min_vector_score,
    )

    # 4. Consolidation (async, may skip if too recent)
    consolidation_result = await consolidate(
        topology, cfg,
        batch_size=batch_size,
        min_gap_hours=min_gap_hours,
    )

    elapsed_ms = int((time.time() - started_at) * 1000)
    result = {
        "ran_at":       _iso_now(),
        "elapsed_ms":   elapsed_ms,
        "health":       health,
        "interaction_prune": prune_result,
        "vector_prune": vector_result,
        "consolidation": consolidation_result,
    }

    # Publish to bus
    if bus:
        try:
            from runtime.signal_bus import SignalEnvelope, STYPE_SYSTEM_HEALTH, SEVERITY_INFO
            bus.publish(SignalEnvelope(
                source="memory_maintenance",
                signal_type=STYPE_SYSTEM_HEALTH,
                severity=SEVERITY_INFO,
                confidence=0.9,
                payload={
                    "elapsed_ms":      elapsed_ms,
                    "interactions_pruned": prune_result.get("pruned", 0),
                    "vectors_pruned":  vector_result.get("pruned", 0),
                    "entries_created": consolidation_result.get("entries_created", 0),
                },
            ))
        except Exception:
            pass

    # Record in tracer
    if tracer:
        try:
            import uuid
            tracer.record_reflection({
                "reflection_id": "MAINT-" + uuid.uuid4().hex[:8],
                "trigger":       "scheduled_maintenance",
                "inputs_reviewed": ["interaction_log", "vector_store"],
                "conclusions": [
                    f"Pruned {prune_result.get('pruned', 0)} interaction entries",
                    f"Pruned {vector_result.get('pruned', 0)} vector store entries",
                    f"Created {consolidation_result.get('entries_created', 0)} consolidated memories",
                ],
                "suggestions": [],
                "similar_before": True,
            })
        except Exception:
            pass

    logger.info(
        "[MemMaint] Done in %dms — "
        "log pruned %d | vectors pruned %d | consolidated %d",
        elapsed_ms,
        prune_result.get("pruned", 0),
        vector_result.get("pruned", 0),
        consolidation_result.get("entries_created", 0),
    )
    return result
