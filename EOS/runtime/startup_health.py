from __future__ import annotations

from typing import Any

START_BACKENDS_MESSAGE = "Start baseline backend services before using UI"


def _baseline_backend_roles(runtime_discovery: Any) -> tuple[str, ...]:
    cfg = getattr(runtime_discovery, "config", {}) or {}
    activation = cfg.get("server_activation", {}) or {}
    servers = cfg.get("servers", {}) or {}
    baseline_default = [r for r in ("primary", "tool", "vision") if servers.get(r, {}).get("enabled", False)]
    baseline = activation.get("baseline_roles") or baseline_default or ["primary"]
    return tuple(str(role) for role in baseline)


def google_unavailable_payload(*, reason: str = "not_configured") -> dict[str, str]:
    return {
        "status": "unavailable",
        "reason": reason,
    }


def detect_startup_guidance(runtime_discovery: Any) -> str | None:
    if runtime_discovery is None:
        return None

    services = getattr(runtime_discovery, "services", {}) or {}
    model_probes = [services.get(role) for role in _baseline_backend_roles(runtime_discovery) if services.get(role) is not None]
    if not model_probes:
        return START_BACKENDS_MESSAGE

    if all(getattr(probe, "status", "unavailable") == "unavailable" for probe in model_probes):
        return START_BACKENDS_MESSAGE
    return None


def issue_record(category: str, component: str, reason: str, *, detail: Any = None) -> dict[str, Any]:
    return {
        "category": category,
        "component": component,
        "reason": reason,
        "detail": detail,
    }
