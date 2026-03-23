from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from runtime.overnight_declaration import OvernightDeclarationExtractor
from runtime.overnight_store import ACTIVE_STATUSES, OvernightCycleStore

DAY_ACTIVE = "DAY_ACTIVE"
EARLY_NIGHT = "EARLY_NIGHT"
DEEP_NIGHT = "DEEP_NIGHT"
PREWAKE = "PREWAKE"


@dataclass
class OvernightCycleConfig:
    enabled: bool = True
    conversation_declare_enabled: bool = True
    early_phase_hours: float = 2.0
    deep_phase_start_hours: float = 2.0
    prewake_lead_hours: float = 1.5
    allow_investigations_overnight: bool = True
    allow_memory_maintenance_overnight: bool = True
    allow_initiative_overnight: bool = True
    use_declared_window_as_bias: bool = True
    cancel_on_live_return: bool = True
    live_override_grace_minutes: float = 20.0
    default_soon_minutes: int = 30
    default_morning_return_hour: float = 8.0

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any] | None) -> "OvernightCycleConfig":
        section = (cfg or {}).get("overnight_cycle", {})
        return cls(
            enabled=bool(section.get("enabled", True)),
            conversation_declare_enabled=bool(section.get("conversation_declare_enabled", True)),
            early_phase_hours=float(section.get("early_phase_hours", 2.0)),
            deep_phase_start_hours=float(section.get("deep_phase_start_hours", 2.0)),
            prewake_lead_hours=float(section.get("prewake_lead_hours", 1.5)),
            allow_investigations_overnight=bool(section.get("allow_investigations_overnight", True)),
            allow_memory_maintenance_overnight=bool(section.get("allow_memory_maintenance_overnight", True)),
            allow_initiative_overnight=bool(section.get("allow_initiative_overnight", True)),
            use_declared_window_as_bias=bool(section.get("use_declared_window_as_bias", True)),
            cancel_on_live_return=bool(section.get("cancel_on_live_return", True)),
            live_override_grace_minutes=float(section.get("live_override_grace_minutes", 20.0)),
            default_soon_minutes=int(section.get("default_soon_minutes", 30)),
            default_morning_return_hour=float(section.get("default_morning_return_hour", 8.0)),
        )


def _ensure_dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def current_phase(
    now: datetime,
    last_interaction: datetime | None,
    active_window: dict[str, Any] | None,
    config: OvernightCycleConfig,
) -> str:
    if not config.enabled or not active_window:
        return DAY_ACTIVE

    away_start = _ensure_dt(active_window.get("away_start_time"))
    expected_return = _ensure_dt(active_window.get("expected_return_time"))
    if away_start is None or expected_return is None:
        return DAY_ACTIVE

    if now < away_start:
        return DAY_ACTIVE

    if last_interaction is not None:
        live_grace = timedelta(minutes=config.live_override_grace_minutes)
        if last_interaction >= away_start and (now - last_interaction) <= live_grace:
            return DAY_ACTIVE

    if now >= expected_return - timedelta(hours=config.prewake_lead_hours):
        return PREWAKE

    inactivity = (now - last_interaction) if last_interaction is not None else (now - away_start)
    away_elapsed = now - away_start
    if away_elapsed >= timedelta(hours=config.deep_phase_start_hours) and inactivity >= timedelta(hours=config.deep_phase_start_hours):
        return DEEP_NIGHT

    if inactivity >= timedelta(minutes=config.live_override_grace_minutes):
        return EARLY_NIGHT

    return DAY_ACTIVE


class OvernightCycleService:
    """Authoritative runtime coordinator for conversational overnight cycles."""

    def __init__(self, cfg: dict[str, Any], store: OvernightCycleStore) -> None:
        self._cfg = cfg
        self._config = OvernightCycleConfig.from_cfg(cfg)
        self._store = store
        self._extractor = OvernightDeclarationExtractor(cfg)
        self._last_interaction_at: datetime | None = None
        self._last_status: dict[str, Any] | None = None

    def note_interaction(self, *, now: datetime | None = None) -> None:
        self._last_interaction_at = _ensure_dt(now or datetime.now(timezone.utc))

    def handle_user_turn(
        self,
        text: str,
        *,
        now: datetime | None = None,
        topology=None,
    ) -> dict[str, Any]:
        now_dt = _ensure_dt(now or datetime.now(timezone.utc))
        self.note_interaction(now=now_dt)
        current = self._store.fetch_current()
        extracted = self._extractor.extract(text, now=now_dt, topology=topology, cfg=self._cfg)

        if extracted.is_declaration and extracted.away_start_time and extracted.expected_return_time:
            record = self._store.create_declaration(
                away_start_time=extracted.away_start_time,
                expected_return_time=extracted.expected_return_time,
                confidence=extracted.confidence,
                source="conversation",
                source_text=extracted.source_text,
                declared_at=_iso(now_dt),
                is_one_off=extracted.is_one_off,
                parser_details=extracted.notes or {},
            )
            status = self.get_status(now=now_dt)
            return {
                "is_declaration": True,
                "record": record,
                "acknowledgment": extracted.acknowledgment,
                "extraction": extracted.to_dict(),
                "status": status,
            }

        return_info: dict[str, Any] | None = None
        if current is not None and self._config.cancel_on_live_return:
            away_start = _ensure_dt(current.get("away_start_time"))
            if away_start is not None and now_dt >= away_start:
                return_info = self._store.mark_return(current["id"], actual_return_time=_iso(now_dt))

        status = self.get_status(now=now_dt)
        return {
            "is_declaration": False,
            "record": return_info,
            "live_return_detected": return_info is not None,
            "status": status,
        }

    def cancel_current(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        cancelled = self._store.cancel_current(cancelled_at=_iso(_ensure_dt(now or datetime.now(timezone.utc))))
        self._last_status = None
        return cancelled

    def update_expected_return_time(self, *, expected_return_time: str, now: datetime | None = None) -> dict[str, Any] | None:
        current = self._store.fetch_current()
        if current is None:
            return None
        return self._store.update_expected_return_time(
            current["id"],
            expected_return_time,
            updated_at=_iso(_ensure_dt(now or datetime.now(timezone.utc))),
        )

    def current_phase(self, *, now: datetime | None = None) -> str:
        now_dt = _ensure_dt(now or datetime.now(timezone.utc))
        status = self.get_status(now=now_dt)
        return str(status.get("phase") or DAY_ACTIVE)

    def get_status(self, *, now: datetime | None = None, include_history: bool = False) -> dict[str, Any]:
        now_dt = _ensure_dt(now or datetime.now(timezone.utc))
        current = self._store.fetch_current()
        phase = current_phase(now_dt, self._last_interaction_at, current, self._config)
        live_activity_override = False

        if current is not None:
            away_start = _ensure_dt(current.get("away_start_time"))
            if away_start is not None and self._last_interaction_at is not None:
                live_activity_override = self._last_interaction_at >= away_start and phase == DAY_ACTIVE
            mapped_status = self._mapped_status_for_phase(now_dt, current, phase)
            if current.get("status") != mapped_status:
                current = self._store.update_status(current["id"], mapped_status) or current
        else:
            mapped_status = "none"

        posture = self._build_posture(phase, current)
        payload = {
            "enabled": self._config.enabled,
            "phase": phase,
            "status": mapped_status if current is not None else "none",
            "current_window": current,
            "last_interaction_at": _iso(self._last_interaction_at),
            "live_activity_override": live_activity_override,
            "config": {
                "early_phase_hours": self._config.early_phase_hours,
                "deep_phase_start_hours": self._config.deep_phase_start_hours,
                "prewake_lead_hours": self._config.prewake_lead_hours,
                "allow_investigations_overnight": self._config.allow_investigations_overnight,
                "allow_memory_maintenance_overnight": self._config.allow_memory_maintenance_overnight,
                "allow_initiative_overnight": self._config.allow_initiative_overnight,
                "cancel_on_live_return": self._config.cancel_on_live_return,
                "live_override_grace_minutes": self._config.live_override_grace_minutes,
            },
            "posture": posture,
        }
        if include_history:
            payload["recent_history"] = self._store.recent_history(limit=10)
        self._last_status = payload
        return payload

    def recent_history(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._store.recent_history(limit=limit)

    def status_summary(self, *, now: datetime | None = None) -> dict[str, Any]:
        status = self.get_status(now=now)
        window = status.get("current_window") or {}
        return {
            "enabled": status.get("enabled"),
            "phase": status.get("phase"),
            "status": status.get("status"),
            "planned_away_start": window.get("away_start_time"),
            "expected_return": window.get("expected_return_time"),
            "actual_return_time": window.get("actual_return_time"),
            "confidence": window.get("confidence"),
            "source": window.get("source"),
            "live_activity_override": status.get("live_activity_override"),
            "posture": status.get("posture"),
        }

    def _mapped_status_for_phase(self, now: datetime, current: dict[str, Any], phase: str) -> str:
        away_start = _ensure_dt(current.get("away_start_time"))
        if away_start is None:
            return str(current.get("status") or "scheduled")
        if now < away_start:
            return "scheduled"
        if phase == PREWAKE:
            return "prewake"
        if phase in {EARLY_NIGHT, DEEP_NIGHT, DAY_ACTIVE}:
            return "active"
        return "scheduled"

    def _build_posture(self, phase: str, current: dict[str, Any] | None) -> dict[str, Any]:
        overnight_active = current is not None and str(current.get("status") or "") in ACTIVE_STATUSES
        allow_investigations = True
        allow_memory = True
        allow_initiative = True
        idle_style = "day"
        prefer_synthesis = False
        heavy_new_work = False

        if overnight_active:
            if phase == EARLY_NIGHT:
                idle_style = "light_reflective"
                heavy_new_work = True
                allow_investigations = False
                allow_memory = False
                allow_initiative = False
            elif phase == DEEP_NIGHT:
                idle_style = "deep_overnight"
                allow_investigations = self._config.allow_investigations_overnight
                allow_memory = self._config.allow_memory_maintenance_overnight
                allow_initiative = self._config.allow_initiative_overnight
            elif phase == PREWAKE:
                idle_style = "prewake_synthesis"
                prefer_synthesis = True
                heavy_new_work = True
                allow_investigations = False
                allow_memory = False
                allow_initiative = False

        return {
            "overnight_active": overnight_active,
            "allow_investigations": allow_investigations,
            "allow_memory_maintenance": allow_memory,
            "allow_initiative": allow_initiative,
            "idle_cognition_style": idle_style,
            "prefer_synthesis": prefer_synthesis,
            "suppress_heavy_new_work": heavy_new_work,
        }
