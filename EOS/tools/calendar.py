"""Tool: Google Calendar — read and create events via Google Calendar API."""
from __future__ import annotations

from datetime import datetime, timedelta

_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _get_service(cfg: dict):
    """Return an authenticated Google Calendar v3 service client.

    Uses core.google_oauth for credential management so that the same
    token file is shared across all Google tools and the web OAuth flow
    initiated via /api/google_workspace/authorize works correctly.
    """
    from core.google_oauth import configure as oauth_configure, build_service
    oauth_configure(cfg)
    return build_service("calendar", "v3", scopes=_CALENDAR_SCOPES)


async def list_events(days_ahead: int = 7, cfg: dict = {}) -> str:
    if not cfg.get("google", {}).get("calendar_enabled", False):
        return "Calendar unavailable: Google calendar integration is disabled."
    try:
        svc = _get_service(cfg)
    except Exception as exc:
        return f"Calendar unavailable: {exc}"

    now = datetime.utcnow().isoformat() + "Z"
    end = (datetime.utcnow() + timedelta(days=days_ahead)).isoformat() + "Z"
    try:
        res = svc.events().list(
            calendarId="primary", timeMin=now, timeMax=end,
            maxResults=10, singleEvents=True, orderBy="startTime",
        ).execute()
    except Exception as exc:
        return f"Calendar error listing events: {exc}"

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
    if not cfg.get("google", {}).get("calendar_enabled", False):
        return "Calendar unavailable: Google calendar integration is disabled."
    try:
        svc = _get_service(cfg)
    except Exception as exc:
        return f"Calendar unavailable: {exc}"

    try:
        start_dt = datetime.fromisoformat(start_iso)
    except ValueError as exc:
        return f"Calendar error: invalid start_iso format '{start_iso}': {exc}"
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    event  = {
        "summary":     title,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "UTC"},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "UTC"},
    }
    try:
        created = svc.events().insert(calendarId="primary", body=event).execute()
    except Exception as exc:
        return f"Calendar error creating event: {exc}"
    return f"Event created: {created.get('summary')} at {start_iso}"
