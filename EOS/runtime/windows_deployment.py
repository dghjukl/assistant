from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from typing import Any, Callable

from runtime.boot import _resolve_mmproj_path, _resolve_model_path, load_config
from runtime.launch_catalog import LAUNCH_ROLES, ROLE_BY_KEY, bundle_for


@dataclass
class HardwareAssessment:
    has_nvidia_gpu: bool
    gpu_name: str | None = None
    total_memory_gb: float | None = None
    cpu_only_reason: str | None = None


@dataclass
class RoleAssessment:
    role: str
    label: str
    enabled: bool
    port: int
    recommended_accel: str
    available_accels: list[str] = field(default_factory=list)
    launchable: bool = False
    selected_model: str | None = None
    selected_mmproj: str | None = None
    issues: list[str] = field(default_factory=list)


@dataclass
class LaunchProfile:
    key: str
    label: str
    description: str
    supported: bool
    tier: str
    selections: dict[str, str]
    issues: list[str] = field(default_factory=list)


@dataclass
class DeploymentAssessment:
    config_path: str
    hardware: HardwareAssessment
    roles: dict[str, RoleAssessment]
    profiles: list[LaunchProfile]
    recommended_profile: str
    setup_complete: bool
    role_catalog: list[dict[str, Any]] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)
    blocking_issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_path": self.config_path,
            "hardware": asdict(self.hardware),
            "roles": {key: asdict(value) for key, value in self.roles.items()},
            "profiles": [asdict(profile) for profile in self.profiles],
            "recommended_profile": self.recommended_profile,
            "setup_complete": self.setup_complete,
            "role_catalog": list(self.role_catalog),
            "summary": list(self.summary),
            "blocking_issues": list(self.blocking_issues),
            "warnings": list(self.warnings),
        }


ROLE_ORDER = [role.key for role in LAUNCH_ROLES]


def detect_nvidia_gpu(run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> tuple[bool, str | None, str | None]:
    try:
        result = run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        return False, None, "nvidia-smi not found"
    except Exception as exc:  # pragma: no cover - defensive
        return False, None, str(exc)

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "nvidia-smi failed").strip()
        return False, None, detail

    for line in result.stdout.splitlines():
        name = line.strip()
        if name:
            return True, name, None
    return False, None, "nvidia-smi returned no GPU names"


def detect_total_memory_gb() -> float | None:
    if sys.platform == "win32":
        try:
            import ctypes

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return round(status.ullTotalPhys / (1024**3), 1)
        except Exception:
            return None
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return round((pages * page_size) / (1024**3), 1)
    except (AttributeError, ValueError, OSError):
        return None


def _role_binary_path(root: Path, srv_cfg: dict[str, Any], accel: str) -> Path | None:
    value = srv_cfg.get(f"binary_{accel}")
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else root / path


def _assess_role(role: str, srv_cfg: dict[str, Any], root: Path, hardware: HardwareAssessment) -> RoleAssessment:
    label = ROLE_BY_KEY.get(role).label if role in ROLE_BY_KEY else role.title()
    enabled = bool(srv_cfg.get("enabled", False))
    port = int(srv_cfg.get("port", 0))
    assessment = RoleAssessment(
        role=role,
        label=label,
        enabled=enabled,
        port=port,
        recommended_accel="off",
    )

    if not enabled:
        assessment.issues.append("disabled in config.json")
        return assessment

    model = _resolve_model_path(srv_cfg.get("model_path", ""), root)
    if model is None:
        assessment.issues.append(f"missing model in {srv_cfg.get('model_path', '')}")
    else:
        assessment.selected_model = str(model.relative_to(root))

    mmproj_cfg = srv_cfg.get("mmproj_path")
    if mmproj_cfg:
        mmproj = _resolve_mmproj_path(mmproj_cfg, root)
        if mmproj is None:
            assessment.issues.append(f"missing mmproj in {mmproj_cfg}")
        else:
            assessment.selected_mmproj = str(mmproj.relative_to(root))

    cpu_binary = _role_binary_path(root, srv_cfg, "cpu")
    gpu_binary = _role_binary_path(root, srv_cfg, "gpu")

    if cpu_binary and cpu_binary.is_file():
        assessment.available_accels.append("cpu")
    elif cpu_binary:
        assessment.issues.append(f"missing CPU runtime at {cpu_binary.relative_to(root)}")

    if gpu_binary and hardware.has_nvidia_gpu and gpu_binary.is_file():
        assessment.available_accels.append("gpu")
    elif gpu_binary and not hardware.has_nvidia_gpu:
        assessment.issues.append("GPU runtime present but no NVIDIA GPU detected")
    elif gpu_binary:
        assessment.issues.append(f"missing GPU runtime at {gpu_binary.relative_to(root)}")

    assessment.available_accels = sorted(set(assessment.available_accels), key=("cpu", "gpu").index)
    if assessment.selected_model and assessment.available_accels:
        assessment.launchable = True
        assessment.recommended_accel = "gpu" if "gpu" in assessment.available_accels else assessment.available_accels[0]
    return assessment


def _profile(label: str, key: str, description: str, tier: str, selections: dict[str, str], roles: dict[str, RoleAssessment]) -> LaunchProfile:
    issues: list[str] = []
    for role, accel in selections.items():
        role_state = roles[role]
        if accel == "off":
            continue
        if accel not in role_state.available_accels:
            if not role_state.enabled:
                issues.append(f"{role_state.label} disabled in config")
            elif role_state.selected_model is None:
                issues.append(f"{role_state.label} model missing")
            else:
                issues.append(f"{role_state.label} cannot run on {accel.upper()}")
    return LaunchProfile(
        key=key,
        label=label,
        description=description,
        supported=len(issues) == 0,
        tier=tier,
        selections=selections,
        issues=issues,
    )


def assess_windows_deployment(root: Path | str, config_path: Path | str = "config.json") -> DeploymentAssessment:
    root = Path(root).resolve()
    config_path = Path(config_path)
    config_path = config_path if config_path.is_absolute() else root / config_path
    cfg = load_config(config_path)

    has_gpu, gpu_name, cpu_reason = detect_nvidia_gpu()
    hardware = HardwareAssessment(
        has_nvidia_gpu=has_gpu,
        gpu_name=gpu_name,
        total_memory_gb=detect_total_memory_gb(),
        cpu_only_reason=cpu_reason if not has_gpu else None,
    )

    roles: dict[str, RoleAssessment] = {}
    for role in ROLE_ORDER:
        srv_cfg = cfg.get("servers", {}).get(role)
        if srv_cfg:
            roles[role] = _assess_role(role, srv_cfg, root, hardware)

    blocking: list[str] = []
    warnings: list[str] = []
    summary: list[str] = []

    primary = roles.get("primary")
    if primary is None or not primary.launchable:
        blocking.append("Main model is not launchable yet. Add a primary GGUF and ensure at least one llama-server runtime is installed.")
    if not hardware.has_nvidia_gpu:
        warnings.append("No NVIDIA GPU detected. EOS should use CPU-only or mixed CPU profiles on this machine.")
    if hardware.total_memory_gb is not None and hardware.total_memory_gb < 16:
        warnings.append(f"System memory is about {hardware.total_memory_gb:.1f} GB. Prefer lighter CPU-safe profiles if the main model feels unstable.")

    for role_key in ["tool", "thinking", "creativity", "vision"]:
        role = roles.get(role_key)
        if role and role.enabled and not role.launchable:
            warnings.append(f"{role.label} is unavailable: {'; '.join(role.issues)}")

    recommended_selections = {
        "primary": primary.recommended_accel if primary and primary.launchable else "off",
        "tool": roles["tool"].recommended_accel if roles.get("tool") and roles["tool"].launchable else "off",
        "thinking": roles["thinking"].recommended_accel if roles.get("thinking") and roles["thinking"].launchable else "off",
        "creativity": "off",
        "vision": "off",
    }
    if hardware.has_nvidia_gpu and roles.get("vision") and roles["vision"].launchable:
        summary.append("Vision support is installed and can be added when needed.")
    if roles.get("creativity") and roles["creativity"].launchable:
        summary.append("Creativity is installed but kept off by default for launch stability.")

    compatibility_selections = {
        "primary": "cpu" if primary and "cpu" in primary.available_accels else recommended_selections["primary"],
        "tool": "cpu" if roles.get("tool") and "cpu" in roles["tool"].available_accels else "off",
        "thinking": "cpu" if roles.get("thinking") and "cpu" in roles["thinking"].available_accels else "off",
        "creativity": "off",
        "vision": "off",
    }
    full_selections = dict(recommended_selections)
    if roles.get("creativity") and roles["creativity"].launchable:
        full_selections["creativity"] = roles["creativity"].recommended_accel
    if roles.get("vision") and roles["vision"].launchable and hardware.has_nvidia_gpu:
        full_selections["vision"] = roles["vision"].recommended_accel

    profiles = [
        _profile(
            "Recommended",
            "recommended",
            "Best default for this machine. Starts the main runtime plus any helpers that are installed and sensible.",
            "tier-2",
            recommended_selections,
            roles,
        ),
        _profile(
            "Compatibility",
            "compatibility",
            "Most resilient fallback. Prefers CPU where possible and keeps optional helpers off.",
            "tier-1",
            compatibility_selections,
            roles,
        ),
        _profile(
            bundle_for("full").label + " installed stack",
            "full",
            "Starts every installed helper your machine appears able to support.",
            "tier-3",
            full_selections,
            roles,
        ),
    ]


    if blocking:
        recommended_profile = "compatibility"
        summary.append("Setup is incomplete. Use the compatibility path only after the blocking items are resolved.")
    elif not hardware.has_nvidia_gpu:
        recommended_profile = "compatibility"
        summary.append("This machine is best treated as a supported CPU-first deployment tier.")
    else:
        recommended_profile = "recommended"
        summary.append("This machine can use the recommended mixed-acceleration launch path.")

    installed = [role.label for role in roles.values() if role.launchable]
    if installed:
        summary.append("Launchable backends: " + ", ".join(installed) + ".")

    return DeploymentAssessment(
        config_path=str(config_path),
        hardware=hardware,
        roles=roles,
        profiles=profiles,
        recommended_profile=recommended_profile,
        setup_complete=len(blocking) == 0,
        role_catalog=[
            {
                "key": role.key,
                "label": role.label,
                "script_base": role.script_base,
                "port": role.port,
                "optional": role.optional,
                "aliases": list(role.aliases),
            }
            for role in LAUNCH_ROLES
        ],
        summary=summary,
        blocking_issues=blocking,
        warnings=warnings,
    )


def _format_report(assessment: DeploymentAssessment) -> str:
    lines = []
    hw = assessment.hardware
    if hw.has_nvidia_gpu:
        lines.append(f"Hardware: NVIDIA GPU detected ({hw.gpu_name})")
    else:
        lines.append("Hardware: no NVIDIA GPU detected; CPU-safe launch path recommended")
    if hw.total_memory_gb is not None:
        lines.append(f"Memory: ~{hw.total_memory_gb:.1f} GB system RAM")

    lines.append(f"Recommended profile: {assessment.recommended_profile}")
    for line in assessment.summary:
        lines.append(f"- {line}")
    if assessment.blocking_issues:
        lines.append("Blocking issues:")
        for issue in assessment.blocking_issues:
            lines.append(f"  * {issue}")
    if assessment.warnings:
        lines.append("Warnings:")
        for warning in assessment.warnings:
            lines.append(f"  * {warning}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Assess EOS Windows deployment readiness.")
    parser.add_argument("--config", default="config.json", help="Config file to inspect (default: config.json)")
    parser.add_argument("--root", default=".", help="Project root directory (default: current directory)")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()

    assessment = assess_windows_deployment(args.root, args.config)
    if args.json:
        print(json.dumps(assessment.to_dict(), indent=2))
    else:
        print(_format_report(assessment))


if __name__ == "__main__":
    main()
