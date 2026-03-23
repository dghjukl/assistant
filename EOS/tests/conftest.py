"""
Shared pytest fixtures for EOS tests.

Fixtures
--------
tmp_db_path     — temporary SQLite path (deleted after test)
registry        — clean ToolRegistry instance
executor        — ToolExecutor wired to the registry
audit_store     — AuditStore backed by a temporary in-memory (or file) DB
minimal_cfg     — minimal config dict sufficient for most tests
backend_server_factory — lightweight fake HTTP backend servers for integration tests
runtime_topology_factory — helper to build realistic RuntimeTopology instances
"""
from __future__ import annotations

import json
import threading
from dataclasses import fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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


# ── Global runtime reset ──────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_global_runtime_state():
    """Reset singleton-ish global state mutated by integration tests."""
    from webui.app_state import AppState, app_state

    fresh_state = AppState()
    for field in fields(AppState):
        setattr(app_state, field.name, getattr(fresh_state, field.name))

    try:
        import runtime.orchestrator as orch

        orch._tool_executor = None
        orch._faculty = None
        orch._conv.clear()
    except Exception:
        pass

    yield

    fresh_state = AppState()
    for field in fields(AppState):
        setattr(app_state, field.name, getattr(fresh_state, field.name))

    try:
        import runtime.orchestrator as orch

        orch._tool_executor = None
        orch._faculty = None
        orch._conv.clear()
    except Exception:
        pass


# ── Lightweight fake backend HTTP server ─────────────────────────────────────

class _BackendHandler(BaseHTTPRequestHandler):
    server_version = "EOSFakeBackend/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args):
        return

    def _dispatch(self):
        length = int(self.headers.get("content-length", "0") or 0)
        raw_body = self.rfile.read(length) if length else b""
        body = None
        if raw_body:
            try:
                body = json.loads(raw_body.decode("utf-8"))
            except Exception:
                body = raw_body.decode("utf-8", errors="replace")

        route = self.server.routes.get((self.command, self.path))
        if route is None:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            payload = json.dumps({"ok": False, "error": f"no route for {self.command} {self.path}"}).encode("utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if callable(route):
            route = route(body, self.headers)

        status = int(route.get("status", 200))
        payload_obj = route.get("json")
        payload_bytes = route.get("body")
        headers = dict(route.get("headers", {}))
        if payload_obj is not None:
            payload_bytes = json.dumps(payload_obj).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif payload_bytes is None:
            payload_bytes = b""
        elif isinstance(payload_bytes, str):
            payload_bytes = payload_bytes.encode("utf-8")

        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload_bytes)))
        self.end_headers()
        if payload_bytes:
            self.wfile.write(payload_bytes)

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()


class FakeBackendServer:
    def __init__(self, routes: dict[tuple[str, str], Any]):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _BackendHandler)
        self._server.routes = routes
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def endpoint(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


@pytest.fixture
def backend_server_factory():
    created: list[FakeBackendServer] = []

    def _factory(*, health_status: int = 200, health_body: str = "ok", chat_reply: str = "hello", extra_routes: dict[tuple[str, str], Any] | None = None):
        routes: dict[tuple[str, str], Any] = {
            ("GET", "/health"): {"status": health_status, "body": health_body, "headers": {"Content-Type": "text/plain"}},
            ("POST", "/v1/chat/completions"): {
                "json": {
                    "choices": [
                        {"message": {"content": chat_reply}}
                    ]
                }
            },
            ("POST", "/v1/completions"): {
                "json": {
                    "choices": [
                        {"text": chat_reply}
                    ]
                }
            },
        }
        routes.update(extra_routes or {})
        server = FakeBackendServer(routes)
        created.append(server)
        return server

    yield _factory

    for server in created:
        server.close()


# ── Runtime topology factory ──────────────────────────────────────────────────

@pytest.fixture
def runtime_topology_factory():
    from runtime.topology import RuntimeTopology, ServerState, ServerStatus

    def _factory(*, primary: str = "ready", tool: str = "absent", thinking: str = "absent", creativity: str = "absent", vision: str = "absent", primary_multimodal: bool = False):
        status_map = {
            "ready": ServerStatus.READY,
            "absent": ServerStatus.ABSENT,
            "error": ServerStatus.ERROR,
            "pending": ServerStatus.PENDING,
            "starting": ServerStatus.STARTING,
        }
        ports = {
            "primary": 18080,
            "tool": 18081,
            "thinking": 18082,
            "creativity": 18083,
            "vision": 18084,
        }
        servers = {}
        for role, state_name in {
            "primary": primary,
            "tool": tool,
            "thinking": thinking,
            "creativity": creativity,
            "vision": vision,
        }.items():
            port = ports[role]
            servers[role] = ServerState(
                role=role,
                port=port,
                endpoint=f"http://127.0.0.1:{port}",
                status=status_map[state_name],
                required=(role == "primary"),
                error=("forced error" if state_name == "error" else None),
            )
        return RuntimeTopology(
            deployment_mode="vision" if primary_multimodal else "standard",
            primary_multimodal=primary_multimodal,
            servers=servers,
        )

    return _factory


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
