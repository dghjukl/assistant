from __future__ import annotations

from typing import Any

MODEL_BACKEND_ROLES = ("primary", "tool", "thinking", "creativity", "vision")
START_BACKENDS_MESSAGE = "Start backend services before using UI"


def google_unavailable_payload(*, reason: str = "not_configured") -> dict[str, str]:
    return {
        "status": "unavailable",
        "reason": reason,
    }


def detect_startup_guidance(runtime_discovery: Any) -> str | None:
    if runtime_discovery is None:
        return None

    services = getattr(runtime_discovery, "services", {}) or {}
    model_probes = [services.get(role) for role in MODEL_BACKEND_ROLES if services.get(role) is not None]
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
