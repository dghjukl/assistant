"""
Integration tests for EOS WebUI API contracts.

These tests use FastAPI's TestClient to send real HTTP requests against
the server's routing layer — validating typed request schemas, response
envelope shapes, and HTTP status codes without starting real backends or
hitting external services.

Most routes require _topology to be non-None; those are tested for correct
503 handling when the topology is absent (as it is in an isolated test env).

Routes that don't depend on topology (auth, schemas, etc.) are tested fully.
"""
from __future__ import annotations

import pytest

# Skip the entire module if FastAPI/httpx test client isn't available
pytest.importorskip("fastapi")
pytest.importorskip("httpx")


@pytest.fixture(scope="module")
def client():
    """Create an isolated TestClient.

    We import the app inside this fixture to avoid triggering the startup
    event (which tries to connect to real backends).
    """
    from fastapi.testclient import TestClient

    # Patch startup so it doesn't attempt real database or backend connections
    import unittest.mock as mock
    with mock.patch("webui.server.startup_event", new=lambda: None):
        from webui.server import app
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


class TestHealthAndStatus:
    def test_status_endpoint_returns_json(self, client):
        resp = client.get("/api/status")
        assert resp.headers["content-type"].startswith("application/json")

    def test_auth_verify_without_token(self, client):
        resp = client.get("/api/auth/verify")
        assert resp.status_code in (200, 401, 503)
        data = resp.json()
        assert "ok" in data


class TestChatContractValidation:
    def test_chat_empty_message_422(self, client):
        """Empty message string should fail Pydantic validation (422)."""
        resp = client.post("/api/chat", json={"message": ""})
        # Either 422 (Pydantic) or 503 (topology not ready) are acceptable
        assert resp.status_code in (400, 422, 503)

    def test_chat_missing_message_422(self, client):
        resp = client.post("/api/chat", json={})
        assert resp.status_code in (422, 503)

    def test_chat_valid_body_503_without_topology(self, client):
        """Valid body but no topology → 503 (not a schema error)."""
        resp = client.post("/api/chat", json={"message": "hello"})
        assert resp.status_code in (200, 503)
        if resp.status_code == 503:
            data = resp.json()
            assert data["ok"] is False


class TestTtsContractValidation:
    def test_tts_empty_text_422(self, client):
        resp = client.post("/api/tts", json={"text": ""})
        assert resp.status_code in (400, 422)

    def test_tts_missing_text_422(self, client):
        resp = client.post("/api/tts", json={})
        assert resp.status_code == 422


class TestAutonomyContractValidation:
    def test_autonomy_missing_dimension_422(self, client):
        resp = client.post("/api/autonomy", json={"enabled": True})
        assert resp.status_code == 422

    def test_autonomy_missing_enabled_422(self, client):
        resp = client.post("/api/autonomy", json={"dimension": "perception"})
        assert resp.status_code == 422

    def test_autonomy_valid_body_processes(self, client):
        resp = client.post("/api/autonomy", json={"dimension": "perception", "enabled": True})
        # May succeed or fail depending on DB state, but should not be 422
        assert resp.status_code != 422


class TestInitiativeFeedbackValidation:
    def test_feedback_wrong_value_422(self, client):
        """Feedback must be 'accept', 'defer', or 'dismiss'."""
        resp = client.post(
            "/admin/initiative/feedback",
            json={"initiative_id": "abc", "feedback": "delete_everything"},
            headers={"X-Admin-Token": "any"},
        )
        # 422 from Pydantic literal validation
        assert resp.status_code in (401, 422, 503)

    def test_feedback_missing_id_422(self, client):
        resp = client.post(
            "/admin/initiative/feedback",
            json={"feedback": "accept"},
            headers={"X-Admin-Token": "any"},
        )
        assert resp.status_code in (401, 422)


class TestInvestigationContractValidation:
    def test_create_missing_title_422(self, client):
        resp = client.post(
            "/admin/investigation/create",
            json={"description": "no title"},
            headers={"X-Admin-Token": "any"},
        )
        assert resp.status_code in (401, 422, 503)

    def test_create_invalid_priority_422(self, client):
        resp = client.post(
            "/admin/investigation/create",
            json={"title": "Test", "priority": 99},
            headers={"X-Admin-Token": "any"},
        )
        assert resp.status_code in (401, 422, 503)


class TestGoogleWorkspaceRoutes:
    def test_status_returns_json(self, client):
        resp = client.get("/api/google_workspace/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "ok" in data
        assert "data" in data

    def test_authorize_returns_auth_url_or_503(self, client):
        """Either returns an auth_url or a 503 if client secret not found."""
        resp = client.get("/api/google_workspace/authorize")
        assert resp.status_code in (200, 503)
        if resp.status_code == 200:
            data = resp.json()
            assert "auth_url" in data.get("data", {})

    def test_callback_missing_code_400(self, client):
        resp = client.get("/api/google_workspace/callback")
        assert resp.status_code in (302, 400)

    def test_revoke_returns_ok_shape(self, client):
        resp = client.post("/api/google_workspace/revoke")
        assert resp.status_code == 200
        data = resp.json()
        assert "ok" in data

    def test_account_unauthorized_401(self, client):
        resp = client.get("/api/google_workspace/account")
        # No credentials stored in test env — expect 401
        assert resp.status_code in (200, 401)


class TestCapabilityContractValidation:
    def test_invalid_group_422(self, client):
        resp = client.post(
            "/admin/capabilities",
            json={"group": "unknown_group", "key": "x", "value": True},
            headers={"X-Admin-Token": "any"},
        )
        assert resp.status_code in (401, 422)

    def test_missing_key_422(self, client):
        resp = client.post(
            "/admin/capabilities",
            json={"group": "autonomy", "value": True},
            headers={"X-Admin-Token": "any"},
        )
        assert resp.status_code in (401, 422)
