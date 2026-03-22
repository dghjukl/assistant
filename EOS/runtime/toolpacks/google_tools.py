"""Google Workspace Tools — Calendar, Gmail, Drive integration

Configuration (config.json):
  google:
    enabled: true
    token_path: data/google_token.json
    client_secret_path: "AI personal files/client_secret_*.json"

Tools registered
----------------
- list_calendar_events  — list upcoming Calendar events
- search_gmail          — search Gmail messages
- list_drive_files      — list recent Drive files
- search_drive          — search Drive files by query

All tools degrade gracefully to a "not authorized" error when credentials
are absent; the user is directed to authorize via the admin panel.
"""
from __future__ import annotations

import json
from typing import Any, Dict


def _jdump(x: Any) -> str:
    try:
        return json.dumps(x, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


def _not_authorized() -> str:
    return _jdump({
        "error": "Google not authorized",
        "hint": "Open the admin panel and click 'Authorize Google' to complete OAuth setup.",
    })


def _not_configured() -> str:
    return _jdump({
        "error": "Google Workspace tools not configured",
        "hint": (
            "Set google.enabled=true in config.json and place your "
            "OAuth client secret in config/google/client_secret_*.json"
        ),
    })


def register(registry: Any, config: Dict[str, Any]) -> None:
    from runtime.tool_registry import ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy

    gw_cfg = config.get("google", {}) if isinstance(config, dict) else {}
    enabled = bool(gw_cfg.get("enabled", False))

    def _service_disabled(service: str) -> str:
        return _jdump({
            "error": f"Google {service} integration is disabled",
            "hint": f"Set google.{service}_enabled=true in config.json and restart EOS.",
        })

    # ── Placeholder path: Google not enabled or libraries missing ─────────────

    try:
        from google.auth.transport.requests import Request  # noqa: F401
        google_available = True
    except ImportError:
        google_available = False

    if not google_available or not enabled:

        def _placeholder(_params: Dict[str, Any]) -> str:
            return _not_configured() if not enabled else _jdump({
                "error": "Google auth libraries not installed",
                "hint": "pip install google-auth google-auth-oauthlib google-api-python-client",
            })

        registry.register(ToolSpec(
            name="list_calendar_events",
            description="List Google Calendar events (Google not configured).",
            pack="google_tools",
            tags=["google", "calendar"],
            parameters={"type": "object", "properties": {
                "days": {"type": "integer", "default": 7}
            }, "required": []},
            handler=_placeholder,
            risk_level=ToolRiskLevel.READ_ONLY,
            trust_level=ToolTrustLevel.VERIFIED_USER,
            confirmation_policy=ConfirmationPolicy.NONE,
            enabled=False,
        ))
        registry.register(ToolSpec(
            name="search_gmail",
            description="Search Gmail messages (Google not configured).",
            pack="google_tools",
            tags=["google", "gmail"],
            parameters={"type": "object", "properties": {
                "query": {"type": "string"}
            }, "required": ["query"]},
            handler=_placeholder,
            risk_level=ToolRiskLevel.READ_ONLY,
            trust_level=ToolTrustLevel.VERIFIED_USER,
            confirmation_policy=ConfirmationPolicy.NONE,
            enabled=False,
        ))
        registry.register(ToolSpec(
            name="list_drive_files",
            description="List Google Drive files (Google not configured).",
            pack="google_tools",
            tags=["google", "drive"],
            parameters={"type": "object", "properties": {
                "max_results": {"type": "integer", "default": 10}
            }, "required": []},
            handler=_placeholder,
            risk_level=ToolRiskLevel.READ_ONLY,
            trust_level=ToolTrustLevel.VERIFIED_USER,
            confirmation_policy=ConfirmationPolicy.NONE,
            enabled=False,
        ))
        registry.register(ToolSpec(
            name="search_drive",
            description="Search Google Drive files (Google not configured).",
            pack="google_tools",
            tags=["google", "drive"],
            parameters={"type": "object", "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 10},
            }, "required": ["query"]},
            handler=_placeholder,
            risk_level=ToolRiskLevel.READ_ONLY,
            trust_level=ToolTrustLevel.VERIFIED_USER,
            confirmation_policy=ConfirmationPolicy.NONE,
            enabled=False,
        ))
        return

    # ── Live path: Google enabled and libraries present ───────────────────────

    def _list_calendar_events(params: Dict[str, Any]) -> str:
        if not gw_cfg.get("calendar_enabled", False):
            return _service_disabled("calendar")
        days = int(params.get("days", 7))
        try:
            from core.google_oauth import configure as oauth_cfg, build_service
            oauth_cfg(config)
            svc = build_service("calendar", "v3",
                                scopes=["https://www.googleapis.com/auth/calendar.readonly"])
            from datetime import datetime, timedelta
            now = datetime.utcnow().isoformat() + "Z"
            end = (datetime.utcnow() + timedelta(days=days)).isoformat() + "Z"
            res = svc.events().list(
                calendarId="primary",
                timeMin=now, timeMax=end,
                maxResults=25, singleEvents=True, orderBy="startTime",
            ).execute()
            events = [
                {
                    "summary": e.get("summary", "(no title)"),
                    "start":   e["start"].get("dateTime", e["start"].get("date", "")),
                    "end":     e["end"].get("dateTime",   e["end"].get("date", "")),
                    "id":      e.get("id", ""),
                }
                for e in res.get("items", [])
            ]
            return _jdump({"events": events, "count": len(events), "days": days})
        except PermissionError:
            return _not_authorized()
        except Exception as exc:
            return _jdump({"error": str(exc)})

    def _search_gmail(params: Dict[str, Any]) -> str:
        if not gw_cfg.get("gmail_enabled", False):
            return _service_disabled("gmail")
        query = str(params.get("query", ""))
        max_results = int(params.get("max_results", 10))
        if not query:
            return _jdump({"error": "query is required"})
        try:
            from core.google_oauth import configure as oauth_cfg, build_service
            oauth_cfg(config)
            svc = build_service("gmail", "v1",
                                scopes=["https://www.googleapis.com/auth/gmail.readonly"])
            response = svc.users().messages().list(
                userId="me", q=query, maxResults=max_results
            ).execute()
            messages = []
            for msg_ref in response.get("messages", []):
                msg = svc.users().messages().get(
                    userId="me", id=msg_ref["id"], format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                ).execute()
                headers = {
                    h["name"]: h["value"]
                    for h in msg.get("payload", {}).get("headers", [])
                }
                messages.append({
                    "id":      msg_ref["id"],
                    "subject": headers.get("Subject", ""),
                    "from":    headers.get("From", ""),
                    "date":    headers.get("Date", ""),
                    "snippet": msg.get("snippet", ""),
                })
            return _jdump({"query": query, "messages": messages, "count": len(messages)})
        except PermissionError:
            return _not_authorized()
        except Exception as exc:
            return _jdump({"error": str(exc)})

    def _list_drive_files(params: Dict[str, Any]) -> str:
        if not gw_cfg.get("drive_enabled", False):
            return _service_disabled("drive")
        max_results = int(params.get("max_results", 10))
        try:
            from core.google_oauth import configure as oauth_cfg, build_service
            oauth_cfg(config)
            svc = build_service("drive", "v3",
                                scopes=["https://www.googleapis.com/auth/drive.readonly"])
            response = svc.files().list(
                pageSize=max_results,
                orderBy="modifiedTime desc",
                fields="files(id,name,mimeType,modifiedTime,webViewLink)",
            ).execute()
            return _jdump({"files": response.get("files", [])})
        except PermissionError:
            return _not_authorized()
        except Exception as exc:
            return _jdump({"error": str(exc)})

    def _search_drive(params: Dict[str, Any]) -> str:
        if not gw_cfg.get("drive_enabled", False):
            return _service_disabled("drive")
        query = str(params.get("query", ""))
        max_results = int(params.get("max_results", 10))
        if not query:
            return _jdump({"error": "query is required"})
        try:
            from core.google_oauth import configure as oauth_cfg, build_service
            oauth_cfg(config)
            svc = build_service("drive", "v3",
                                scopes=["https://www.googleapis.com/auth/drive.readonly"])
            response = svc.files().list(
                q=f"fullText contains '{query}'",
                pageSize=max_results,
                fields="files(id,name,mimeType,modifiedTime,webViewLink)",
            ).execute()
            return _jdump({"query": query, "files": response.get("files", [])})
        except PermissionError:
            return _not_authorized()
        except Exception as exc:
            return _jdump({"error": str(exc)})

    registry.register(ToolSpec(
        name="list_calendar_events",
        description="List upcoming Google Calendar events within a given number of days.",
        pack="google_tools",
        tags=["google", "calendar"],
        parameters={
            "type": "object",
            "properties": {"days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 90}},
            "required": [],
        },
        handler=_list_calendar_events,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=True,
    ))

    registry.register(ToolSpec(
        name="search_gmail",
        description="Search Gmail messages using a Gmail query string.",
        pack="google_tools",
        tags=["google", "gmail"],
        parameters={
            "type": "object",
            "properties": {
                "query":       {"type": "string", "minLength": 1},
                "max_results": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        },
        handler=_search_gmail,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=True,
    ))

    registry.register(ToolSpec(
        name="list_drive_files",
        description="List recently modified Google Drive files.",
        pack="google_tools",
        tags=["google", "drive"],
        parameters={
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            },
            "required": [],
        },
        handler=_list_drive_files,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=True,
    ))

    registry.register(ToolSpec(
        name="search_drive",
        description="Search Google Drive files by name or content.",
        pack="google_tools",
        tags=["google", "drive"],
        parameters={
            "type": "object",
            "properties": {
                "query":       {"type": "string", "minLength": 1},
                "max_results": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        },
        handler=_search_drive,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=True,
    ))
