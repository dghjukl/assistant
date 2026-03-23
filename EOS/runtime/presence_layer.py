"""EOS — Presence Layer
Transforms raw subsystem state into concise, user-facing presence cues.

The presence layer sits between low-level runtime state and anything the user
might actually see or that should shape the assistant's voice.  It does not
own truth; it renders truth into a compact "resident intelligence" register.

Primary uses:
- system-prompt augmentation before each chat turn
- lightweight ambient outputs returned by the WebUI/API
- bounded proactive check-in suggestions driven by initiative + idle state
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

UTC = timezone.utc


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _truncate(text: str, max_chars: int) -> str:
    text = str(text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


@dataclass
class PresenceCue:
    kind: str
    text: str
    source: str = ""
    priority: str = "normal"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PresenceState:
    created_at: str
    focus: dict[str, Any]
    attention: dict[str, Any]
    continuity: dict[str, Any]
    environment: dict[str, Any]
    capabilities: dict[str, Any]
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    recent_interactions: list[dict[str, Any]] = field(default_factory=list)
    initiative: dict[str, Any] = field(default_factory=dict)
    idle: dict[str, Any] = field(default_factory=dict)
    cues: dict[str, PresenceCue] = field(default_factory=dict)
    proactive_checkin: PresenceCue | None = None

    def render_system_prompt_block(self) -> str:
        """Render a compact block for pre-turn system-prompt augmentation."""
        lines = [
            "## Presence Layer",
            "Translate subsystem state into a stable, resident sense of continuity.",
        ]

        doing = self.cues.get("what_ive_been_doing")
        changed = self.cues.get("what_changed_since_last_time")
        watching = self.cues.get("what_im_keeping_an_eye_on")

        if doing and doing.text:
            lines.append(f"- Ongoing stance: {doing.text}")
        if changed and changed.text:
            lines.append(f"- Fresh change: {changed.text}")
        if watching and watching.text:
            lines.append(f"- Watchpoint: {watching.text}")
        if self.attention.get("compact"):
            lines.append(f"- Durable preferences: {self.attention.get('compact')}")

        if self.proactive_checkin and self.proactive_checkin.text:
            lines.append(
                "- If a check-in is appropriate, keep it brief, optional, and bounded: "
                f"{self.proactive_checkin.text}"
            )

        lines.append(
            "- Let these cues shape tone and continuity, but do not dump raw telemetry unless it is relevant."
        )
        return "\n".join(lines)

    def ambient_payload(self) -> dict[str, Any]:
        return {
            "what_ive_been_doing": self.cues.get("what_ive_been_doing").to_dict()
            if self.cues.get("what_ive_been_doing") else None,
            "what_changed_since_last_time": self.cues.get("what_changed_since_last_time").to_dict()
            if self.cues.get("what_changed_since_last_time") else None,
            "what_im_keeping_an_eye_on": self.cues.get("what_im_keeping_an_eye_on").to_dict()
            if self.cues.get("what_im_keeping_an_eye_on") else None,
            "proactive_checkin": self.proactive_checkin.to_dict() if self.proactive_checkin else None,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "focus": self.focus,
            "attention": self.attention,
            "continuity": self.continuity,
            "environment": self.environment,
            "capabilities": self.capabilities,
            "recent_events": list(self.recent_events),
            "recent_interactions": list(self.recent_interactions),
            "initiative": dict(self.initiative),
            "idle": dict(self.idle),
            "ambient": self.ambient_payload(),
        }


def build_presence_state(
    *,
    current_focus: dict[str, Any] | None = None,
    attention_summary: dict[str, Any] | None = None,
    continuity: dict[str, Any] | None = None,
    environment: dict[str, Any] | None = None,
    capabilities: dict[str, Any] | None = None,
    recent_events: list[dict[str, Any]] | None = None,
    recent_interactions: list[dict[str, Any]] | None = None,
    initiative: dict[str, Any] | None = None,
    idle: dict[str, Any] | None = None,
) -> PresenceState:
    """Build a PresenceState from raw subsystem summaries."""
    focus = dict(current_focus or {})
    attention = dict(attention_summary or {})
    continuity = dict(continuity or {})
    environment = dict(environment or {})
    capabilities = dict(capabilities or {})
    recent_events = list(recent_events or [])
    recent_interactions = list(recent_interactions or [])
    initiative = dict(initiative or {})
    idle = dict(idle or {})

    cues = {
        "what_ive_been_doing": _render_doing_cue(focus, initiative, attention),
        "what_changed_since_last_time": _render_change_cue(recent_events, continuity, recent_interactions),
        "what_im_keeping_an_eye_on": _render_watch_cue(focus, attention, environment, capabilities, recent_events, initiative),
    }
    proactive = _render_checkin_cue(focus, attention, initiative, idle, recent_events)

    return PresenceState(
        created_at=_now_iso(),
        focus=focus,
        attention=attention,
        continuity=continuity,
        environment=environment,
        capabilities=capabilities,
        recent_events=recent_events,
        recent_interactions=recent_interactions,
        initiative=initiative,
        idle=idle,
        cues=cues,
        proactive_checkin=proactive,
    )


def _attention_topics(attention: dict[str, Any], key: str, limit: int = 2) -> list[str]:
    items = attention.get(key, [])
    if not isinstance(items, list):
        return []
    topics: list[str] = []
    for item in items[:limit]:
        topic = str((item or {}).get("topic") or "").strip()
        if topic:
            topics.append(topic)
    return topics


def _render_doing_cue(
    focus: dict[str, Any],
    initiative: dict[str, Any],
    attention: dict[str, Any],
) -> PresenceCue:
    title = _truncate(focus.get("title") or "Standing by for the next meaningful task", 120)
    next_action = _truncate(focus.get("next_action") or "wait for the next turn", 120)
    source = str(focus.get("source") or "maintenance")
    queue_depth = int(initiative.get("queue_depth") or 0)
    preferred_projects = _attention_topics(attention, "preferred_projects")

    if source == "user_goal":
        if preferred_projects and title not in preferred_projects:
            text = (
                f"I'm working around the active goal '{title}', while keeping continuity with "
                f"preferred project threads like '{preferred_projects[0]}'; next attention is on {next_action.lower()}."
            )
        else:
            text = f"I'm working around the active goal '{title}', with next attention on {next_action.lower()}."
    elif source == "initiative" and queue_depth > 0:
        text = f"I've been handling a queued initiative around '{title}', and next up is {next_action.lower()}."
    elif source == "investigation":
        text = f"I've been tracking the investigation '{title}', with the next step being {next_action.lower()}."
    elif preferred_projects:
        text = (
            f"I've been keeping steady continuity around preferred projects like '{preferred_projects[0]}', "
            f"and right now I’m oriented around '{title}' with next attention on {next_action.lower()}."
        )
    else:
        text = f"I've been oriented around '{title}', and next I’m set to {next_action.lower()}."

    return PresenceCue(kind="doing", text=text, source=source)


def _render_change_cue(
    recent_events: list[dict[str, Any]],
    continuity: dict[str, Any],
    recent_interactions: list[dict[str, Any]],
) -> PresenceCue:
    if recent_events:
        event = recent_events[0]
        message = _truncate(event.get("message") or "A background update landed.", 160)
        source = str(event.get("source") or "runtime")
        return PresenceCue(
            kind="changed",
            text=f"Since last time, the newest notable change was: {message}",
            source=source,
            priority="high" if event.get("level") in {"warn", "error"} else "normal",
        )

    if continuity.get("has_prior_session"):
        ended = str(continuity.get("session_ended_at") or "a prior session")[:10]
        turns = int(continuity.get("turn_count") or 0)
        return PresenceCue(
            kind="changed",
            text=f"I’m carrying continuity forward from the prior session that ended on {ended} after {turns} turns.",
            source="session_continuity",
        )

    latest_user = ""
    for item in reversed(recent_interactions):
        if item.get("role") == "user":
            latest_user = _truncate(item.get("content") or "", 100)
            break
    if latest_user:
        return PresenceCue(
            kind="changed",
            text=f"The most recent shift in our thread was your request about '{latest_user}'.",
            source="interaction_log",
        )

    return PresenceCue(
        kind="changed",
        text="Nothing significant has shifted in the background since the last exchange.",
        source="runtime",
    )


def _render_watch_cue(
    focus: dict[str, Any],
    attention: dict[str, Any],
    environment: dict[str, Any],
    capabilities: dict[str, Any],
    recent_events: list[dict[str, Any]],
    initiative: dict[str, Any],
) -> PresenceCue:
    env_headline = ""
    summary = environment.get("summary") if isinstance(environment.get("summary"), dict) else None
    if summary:
        env_headline = str(summary.get("headline") or "").strip()
    env_headline = env_headline or str(environment.get("headline") or "").strip()

    status_line = str(capabilities.get("status_line") or capabilities.get("summary") or "").strip()
    queue_depth = int(initiative.get("queue_depth") or 0)
    warned = next((e for e in recent_events if e.get("level") in {"warn", "error"}), None)
    watch_topics = _attention_topics(attention, "monitored_topics", limit=3)
    concern_topics = _attention_topics(attention, "recurring_concerns", limit=2)

    if warned:
        return PresenceCue(
            kind="watching",
            text=f"I’m keeping an eye on {str(warned.get('message') or 'a recent warning').rstrip('.')}.",
            source=str(warned.get("source") or "runtime"),
            priority="high",
        )
    if "degraded" in status_line.lower() or "offline" in status_line.lower():
        return PresenceCue(
            kind="watching",
            text=f"I’m watching capability health because it currently looks {status_line}.",
            source="capabilities",
            priority="high",
        )
    if env_headline and any(token in env_headline.lower() for token in ("degraded", "offline", "limited")):
        return PresenceCue(
            kind="watching",
            text=f"I’m tracking the environment state: {env_headline}.",
            source="environment",
        )
    if watch_topics:
        topic = watch_topics[0]
        tail = f" and recurring concern '{concern_topics[0]}'" if concern_topics else ""
        return PresenceCue(
            kind="watching",
            text=f"I’m keeping durable watch on '{topic}'{tail} across sessions.",
            source="attention_profile",
        )
    if concern_topics:
        return PresenceCue(
            kind="watching",
            text=f"I’m keeping an eye on recurring concern '{concern_topics[0]}' so it stays salient over time.",
            source="attention_profile",
        )
    if queue_depth > 0:
        return PresenceCue(
            kind="watching",
            text=f"I’m keeping an eye on {queue_depth} queued proactive item(s) while staying aligned with the current focus.",
            source="initiative",
        )

    title = _truncate(focus.get("title") or "the current thread", 90)
    return PresenceCue(
        kind="watching",
        text=f"I’m keeping an eye on anything that could affect '{title}'.",
        source=str(focus.get("source") or "focus"),
    )


def _render_checkin_cue(
    focus: dict[str, Any],
    attention: dict[str, Any],
    initiative: dict[str, Any],
    idle: dict[str, Any],
    recent_events: list[dict[str, Any]],
) -> PresenceCue | None:
    initiative_enabled = bool(initiative.get("enabled") or initiative.get("autonomy_gate"))
    idle_tier = str(idle.get("tier") or "active")
    queue_depth = int(initiative.get("queue_depth") or 0)
    watch_event = next((e for e in recent_events if e.get("level") in {"warn", "error"}), None)
    title = _truncate(focus.get("title") or "what’s been happening", 100)
    watch_topics = _attention_topics(attention, "monitored_topics", limit=2)
    preferred_projects = _attention_topics(attention, "preferred_projects", limit=2)

    if watch_event:
        msg = _truncate(watch_event.get("message") or "a recent runtime warning", 120)
        return PresenceCue(
            kind="checkin",
            text=f"If helpful, I can give you a quick check-in about {msg.lower()}.",
            source=str(watch_event.get("source") or "runtime"),
            priority="high",
        )

    if initiative_enabled and queue_depth > 0:
        if watch_topics:
            return PresenceCue(
                kind="checkin",
                text=f"If you want, I can give a short watchlist update on '{watch_topics[0]}' while I handle the queued proactive work.",
                source="attention_profile",
            )
        return PresenceCue(
            kind="checkin",
            text=f"If you want, I can give a short update on the proactive work around '{title}'.",
            source="initiative",
        )

    if initiative_enabled and idle_tier in {"resting", "drifting", "deep"}:
        if preferred_projects:
            return PresenceCue(
                kind="checkin",
                text=f"If you'd like, I can give a brief continuity check-in on '{preferred_projects[0]}' and anything that shifted while things were quiet.",
                source="attention_profile",
            )
        return PresenceCue(
            kind="checkin",
            text=f"If you'd like, I can give a brief resident check-in on '{title}' and anything that shifted while things were quiet.",
            source="idle_cognition",
        )

    return None
