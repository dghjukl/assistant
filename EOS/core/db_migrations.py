"""
Database migration runner for EOS SQLite stores.

Every SQLite database in EOS registers its own ordered list of migration
functions here.  On startup, ``apply_migrations(conn, db_name)`` runs any
migrations that have not yet been recorded in the ``schema_migrations`` table,
then stamps them as applied.

Adding a migration
------------------
1. Write a function  ``def mNNN_description(conn): ...``  that applies the
   DDL / DML changes.  The function receives an open sqlite3.Connection and
   must NOT commit — the runner commits after each successful migration.
2. Append it to the relevant registry list below.
3. The order of the list is authoritative; never re-order or remove entries.

The migration table itself is created automatically on the first call to
``apply_migrations``.  If ``schema_migrations`` already exists but the
``db_name`` column doesn't, the runner performs a safe upgrade.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

# ── Types ─────────────────────────────────────────────────────────────────────

MigrationFn = Callable[[sqlite3.Connection], None]

# ── entity_state.db migrations ───────────────────────────────────────────────


def _entity_m001_add_interaction_metadata(conn: sqlite3.Connection) -> None:
    """Ensure interaction_log.metadata column exists (added in v1)."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "interaction_log" not in tables:
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(interaction_log)")}
    if "metadata" not in cols:
        conn.execute("ALTER TABLE interaction_log ADD COLUMN metadata TEXT")


def _entity_m002_add_entity_meta_table(conn: sqlite3.Connection) -> None:
    """Ensure entity_meta key/value table exists."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)


def _entity_m003_add_autonomy_updated_at(conn: sqlite3.Connection) -> None:
    """Ensure autonomy_profile.updated_at column exists."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "autonomy_profile" not in tables:
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(autonomy_profile)")}
    if "updated_at" not in cols:
        conn.execute(
            "ALTER TABLE autonomy_profile ADD COLUMN updated_at REAL NOT NULL DEFAULT 0"
        )


def _entity_m004_interaction_log_indexes(conn: sqlite3.Connection) -> None:
    """Add performance indexes to interaction_log."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "interaction_log" not in tables:
        return
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_interaction_ts "
        "ON interaction_log (timestamp)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_interaction_role "
        "ON interaction_log (role)"
    )


def _entity_m005_add_external_inference_ledger(conn: sqlite3.Connection) -> None:
    """Create the external_inference_ledger table for upgrades from pre-EI installs.

    Fresh installs have this table created by init_db(); this migration ensures
    existing databases are upgraded transparently.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS external_inference_ledger (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                   TEXT    NOT NULL,
            epoch_ts             REAL    NOT NULL,
            provider             TEXT    NOT NULL DEFAULT 'huggingface',
            request_origin_tier  TEXT    NOT NULL DEFAULT 'localhost',
            request_origin_ip    TEXT    NOT NULL DEFAULT '',
            request_reason       TEXT    NOT NULL DEFAULT '',
            model_id             TEXT    NOT NULL DEFAULT '',
            estimated_cost_usd   REAL    NOT NULL DEFAULT 0.0,
            actual_cost_usd      REAL,
            tokens_input         INTEGER,
            tokens_output        INTEGER,
            approval_mode        TEXT    NOT NULL DEFAULT '',
            auto_approved        INTEGER NOT NULL DEFAULT 0,
            succeeded            INTEGER NOT NULL DEFAULT 0,
            denied               INTEGER NOT NULL DEFAULT 0,
            denial_reason        TEXT,
            billing_cycle_start  TEXT    NOT NULL DEFAULT '',
            response_latency_ms  INTEGER,
            error_detail         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_eil_epoch ON external_inference_ledger(epoch_ts);
        CREATE INDEX IF NOT EXISTS idx_eil_cycle ON external_inference_ledger(billing_cycle_start);
    """)


# ── audit.db migrations ───────────────────────────────────────────────────────


def _audit_m001_add_actor_index(conn: sqlite3.Connection) -> None:
    """Add actor index to admin_actions for faster per-actor queries."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(admin_actions)")}
    if "actor" not in cols:
        return
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_actions_actor "
        "ON admin_actions (actor)"
    )


def _audit_m002_add_pack_index(conn: sqlite3.Connection) -> None:
    """Add pack index to tool_executions for pack-level aggregation."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tool_executions)")}
    if "pack" not in cols:
        return
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tool_exec_pack "
        "ON tool_executions (pack)"
    )


def _audit_m003_add_origin_to_admin_actions(conn: sqlite3.Connection) -> None:
    """Add origin_tier and client_ip columns to admin_actions."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(admin_actions)")}
    if not cols:
        return
    if "origin_tier" not in cols:
        conn.execute("ALTER TABLE admin_actions ADD COLUMN origin_tier TEXT")
    if "client_ip" not in cols:
        conn.execute("ALTER TABLE admin_actions ADD COLUMN client_ip TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_actions_origin "
        "ON admin_actions (origin_tier)"
    )


def _audit_m004_add_origin_to_tool_executions(conn: sqlite3.Connection) -> None:
    """Add origin_tier and client_ip columns to tool_executions."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tool_executions)")}
    if not cols:
        return
    if "origin_tier" not in cols:
        conn.execute("ALTER TABLE tool_executions ADD COLUMN origin_tier TEXT")
    if "client_ip" not in cols:
        conn.execute("ALTER TABLE tool_executions ADD COLUMN client_ip TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tool_exec_origin "
        "ON tool_executions (origin_tier)"
    )


# ── Registry ─────────────────────────────────────────────────────────────────

_MIGRATIONS: Dict[str, List[tuple[str, MigrationFn]]] = {
    "entity_state": [
        ("m001_add_interaction_metadata",          _entity_m001_add_interaction_metadata),
        ("m002_add_entity_meta_table",             _entity_m002_add_entity_meta_table),
        ("m003_add_autonomy_updated_at",           _entity_m003_add_autonomy_updated_at),
        ("m004_interaction_log_indexes",           _entity_m004_interaction_log_indexes),
        ("m005_add_external_inference_ledger",     _entity_m005_add_external_inference_ledger),
    ],
    "audit": [
        ("m001_add_actor_index",                 _audit_m001_add_actor_index),
        ("m002_add_pack_index",                  _audit_m002_add_pack_index),
        ("m003_add_origin_to_admin_actions",     _audit_m003_add_origin_to_admin_actions),
        ("m004_add_origin_to_tool_executions",   _audit_m004_add_origin_to_tool_executions),
    ],
}


# ── Runner ────────────────────────────────────────────────────────────────────

def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    """Create (or upgrade) the schema_migrations tracking table."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id         TEXT NOT NULL,
            db_name    TEXT NOT NULL DEFAULT '',
            applied_at REAL NOT NULL,
            PRIMARY KEY (id, db_name)
        )
    """)
    # Upgrade: add db_name column if this table predates the multi-db design
    cols = {row[1] for row in conn.execute("PRAGMA table_info(schema_migrations)")}
    if "db_name" not in cols:
        conn.execute(
            "ALTER TABLE schema_migrations ADD COLUMN db_name TEXT NOT NULL DEFAULT ''"
        )


def _infer_db_name(conn_or_path: sqlite3.Connection | str | Path) -> str:
    if isinstance(conn_or_path, sqlite3.Connection):
        return "entity_state"
    stem = Path(conn_or_path).stem.lower()
    if "audit" in stem:
        return "audit"
    return "entity_state"


def apply_migrations(conn: sqlite3.Connection | str | Path, db_name: str | None = None) -> int:
    """Apply all pending migrations for *db_name* to the open connection.

    Parameters
    ----------
    conn
        An open ``sqlite3.Connection`` (WAL mode recommended).
    db_name
        Registry key identifying which migration list to use.
        Must match a key in ``_MIGRATIONS``.

    Returns
    -------
    int
        Number of migrations newly applied.
    """
    if not isinstance(conn, sqlite3.Connection):
        db_path = Path(conn)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path)) as db_conn:
            return apply_migrations(db_conn, db_name or _infer_db_name(db_path))

    db_name = db_name or _infer_db_name(conn)

    _ensure_migrations_table(conn)
    conn.commit()

    migrations = _MIGRATIONS.get(db_name, [])
    if not migrations:
        logger.debug("[migrations] No migrations registered for db '%s'", db_name)
        return 0

    # Collect already-applied IDs for this db
    applied = {
        row[0]
        for row in conn.execute(
            "SELECT id FROM schema_migrations WHERE db_name = ?", (db_name,)
        )
    }

    applied_count = 0
    for migration_id, fn in migrations:
        if migration_id in applied:
            continue
        logger.info("[migrations] Applying %s/%s", db_name, migration_id)
        try:
            fn(conn)
            conn.execute(
                "INSERT INTO schema_migrations (id, db_name, applied_at) VALUES (?, ?, ?)",
                (migration_id, db_name, time.time()),
            )
            conn.commit()
            applied_count += 1
        except Exception as exc:
            conn.rollback()
            logger.error(
                "[migrations] Migration %s/%s FAILED: %s — rolling back",
                db_name, migration_id, exc,
            )
            raise

    if applied_count:
        logger.info("[migrations] Applied %d migration(s) to '%s'", applied_count, db_name)
    else:
        logger.debug("[migrations] '%s' is up to date", db_name)

    return applied_count


def list_applied(conn: sqlite3.Connection, db_name: str) -> list[dict]:
    """Return all applied migrations for *db_name*, newest first."""
    _ensure_migrations_table(conn)
    rows = conn.execute(
        "SELECT id, applied_at FROM schema_migrations "
        "WHERE db_name = ? ORDER BY applied_at DESC",
        (db_name,),
    ).fetchall()
    return [{"id": r[0], "applied_at": r[1]} for r in rows]


def pending_count(conn: sqlite3.Connection, db_name: str) -> int:
    """Return the number of migrations not yet applied for *db_name*."""
    _ensure_migrations_table(conn)
    applied = {
        row[0]
        for row in conn.execute(
            "SELECT id FROM schema_migrations WHERE db_name = ?", (db_name,)
        )
    }
    return sum(1 for mid, _ in _MIGRATIONS.get(db_name, []) if mid not in applied)
