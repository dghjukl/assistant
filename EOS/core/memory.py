"""
EOS — Persistence Layer
SQLite for structured entity state + ChromaDB for vector memory.

Memory metadata schema includes Mcore-derived scoring fields:
  salience_score   — importance weight (0.0–1.0), set at store time, updated by relevance
  emotional_weight — emotional significance (0.0–1.0)
  recency_weight   — derived from timestamp, not stored (computed at query time)
  retrieval_count  — incremented on every retrieval
  last_used_at     — unix timestamp of last retrieval
  source           — "interaction" | "reflection" | "manual"
  tags             — comma-separated tag string

Combined retrieval scoring:
  combined = semantic(0.40) + salience(0.25) + recency(0.20) + emotional(0.10) + frequency(0.05)
  recency_weight = exp(-λ * days_since_creation)  where λ = 0.1
  frequency_factor = log(1 + retrieval_count) / log(max_count + 1)
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

# Config is passed in at init time to avoid circular imports
_cfg: dict = {}


def configure(cfg: dict) -> None:
    """Must be called before any other function. Passes the loaded config dict."""
    global _cfg
    _cfg = cfg
    Path(_cfg["db_path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(_cfg["retrieval"]["chroma_path"]).mkdir(parents=True, exist_ok=True)


# ── SQLite ────────────────────────────────────────────────────────────────────

def _db_path() -> str:
    return _cfg.get("db_path", "data/entity_state.db")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create all tables, seed defaults, and run pending migrations."""
    defaults = _cfg.get("autonomy_defaults", {
        "perception": True,
        "cognition":  True,
        "action":     False,
        "initiative": False,
    })
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS identity_state (
                domain      TEXT PRIMARY KEY,
                answer      TEXT NOT NULL DEFAULT '',
                confidence  REAL NOT NULL DEFAULT 0.0,
                updated_at  REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS interaction_log (
                id          TEXT PRIMARY KEY,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                timestamp   REAL NOT NULL,
                metadata    TEXT
            );

            CREATE TABLE IF NOT EXISTS reflection_log (
                id          TEXT PRIMARY KEY,
                cycle       INTEGER NOT NULL,
                domain      TEXT NOT NULL,
                answer      TEXT NOT NULL,
                confidence  REAL NOT NULL,
                drift       REAL,
                timestamp   REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS relational_model (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                updated_at  REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS autonomy_profile (
                dimension   TEXT PRIMARY KEY,
                enabled     INTEGER NOT NULL DEFAULT 0,
                updated_at  REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS entity_meta (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS attention_preferences (
                category      TEXT NOT NULL,
                topic         TEXT NOT NULL,
                score         REAL NOT NULL DEFAULT 0.0,
                note          TEXT NOT NULL DEFAULT '',
                origin        TEXT NOT NULL DEFAULT '',
                first_seen_at REAL NOT NULL,
                last_seen_at  REAL NOT NULL,
                updated_at    REAL NOT NULL,
                metadata      TEXT,
                PRIMARY KEY (category, topic)
            );
        """)

        # Seed identity domains
        for domain in ["ontology", "purpose", "relational", "agency", "constraints", "self_change"]:
            conn.execute(
                "INSERT OR IGNORE INTO identity_state VALUES (?, '', 0.0, ?)",
                (domain, time.time())
            )

        # Seed autonomy defaults
        for dim, enabled in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO autonomy_profile VALUES (?, ?, ?)",
                (dim, int(enabled), time.time())
            )
        conn.commit()

    # Run any pending schema migrations for this database
    try:
        from core.db_migrations import apply_migrations
        with get_db() as mconn:
            apply_migrations(mconn, "entity_state")
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "[memory] Migration runner failed (non-fatal): %s", exc
        )


# ── Interaction log ───────────────────────────────────────────────────────────

def log_interaction(role: str, content: str, metadata: dict | None = None) -> str:
    entry_id = str(uuid.uuid4())
    with get_db() as conn:
        conn.execute(
            "INSERT INTO interaction_log VALUES (?, ?, ?, ?, ?)",
            (entry_id, role, content, time.time(), json.dumps(metadata) if metadata else None)
        )
        conn.commit()
    return entry_id


def get_recent_interactions(n: int = 20) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT role, content, timestamp FROM interaction_log "
            "ORDER BY timestamp DESC LIMIT ?",
            (n,)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def count_interactions() -> int:
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM interaction_log").fetchone()[0]


# ── Identity state ────────────────────────────────────────────────────────────

def get_identity_state() -> dict[str, dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM identity_state").fetchall()
    return {
        r["domain"]: {"answer": r["answer"], "confidence": r["confidence"]}
        for r in rows
    }


def update_identity_domain(domain: str, answer: str, confidence: float) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE identity_state SET answer=?, confidence=?, updated_at=? WHERE domain=?",
            (answer, confidence, time.time(), domain)
        )
        conn.commit()


def log_reflection(
    cycle: int, domain: str, answer: str, confidence: float, drift: float | None
) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO reflection_log VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), cycle, domain, answer, confidence, drift, time.time())
        )
        conn.commit()


def get_reflection_log(limit: int = 50) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM reflection_log ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_reflection_cycle() -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM entity_meta WHERE key='reflection_cycle'"
        ).fetchone()
    return int(row["value"]) if row else 0


def increment_reflection_cycle() -> int:
    cycle = get_reflection_cycle() + 1
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO entity_meta VALUES ('reflection_cycle', ?)",
            (str(cycle),)
        )
        conn.commit()
    return cycle


# ── Relational model ──────────────────────────────────────────────────────────

def get_relational_model() -> dict[str, Any]:
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM relational_model").fetchall()
    return {r["key"]: json.loads(r["value"]) for r in rows}


def update_relational(key: str, value: Any) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO relational_model VALUES (?, ?, ?)",
            (key, json.dumps(value), time.time())
        )
        conn.commit()


# ── Autonomy ──────────────────────────────────────────────────────────────────

def get_autonomy() -> dict[str, bool]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT dimension, enabled FROM autonomy_profile"
        ).fetchall()
    return {r["dimension"]: bool(r["enabled"]) for r in rows}


def set_autonomy(dimension: str, enabled: bool) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE autonomy_profile SET enabled=?, updated_at=? WHERE dimension=?",
            (int(enabled), time.time(), dimension)
        )
        conn.commit()


# ── Entity name / meta ────────────────────────────────────────────────────────

def get_entity_name() -> str | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM entity_meta WHERE key='name'"
        ).fetchone()
    return row["value"] if row else None


def set_entity_name(name: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO entity_meta VALUES ('name', ?)", (name,)
        )
        conn.commit()


def get_meta(key: str, default: str | None = None) -> str | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM entity_meta WHERE key=?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO entity_meta VALUES (?, ?)", (key, value)
        )
        conn.commit()


# ── ChromaDB vector memory ────────────────────────────────────────────────────

_chroma_client = None
_embed_model: SentenceTransformer | None = None
_collection = None

_RECENCY_LAMBDA = 0.1   # decay constant: recency_weight = exp(-λ * days)


def _get_chroma():
    global _chroma_client, _collection
    if _chroma_client is None:
        path = _cfg["retrieval"]["chroma_path"]
        _chroma_client = chromadb.PersistentClient(
            path=path,
            settings=Settings(anonymized_telemetry=False),
        )
        _collection = _chroma_client.get_or_create_collection(
            name=_cfg["retrieval"]["collection"],
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


_embed_unavailable: bool = False  # set True if model not cached; avoids repeated hang attempts

def _get_embed() -> SentenceTransformer:
    global _embed_model, _embed_unavailable
    if _embed_unavailable:
        raise RuntimeError("Embedding model not available (not cached locally)")
    if _embed_model is None:
        model_name = _cfg["retrieval"]["embed_model"]
        try:
            # Try cache-only first — never block on a network download
            _embed_model = SentenceTransformer(
                model_name, device="cpu", local_files_only=True
            )
        except Exception:
            # Model not cached; mark unavailable so future calls skip immediately
            _embed_unavailable = True
            raise RuntimeError(
                f"Embedding model '{model_name}' is not cached locally. "
                "Memory retrieval disabled until the model is downloaded."
            )
    return _embed_model


def store_memory(
    text: str,
    *,
    source:          str   = "interaction",
    salience_score:  float = 0.5,
    emotional_weight: float = 0.3,
    tags:            str   = "",
) -> str:
    """
    Embed and store a text chunk with rich scoring metadata.
    Returns the memory ID.
    """
    collection = _get_chroma()
    model      = _get_embed()
    mem_id     = str(uuid.uuid4())
    embedding  = model.encode(text).tolist()
    now        = time.time()

    collection.add(
        ids=[mem_id],
        embeddings=[embedding],
        documents=[text],
        metadatas=[{
            "timestamp":        now,
            "last_used_at":     now,
            "retrieval_count":  0,
            "salience_score":   salience_score,
            "emotional_weight": emotional_weight,
            "source":           source,
            "tags":             tags,
        }],
    )
    return mem_id


def _recency_weight(timestamp: float) -> float:
    """Exponential decay: recent memories score higher."""
    days = (time.time() - timestamp) / 86400.0
    return math.exp(-_RECENCY_LAMBDA * days)


def _frequency_factor(count: int, max_count: int) -> float:
    if max_count <= 0:
        return 0.0
    return math.log(1 + count) / math.log(max_count + 1)


def search_memory(query: str, top_k: int | None = None) -> list[dict]:
    """
    Retrieve top-k most relevant memories using combined scoring.
    Updates retrieval_count and last_used_at for each hit.

    Score = semantic(0.40) + salience(0.25) + recency(0.20) + emotional(0.10) + freq(0.05)
    """
    collection = _get_chroma()
    model      = _get_embed()
    k          = top_k or _cfg["retrieval"].get("top_k", 5)

    embedding = model.encode(query).tolist()
    # Fetch more than needed so we can re-rank
    fetch_k   = min(k * 3, 50)

    try:
        results = collection.query(
            query_embeddings=[embedding],
            n_results=fetch_k,
            include=["documents", "metadatas", "distances"],
        )
    except Exception:
        return []

    docs      = results["documents"][0]
    metas     = results["metadatas"][0]
    distances = results["distances"][0]   # cosine distance: 0=identical, 2=opposite

    # Find max retrieval count for normalisation
    max_count = max((int(m.get("retrieval_count", 0)) for m in metas), default=0)

    scored: list[tuple[float, dict]] = []
    for doc, meta, dist in zip(docs, metas, distances):
        semantic  = 1.0 - (dist / 2.0)   # convert cosine distance → similarity [0,1]
        salience  = float(meta.get("salience_score",   0.5))
        emotional = float(meta.get("emotional_weight", 0.3))
        recency   = _recency_weight(float(meta.get("timestamp", time.time())))
        freq      = _frequency_factor(int(meta.get("retrieval_count", 0)), max_count)

        combined = (
            semantic  * 0.40 +
            salience  * 0.25 +
            recency   * 0.20 +
            emotional * 0.10 +
            freq      * 0.05
        )

        scored.append((combined, {
            "text":             doc,
            "combined_score":   combined,
            "semantic":         semantic,
            "salience":         salience,
            "emotional_weight": emotional,
            "recency":          recency,
            "retrieval_count":  int(meta.get("retrieval_count", 0)),
            "source":           meta.get("source", ""),
            "tags":             meta.get("tags", ""),
            "timestamp":        meta.get("timestamp"),
            "last_used_at":     meta.get("last_used_at"),
        }))

    # Re-rank by combined score, take top k
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [item for _, item in scored[:k]]

    # Update retrieval metadata in ChromaDB (best-effort, non-blocking)
    _update_retrieval_metadata(docs[:k], metas[:k], top)

    return top


def _update_retrieval_metadata(
    docs: list[str],
    metas: list[dict],
    results: list[dict],
) -> None:
    """Increment retrieval_count and update last_used_at for retrieved memories."""
    collection = _get_chroma()
    now = time.time()
    for doc, meta in zip(docs, metas):
        try:
            # Re-query by document content to get the ID
            hits = collection.query(
                query_texts=[doc],
                n_results=1,
                include=["metadatas"],
            )
            if hits["ids"] and hits["ids"][0]:
                mem_id = hits["ids"][0][0]
                new_meta = dict(meta)
                new_meta["retrieval_count"] = int(meta.get("retrieval_count", 0)) + 1
                new_meta["last_used_at"]    = now
                collection.update(ids=[mem_id], metadatas=[new_meta])
        except Exception:
            pass  # retrieval count update is best-effort
