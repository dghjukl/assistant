from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class LaunchRole:
    key: str
    label: str
    script_base: str
    port: int
    optional: bool
    aliases: tuple[str, ...] = ()

    @property
    def all_aliases(self) -> tuple[str, ...]:
        return (self.key, *self.aliases)

    def launcher_name(self, accel: str) -> str:
        return f"start-{self.script_base}-{accel}.bat"

    def launcher_path(self, root: Path | None = None, *, accel: str) -> Path:
        return (root or ROOT) / "launchers" / self.launcher_name(accel)


@dataclass(frozen=True)
class LaunchBundle:
    key: str
    label: str
    description: str
    roles: tuple[str, ...]
    launcher: str | None = None
    legacy_tier: str = "first_class"


LAUNCH_ROLES: tuple[LaunchRole, ...] = (
    LaunchRole("primary", "Main", "main", 8080, optional=False, aliases=("main",)),
    LaunchRole("tool", "Tools", "tools", 8082, optional=True, aliases=("tool", "tools")),
    LaunchRole("thinking", "Thinking", "thinking", 8083, optional=True),
    LaunchRole("creativity", "Creativity", "creativity", 8084, optional=True),
    LaunchRole("vision", "Vision", "vision", 8081, optional=True),
)

ROLE_BY_KEY = {role.key: role for role in LAUNCH_ROLES}
ROLE_ALIAS_MAP = {
    alias: role.key
    for role in LAUNCH_ROLES
    for alias in role.all_aliases
}

LAUNCH_BUNDLES: tuple[LaunchBundle, ...] = (
    LaunchBundle(
        key="minimal",
        label="Minimal",
        description="Main model only, using the safest supported accelerator.",
        roles=("primary",),
        launcher="start-minimal.bat",
    ),
    LaunchBundle(
        key="standard",
        label="Standard",
        description="Resident baseline stack only: main model plus baseline helpers. Auxiliary reasoning servers stay elastic/on-demand.",
        roles=("primary", "tool", "vision"),
        launcher="start-standard.bat",
    ),
    LaunchBundle(
        key="full",
        label="Full",
        description="Resident baseline stack with optional vision preloaded when supported. Auxiliary cognition remains policy-driven and on-demand.",
        roles=("primary", "tool", "vision"),
        launcher="start-full.bat",
    ),
    LaunchBundle(
        key="vision",
        label="Vision",
        description="Dedicated vision helper; additive to the normal backend bundle.",
        roles=("vision",),
        launcher="start-vision-gpu.bat",
        legacy_tier="advanced_only",
    ),
)

BUNDLE_BY_KEY = {bundle.key: bundle for bundle in LAUNCH_BUNDLES}
BUNDLE_KEYS = tuple(bundle.key for bundle in LAUNCH_BUNDLES)


def normalize_role_name(role: str) -> str:
    key = role.strip().lower()
    if key not in ROLE_ALIAS_MAP:
        raise KeyError(role)
    return ROLE_ALIAS_MAP[key]


def role_for(role: str) -> LaunchRole:
    return ROLE_BY_KEY[normalize_role_name(role)]


def role_label(role: str) -> str:
    return role_for(role).label


def service_label(role: str) -> str:
    role_meta = role_for(role)
    if role_meta.key == "primary":
        return "Main model"
    if role_meta.key in {"tool", "thinking", "creativity"}:
        return f"{role_meta.label} helper"
    return role_meta.label


def bundle_for(bundle: str) -> LaunchBundle:
    key = bundle.strip().lower()
    if key not in BUNDLE_BY_KEY:
        raise KeyError(bundle)
    return BUNDLE_BY_KEY[key]


def export_catalog() -> dict[str, object]:
    return {
        "roles": [
            {
                **asdict(role),
                "aliases": list(role.aliases),
            }
            for role in LAUNCH_ROLES
        ],
        "bundles": [asdict(bundle) for bundle in LAUNCH_BUNDLES],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Emit the canonical EOS launch catalog.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()

    catalog = export_catalog()
    if args.json:
        print(json.dumps(catalog, indent=2))
        return

    print("Roles:")
    for role in catalog["roles"]:
        aliases = ", ".join(role["aliases"]) if role["aliases"] else "—"
        print(f"- {role['key']}: {role['label']} (aliases: {aliases})")
    print("\nBundles:")
    for bundle in catalog["bundles"]:
        roles = ", ".join(bundle["roles"])
        print(f"- {bundle['key']}: {roles}")


if __name__ == "__main__":
    main()
