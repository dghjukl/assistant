"""Unit tests for core.db_migrations migration runner."""
from __future__ import annotations

import sqlite3

import pytest
from core.db_migrations import (
    apply_migrations,
    list_applied,
    pending_count,
    _MIGRATIONS,
    _ensure_migrations_table,
)


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "test_mig.db"
    c = sqlite3.connect(str(db))
    c.execute("PRAGMA journal_mode=WAL")
    yield c
    c.close()


class TestMigrationRunner:
    def test_apply_creates_migrations_table(self, conn):
        apply_migrations(conn, "entity_state")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "schema_migrations" in tables

    def test_apply_is_idempotent(self, conn):
        # Create the tables that migrations depend on first
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS interaction_log (
                id TEXT, role TEXT, content TEXT, timestamp REAL, metadata TEXT
            );
            CREATE TABLE IF NOT EXISTS autonomy_profile (
                dimension TEXT, enabled INTEGER
            );
        """)
        conn.commit()
        n1 = apply_migrations(conn, "entity_state")
        n2 = apply_migrations(conn, "entity_state")
        assert n2 == 0  # second run applies nothing

    def test_unknown_db_name_applies_nothing(self, conn):
        n = apply_migrations(conn, "nonexistent_db")
        assert n == 0

    def test_list_applied_empty_before_run(self, conn):
        _ensure_migrations_table(conn)
        conn.commit()
        applied = list_applied(conn, "audit")
        assert applied == []

    def test_pending_count_decreases_after_apply(self, conn):
        # Build prerequisite tables for audit migrations
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS admin_actions (id TEXT);
            CREATE TABLE IF NOT EXISTS tool_executions (id TEXT, pack TEXT);
        """)
        conn.commit()
        before = pending_count(conn, "audit")
        apply_migrations(conn, "audit")
        after = pending_count(conn, "audit")
        assert after == 0
        assert before >= after

    def test_list_applied_after_run(self, conn):
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS admin_actions (id TEXT);
            CREATE TABLE IF NOT EXISTS tool_executions (id TEXT, pack TEXT);
        """)
        conn.commit()
        apply_migrations(conn, "audit")
        applied = list_applied(conn, "audit")
        assert len(applied) == len(_MIGRATIONS.get("audit", []))
        assert all("id" in a and "applied_at" in a for a in applied)
