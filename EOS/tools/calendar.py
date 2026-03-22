"""Tool: Google Calendar — read and create events via Google Calendar API."""
from __future__ import annotations

import glob
from datetime import datetime, timedelta
from pathlib import Path

_service = None
_cfg_ref: dict = {}


def _get_service(cfg: dict):
    global _service, _cfg_ref
    if _service is not None and cfg is _cfg_ref:
        return _service

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        raise ImportError(
            "Run: pip install google-auth google-auth-oauthlib google-api-python-client"
        )

    gcfg       = cfg.get("google", {})
    token_path = Path(gcfg.get("token_path", "data/google_token.json"))
    creds_glob = gcfg.get("client_secret_glob", "AI personal files/client_secret_*.json")
    creds_file = next(iter(glob.glob(creds_glob)), None)

    SCOPES = ["https://www.googleapis.com/auth/calendar"]
    creds  = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_file:
                raise FileNotFoundError("Google OAuth2 credentials file not found.")
            flow  = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())

    _service = build("calendar", "v3", credentials=creds)
    _cfg_ref = cfg
    return _service


async def list_events(days_ahead: int = 7, cfg: dict = {}) -> str:
    try:
        svc = _get_service(cfg)
    except Exception as exc:
        return f"Calendar unavailable: {exc}"

    now = datetime.utcnow().isoformat() + "Z"
    end = (datetime.utcnow() + timedelta(days=days_ahead)).isoformat() + "Z"
    res = svc.events().list(
        calendarId="primary", timeMin=now, timeMax=end,
        maxResults=10, singleEvents=True, orderBy="startTime",
    ).execute()

    events = res.get("items", [])
    if not events:
        return f"No events in the next {days_ahead} days."
    lines = [f"Upcoming events (next {days_ahead} days):"]
    for e in events:
        start = e["start"].get("dateTime", e["start"].get("date", ""))
        lines.append(f"  • {start[:16]} — {e.get('summary', '(no title)')}")
    return "\n".join(lines)


async def create_event(
    title: str,
    start_iso: str,
    duration_minutes: int = 60,
    description: str = "",
    cfg: dict = {},
) -> str:
    try:
        svc = _get_service(cfg)
    except Exception as exc:
        return f"Calendar unavailable: {exc}"

    start_dt = datetime.fromisoformat(start_iso)
    end_dt   = start_dt + timedelta(minutes=duration_minutes)
    event    = {
        "summary":     title,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "UTC"},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "UTC"},
    }
    created = svc.events().insert(calendarId="primary", body=event).execute()
    return f"Event created: {created.get('summary')} at {start_iso}"
