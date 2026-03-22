"""
Shared pytest fixtures for EOS tests.

Fixtures
--------
tmp_db_path     — temporary SQLite path (deleted after test)
registry        — clean ToolRegistry instance
executor        — ToolExecutor wired to the registry
audit_store     — AuditStore backed by a temporary in-memory (or file) DB
minimal_cfg     — minimal config dict sufficient for most tests
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

# ── ToolRegistry fixture ──────────────────────────────────────────────────────

@pytest.fixture
def registry():
    from runtime.tool_registry import ToolRegistry
    return ToolRegistry()


@pytest.fixture
def executor(registry):
    from runtime.tool_executor import ToolExecutor
    return ToolExecutor(registry=registry)


# ── AuditStore fixture ────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def audit_store(tmp_db_path):
    from core.audit import AuditStore
    return AuditStore(tmp_db_path)


# ── Minimal config fixture ────────────────────────────────────────────────────

@pytest.fixture
def minimal_cfg(tmp_path):
    """Return a minimal config dict with temp paths for databases."""
    db = tmp_path / "entity_state.db"
    chroma = tmp_path / "chroma"
    chroma.mkdir()
    return {
        "db_path": str(db),
        "retrieval": {"chroma_path": str(chroma)},
        "google": {"enabled": False},
        "discord": {"enabled": False},
    }


# ── Helper: make a simple ToolSpec ────────────────────────────────────────────

def make_spec(
    name: str = "test_tool",
    handler=None,
    risk_level: str = "read_only",
    trust_level: str = "public",
    confirmation_policy: str = "none",
    enabled: bool = True,
    parameters: dict | None = None,
    timeout_seconds: int = 5,
):
    from runtime.tool_registry import ToolSpec
    if handler is None:
        handler = lambda params: '{"ok": true}'
    return ToolSpec(
        name=name,
        description=f"Test tool: {name}",
        pack="test_pack",
        tags=["test"],
        parameters=parameters or {"type": "object", "properties": {}, "required": []},
        handler=handler,
        risk_level=risk_level,
        trust_level=trust_level,
        confirmation_policy=confirmation_policy,
        enabled=enabled,
        timeout_seconds=timeout_seconds,
    )
