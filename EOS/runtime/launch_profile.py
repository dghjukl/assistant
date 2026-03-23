from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.windows_deployment import DeploymentAssessment, assess_windows_deployment

ROOT = Path(__file__).resolve().parent.parent

ROLE_TO_SCRIPT = {
    "primary": "main",
    "tool": "tools",
    "thinking": "thinking",
    "creativity": "creativity",
    "vision": "vision",
}

PROFILE_ROLE_ORDER = {
    "minimal": ["primary"],
    "standard": ["primary", "tool", "thinking"],
    "full": ["primary", "tool", "thinking", "creativity"],
    "vision": ["vision"],
}


@dataclass
class LaunchItem:
    role: str
    accel: str
    script_path: Path


class LaunchPlanError(RuntimeError):
    pass


def _choose_accel(role_key: str, assessment: DeploymentAssessment, *, cpu_preferred: bool = False) -> str:
    role = assessment.roles.get(role_key)
    if role is None or not role.launchable:
        raise LaunchPlanError(f"{role_key} is not launchable on this machine")
    if cpu_preferred and "cpu" in role.available_accels:
        return "cpu"
    if role.recommended_accel in role.available_accels:
        return role.recommended_accel
    return role.available_accels[0]


def build_launch_plan(profile: str, assessment: DeploymentAssessment, root: Path | None = None) -> list[LaunchItem]:
    root = (root or ROOT).resolve()
    profile = profile.lower()
    if assessment.blocking_issues and profile != "vision":
        raise LaunchPlanError("Setup is incomplete. Run 'python verify.py' and fix the blocking issues first.")
    if profile not in PROFILE_ROLE_ORDER:
        raise LaunchPlanError(f"Unknown profile: {profile}")

    cpu_preferred = assessment.recommended_profile == "compatibility"
    plan: list[LaunchItem] = []
    for role_key in PROFILE_ROLE_ORDER[profile]:
        accel = _choose_accel(role_key, assessment, cpu_preferred=cpu_preferred)
        script_base = ROLE_TO_SCRIPT[role_key]
        script_path = root / "launchers" / f"start-{script_base}-{accel}.bat"
        if not script_path.is_file():
            raise LaunchPlanError(f"Launcher not found: {script_path}")
        plan.append(LaunchItem(role=role_key, accel=accel, script_path=script_path))
    return plan


def launch_plan(plan: list[LaunchItem], root: Path | None = None) -> None:
    root = (root or ROOT).resolve()
    for item in plan:
        title_role = item.role.title()
        print(f"[EOS] Starting {title_role} on {item.accel.upper()} using {item.script_path.name}")
        if sys.platform == "win32":
            subprocess.Popen(["cmd.exe", "/k", str(item.script_path)], cwd=root)
        else:  # pragma: no cover - for non-Windows dev/test environments
            subprocess.Popen([str(item.script_path)], cwd=root)
        time.sleep(0.4)


def main() -> None:
    parser = argparse.ArgumentParser(description="Start a hardened EOS launch profile.")
    parser.add_argument("profile", choices=tuple(PROFILE_ROLE_ORDER), help="Profile to start")
    parser.add_argument("--root", default=str(ROOT), help="EOS root directory")
    parser.add_argument("--config", default="config.json", help="Config file to inspect")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    assessment = assess_windows_deployment(root, args.config)
    try:
        plan = build_launch_plan(args.profile, assessment, root=root)
    except LaunchPlanError as exc:
        print(f"[EOS] {exc}", file=sys.stderr)
        if assessment.summary:
            for line in assessment.summary:
                print(f"[EOS] {line}", file=sys.stderr)
        for issue in assessment.blocking_issues:
            print(f"[EOS] BLOCKING: {issue}", file=sys.stderr)
        for warning in assessment.warnings:
            print(f"[EOS] NOTE: {warning}", file=sys.stderr)
        sys.exit(1)

    launch_plan(plan, root=root)


if __name__ == "__main__":
    main()
