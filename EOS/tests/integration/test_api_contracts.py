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

import sys
import types

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
    with mock.patch.dict("os.environ", {"EOS_TRUST_PROXY": "1"}):
        with mock.patch("webui.server.startup_event", new=lambda: None):
            from webui.server import app
            with TestClient(
                app,
                raise_server_exceptions=False,
                base_url="http://127.0.0.1:7860",
                headers={"X-Real-IP": "127.0.0.1"},
            ) as c:
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
    def test_chat_empty_body_422(self, client):
        """Neither text nor message provided — Pydantic validator rejects it."""
        resp = client.post("/api/chat", json={})
        assert resp.status_code in (422, 503)

    def test_chat_text_field_accepted(self, client):
        """Frontend sends 'text' — should be accepted (503 without topology, not 422)."""
        resp = client.post("/api/chat", json={"text": "hello"})
        assert resp.status_code in (200, 503)
        if resp.status_code == 503:
            assert resp.json()["ok"] is False

    def test_chat_message_field_accepted(self, client):
        """Legacy 'message' field is still accepted for backward compatibility."""
        resp = client.post("/api/chat", json={"message": "hello"})
        assert resp.status_code in (200, 503)
        if resp.status_code == 503:
            assert resp.json()["ok"] is False

    def test_chat_response_shape_when_successful(self, client):
        """When /api/chat succeeds, response must have flat 'response' key (not data.response)."""
        # We can only verify the schema when a real response comes back;
        # without topology this just checks 503 shape.
        resp = client.post("/api/chat", json={"text": "hello"})
        data = resp.json()
        assert "ok" in data
        if data["ok"]:
            assert "response" in data          # flat, not nested under "data"
            assert "data" not in data          # no legacy wrapper


class TestUploadContractValidation:
    def test_upload_missing_fields_422(self, client):
        """Upload with no body is rejected with 422."""
        resp = client.post("/api/upload", json={})
        assert resp.status_code == 422

    def test_upload_invalid_base64_400(self, client):
        """Upload with malformed base64 data returns 400."""
        resp = client.post("/api/upload", json={
            "filename": "test.txt",
            "content_type": "text/plain",
            "data": "!!!not-valid-base64!!!",
        })
        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_upload_valid_returns_flat_fields(self, client, tmp_path, monkeypatch):
        """Valid upload returns file_id, filename, content_type, kind at root level."""
        import base64, webui.server as srv
        monkeypatch.setattr(srv, "_cfg", {"upload_dir": str(tmp_path)}, raising=False)

        payload = base64.b64encode(b"hello world").decode()
        resp = client.post("/api/upload", json={
            "filename": "hello.txt",
            "content_type": "text/plain",
            "data": payload,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "file_id" in data             # flat, not nested under "data"
        assert data["filename"] == "hello.txt"
        assert data["content_type"] == "text/plain"
        assert data["kind"] == "text"
        assert "data" not in data            # no legacy wrapper


class TestApiResponseKeys:
    def test_tools_response_uses_tools_key(self, client):
        """GET /api/tools must return {ok, tools: [...]} not {ok, data: [...]}."""
        resp = client.get("/api/tools")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "tools" in data
        assert "data" not in data            # key renamed from legacy

    def test_memory_recent_uses_memories_key(self, client):
        """GET /api/memory/recent must return {ok, memories: [...]}."""
        resp = client.get("/api/memory/recent")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "memories" in data
        assert "data" not in data

    def test_vision_settings_get_flat(self, client):
        """GET /api/vision/settings must return 'enabled' at root level."""
        resp = client.get("/api/vision/settings")
        # 503 when topology is absent — check shape regardless
        data = resp.json()
        if data.get("ok"):
            assert "enabled" in data
            assert "data" not in data


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
    @staticmethod
    def _install_fake_google(monkeypatch, tmp_path):
        class _Exec:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class _CalendarEvents:
            def list(self, **_kwargs):
                return _Exec({
                    "items": [
                        {
                            "id": "evt-1",
                            "summary": "Standup",
                            "description": "Daily sync",
                            "start": {"dateTime": "2026-03-22T09:00:00Z"},
                            "end": {"dateTime": "2026-03-22T09:15:00Z"},
                        }
                    ]
                })

        class _CalendarService:
            def events(self):
                return _CalendarEvents()

        class _GmailMessages:
            def __init__(self):
                self.last_list_kwargs = {}

            def list(self, **kwargs):
                self.last_list_kwargs = kwargs
                return _Exec({"messages": [{"id": "msg-1"}]})

            def get(self, **_kwargs):
                return _Exec({
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Hello"},
                            {"name": "From", "value": "alice@example.com"},
                            {"name": "Date", "value": "2026-03-22"},
                        ]
                    },
                    "snippet": "Snippet",
                })

        class _GmailUsers:
            def __init__(self):
                self.messages_api = _GmailMessages()

            def messages(self):
                return self.messages_api

        class _GmailService:
            def __init__(self):
                self.users_api = _GmailUsers()

            def users(self):
                return self.users_api

        class _DriveFiles:
            def __init__(self):
                self.last_list_kwargs = {}

            def list(self, **kwargs):
                self.last_list_kwargs = kwargs
                return _Exec({
                    "files": [
                        {
                            "id": "file-1",
                            "name": "Spec Doc",
                            "mimeType": "text/plain",
                            "modifiedTime": "2026-03-22T10:00:00Z",
                            "webViewLink": "https://example.com/file-1",
                        }
                    ]
                })

        class _DriveService:
            def __init__(self):
                self.files_api = _DriveFiles()

            def files(self):
                return self.files_api

        services = {
            "calendar": _CalendarService(),
            "gmail": _GmailService(),
            "drive": _DriveService(),
        }
        token_path = tmp_path / "google_token.json"
        token_path.write_text("{}", encoding="utf-8")
        client_secret = tmp_path / "client_secret.json"
        client_secret.write_text("{}", encoding="utf-8")

        fake_google = types.SimpleNamespace(
            configure=lambda cfg: None,
            is_authorized=lambda: True,
            get_account_info=lambda: {"email": "tester@example.com"},
            get_credentials=lambda scopes=None: object(),
            build_authorize_url=lambda redirect_uri: ("https://accounts.example/auth", "state-123"),
            exchange_code=lambda code, state, redirect_uri: {"ok": True, "account": {"email": "tester@example.com"}},
            revoke=lambda: {"ok": True},
            build_service=lambda service_name, version, scopes=None: services[service_name],
            _client_secret_path=lambda: client_secret,
            _token_path=lambda: token_path,
        )
        monkeypatch.setitem(sys.modules, "core.google_oauth", fake_google)

        import webui.server as server
        monkeypatch.setattr(server, "_cfg", {
            "google": {
                "enabled": True,
                "calendar_enabled": True,
                "gmail_enabled": True,
                "drive_enabled": True,
            }
        }, raising=False)
        return services

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

    def test_calendar_today_returns_structured_events(self, client, monkeypatch, tmp_path):
        self._install_fake_google(monkeypatch, tmp_path)
        resp = client.get("/api/google_workspace/calendar/today")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        events = data["data"]["events"]
        assert isinstance(events, list)
        assert events[0]["summary"] == "Standup"

    def test_gmail_inbox_honors_query_parameter(self, client, monkeypatch, tmp_path):
        services = self._install_fake_google(monkeypatch, tmp_path)
        resp = client.get("/api/google_workspace/gmail/inbox?query=from%3Aalice&max_results=5")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["query"] == "from:alice"
        assert services["gmail"].users_api.messages_api.last_list_kwargs["q"] == "from:alice"

    def test_drive_search_accepts_q_alias(self, client, monkeypatch, tmp_path):
        services = self._install_fake_google(monkeypatch, tmp_path)
        resp = client.get("/api/google_workspace/drive/search?q=spec")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["query"] == "spec"
        assert services["drive"].files_api.last_list_kwargs["q"] == "fullText contains 'spec'"

    def test_calendar_today_backend_exception_returns_ok_false(self, client, monkeypatch, tmp_path):
        """Calendar backend exception must return ok:False — must not silently return ok:True."""
        import types, sys
        fake_google = types.SimpleNamespace(
            configure=lambda cfg: None,
            is_authorized=lambda: True,
            get_account_info=lambda: {},
            get_credentials=lambda scopes=None: object(),
            build_authorize_url=lambda redirect_uri: ("https://accounts.example/auth", "state"),
            exchange_code=lambda code, state, redirect_uri: {"ok": True},
            revoke=lambda: {"ok": True},
            build_service=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("backend failure")),
            _client_secret_path=lambda: tmp_path / "cs.json",
            _token_path=lambda: tmp_path / "tok.json",
        )
        monkeypatch.setitem(sys.modules, "core.google_oauth", fake_google)
        import webui.server as server
        monkeypatch.setattr(server, "_cfg", {
            "google": {"enabled": True, "calendar_enabled": True}
        }, raising=False)
        resp = client.get("/api/google_workspace/calendar/today")
        data = resp.json()
        assert data["ok"] is False, (
            f"Expected ok:False on backend exception; got ok:{data.get('ok')} — "
            f"calendar endpoint is masking errors"
        )

    def test_calendar_upcoming_backend_exception_returns_ok_false(self, client, monkeypatch, tmp_path):
        """Calendar/upcoming backend exception must return ok:False."""
        import types, sys
        fake_google = types.SimpleNamespace(
            configure=lambda cfg: None,
            is_authorized=lambda: True,
            get_account_info=lambda: {},
            get_credentials=lambda scopes=None: object(),
            build_authorize_url=lambda redirect_uri: ("https://accounts.example/auth", "state"),
            exchange_code=lambda code, state, redirect_uri: {"ok": True},
            revoke=lambda: {"ok": True},
            build_service=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("backend failure")),
            _client_secret_path=lambda: tmp_path / "cs.json",
            _token_path=lambda: tmp_path / "tok.json",
        )
        monkeypatch.setitem(sys.modules, "core.google_oauth", fake_google)
        import webui.server as server
        monkeypatch.setattr(server, "_cfg", {
            "google": {"enabled": True, "calendar_enabled": True}
        }, raising=False)
        resp = client.get("/api/google_workspace/calendar/upcoming")
        data = resp.json()
        assert data["ok"] is False, (
            f"Expected ok:False on backend exception; got ok:{data.get('ok')} — "
            f"calendar/upcoming endpoint is masking errors"
        )


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
