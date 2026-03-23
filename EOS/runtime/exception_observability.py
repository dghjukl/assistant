"""
EOS runtime exception observability helpers.

Policy
------
Exceptions may be intentionally ignored only when all of the following are true:
  1. The operation is optional or best-effort cleanup.
  2. Failure cannot change user-visible state or corrupt durable state.
  3. There is no practical operator action to take.

Otherwise failures should be surfaced with at least one of:
  - debug   : degraded observability or optional enrichment failed
  - warning : a feature degraded, retried, or partially completed
  - error   : primary work failed, state may be incomplete, or operator action is needed
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any


def observe_exception(
    *,
    logger: logging.Logger,
    subsystem: str,
    operation: str,
    exc: Exception,
    level: int = logging.WARNING,
    context: dict[str, Any] | None = None,
    diagnostics: deque | None = None,
    capability_name: str | None = None,
    capability_status: str | None = None,
) -> dict[str, Any]:
    """Record an exception in logs, the admin log ring, and optional diagnostics."""
    event = {
        "timestamp": time.time(),
        "subsystem": subsystem,
        "operation": operation,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "context": dict(context or {}),
        "level": logging.getLevelName(level).lower(),
    }

    logger.log(
        level,
        "[%s] %s failed: %s | context=%s",
        subsystem,
        operation,
        exc,
        event["context"],
    )

    if diagnostics is not None:
        diagnostics.append(event)

    try:
        from webui.app_state import app_state

        app_state.log_ring.append({
            "timestamp": event["timestamp"],
            "level": event["level"],
            "source": subsystem,
            "message": f"{operation} failed: {event['error_type']}: {event['error']}",
            "detail": event["context"],
        })

        registry = getattr(app_state, "capability_registry", None)
        if registry is not None and capability_name and capability_status:
            registry.set_status(
                capability_name,
                capability_status,
                f"{operation} failed: {event['error_type']}: {event['error']}",
            )
    except Exception:
        # Never let observability helpers cause secondary failures.
        pass

    return event
