from __future__ import annotations

import webui.app_runtime as app_runtime
from runtime.idle_cognition import IdleCognitionEngine
from webui.app_state import app_state


class _TurnSpy:
    def __init__(self):
        self.calls = 0

    def notify_turn(self) -> None:
        self.calls += 1


class _LegacyIdleCognition:
    def __init__(self):
        self.calls = 0

    def notify_interaction(self) -> None:
        self.calls += 1


def test_notify_interaction_updates_app_and_idle_cognition_timestamps():
    app_state.idle_cognition = IdleCognitionEngine({})

    interaction_ts = app_runtime._notify_interaction()

    assert app_state.last_interaction_monotonic == interaction_ts
    assert app_state.idle_cognition._last_interaction_monotonic == interaction_ts


def test_notify_interaction_supports_legacy_idle_cognition_signature():
    app_state.idle_cognition = _LegacyIdleCognition()

    interaction_ts = app_runtime._notify_interaction()

    assert interaction_ts == app_state.last_interaction_monotonic
    assert app_state.idle_cognition.calls == 1


def test_discord_turn_notifier_updates_interaction_and_turn_engines():
    app_state.idle_cognition = IdleCognitionEngine({})
    app_state.reflection_pipeline = _TurnSpy()
    app_state.initiative_engine = _TurnSpy()

    notifier = app_runtime._build_discord_turn_notifier()
    notifier()

    assert app_state.last_interaction_monotonic == app_state.idle_cognition._last_interaction_monotonic
    assert app_state.reflection_pipeline.calls == 1
    assert app_state.initiative_engine.calls == 1
