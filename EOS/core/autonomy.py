"""
EOS — Autonomy Profile
Controls what the entity can perceive, decide, and do.

Four dimensions, each independently togglable.
Defaults: perception=True, cognition=True, action=False, initiative=False.
Action and initiative start locked; the partner unlocks them deliberately.
"""
from __future__ import annotations

from core.memory import get_autonomy, set_autonomy


DIMENSION_DESCRIPTIONS: dict[str, str] = {
    "perception":  "Can access screen, webcam, files, and external data",
    "cognition":   "Can reason, plan, and use the background thinking helper",
    "action":      "Can execute tools, send messages, modify files, use calendar",
    "initiative":  "Can act without being explicitly asked (proactive behaviour)",
}

# Ordered for display
DIMENSION_ORDER = ["perception", "cognition", "action", "initiative"]


def get_profile() -> dict[str, bool]:
    """Return the current autonomy profile."""
    return get_autonomy()


def set_dimension(dimension: str, enabled: bool) -> None:
    """Enable or disable a single autonomy dimension."""
    if dimension not in DIMENSION_DESCRIPTIONS:
        raise ValueError(
            f"Unknown autonomy dimension: {dimension!r}. "
            f"Valid: {list(DIMENSION_DESCRIPTIONS)}"
        )
    set_autonomy(dimension, enabled)


def can(dimension: str) -> bool:
    """Return True if the given dimension is currently enabled."""
    return get_autonomy().get(dimension, False)


def build_autonomy_clause() -> str:
    """
    Return a human-readable constraint clause for injection into the system prompt.
    Lists each dimension with its current state and description.
    """
    profile = get_profile()
    lines = []
    for dim in DIMENSION_ORDER:
        enabled = profile.get(dim, False)
        status  = "ENABLED" if enabled else "DISABLED"
        lines.append(f"  {dim}: {status} — {DIMENSION_DESCRIPTIONS[dim]}")
    return "\n".join(lines)


def get_full_profile() -> dict:
    """Return the full profile with descriptions, for admin API use."""
    profile = get_profile()
    return {
        dim: {
            "enabled":     profile.get(dim, False),
            "description": DIMENSION_DESCRIPTIONS[dim],
        }
        for dim in DIMENSION_ORDER
    }
