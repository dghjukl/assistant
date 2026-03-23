from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.release_gate


def test_google_toolpack_stub_returns_structured_unavailable_error(registry):
    from runtime.toolpacks import google_tools

    google_tools.register(registry, {"google": {"enabled": False}})

    spec = next(spec for spec in registry.all_tools() if spec.name == "list_calendar_events")
    payload = json.loads(spec.handler({}))

    assert payload == {
        "status": "unavailable",
        "reason": "not_configured",
    }
