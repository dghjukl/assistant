"""
EOS — Post-Install Verification Script.

Usage:
    python verify.py
    python verify.py --json
"""
from __future__ import annotations

import argparse
import importlib
import json
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.resolve()

REQUIRED_PACKAGES = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("httpx", "httpx"),
    ("chromadb", "chromadb"),
    ("sentence_transformers", "sentence_transformers"),
    ("sounddevice", "sounddevice"),
    ("numpy", "numpy"),
    ("mss", "mss"),
    ("PIL", "Pillow"),
    ("cv2", "opencv-python-headless"),
    ("multipart", "python-multipart"),
    ("websockets", "websockets"),
]
OPTIONAL_PACKAGES = [
    ("faster_whisper", "faster-whisper"),
    ("discord", "discord.py"),
    ("googleapiclient", "google-api-python-client"),
    ("google.auth", "google-auth"),
]


@dataclass
class VerifyReport:
    required_failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    def add_required(self, ok: bool, label: str) -> None:
        if not ok:
            self.required_failures.append(label)

    @property
    def ok(self) -> bool:
        return not self.required_failures

    def exit_code(self) -> int:
        return 0 if self.ok else 1


def _probe_import(import_name: str) -> dict[str, str]:
    try:
        importlib.import_module(import_name)
        return {"status": "ok", "error_type": "", "error_message": ""}
    except ModuleNotFoundError as exc:
        return {"status": "missing", "error_type": type(exc).__name__, "error_message": str(exc)}
    except Exception as exc:  # catches OSError and runtime/native load failures
        return {"status": "broken", "error_type": type(exc).__name__, "error_message": str(exc)}


def _check_python(report: VerifyReport) -> None:
    major, minor = sys.version_info[:2]
    ok = major == 3 and minor >= 10
    report.add_required(ok, f"Python {major}.{minor} (need 3.10+)")
    report.checks["python"] = {
        "required": True,
        "status": "ok" if ok else "broken",
        "version": sys.version.split()[0],
    }


def _check_packages(report: VerifyReport) -> None:
    deps: dict[str, Any] = {}
    for import_name, pip_name in REQUIRED_PACKAGES:
        res = _probe_import(import_name)
        deps[pip_name] = {"required": True, **res}
        report.add_required(res["status"] == "ok", f"package:{pip_name}")
    for import_name, pip_name in OPTIONAL_PACKAGES:
        res = _probe_import(import_name)
        deps[pip_name] = {"required": False, **res}
        if res["status"] != "ok":
            report.warnings.append(f"package:{pip_name}")
    report.checks["packages"] = deps


def _check_binaries(report: VerifyReport) -> None:
    cpu_bin = ROOT / "llama-CPU" / "llama-server.exe"
    gpu_bin = ROOT / "llama-b8149-bin-win-cuda-13.1-x64" / "llama-server.exe"
    piper_bin = ROOT / "Piper" / "piper" / "piper.exe"

    binaries = {
        "llama_cpu": {"path": str(cpu_bin), "required": True, "exists": cpu_bin.is_file()},
        "llama_gpu": {"path": str(gpu_bin), "required": False, "exists": gpu_bin.is_file()},
        "piper": {"path": str(piper_bin), "required": False, "exists": piper_bin.is_file()},
    }
    report.add_required(cpu_bin.is_file(), "binary:llama_cpu")

    if cpu_bin.is_file():
        try:
            proc = subprocess.run([str(cpu_bin), "--version"], capture_output=True, text=True, timeout=10)
            binaries["llama_cpu"]["exec_status"] = "ok" if proc.returncode == 0 else "broken"
            binaries["llama_cpu"]["returncode"] = proc.returncode
            if proc.returncode != 0:
                report.warnings.append("binary:llama_cpu_exec")
        except Exception as exc:
            binaries["llama_cpu"]["exec_status"] = "broken"
            binaries["llama_cpu"]["error_type"] = type(exc).__name__
            binaries["llama_cpu"]["error_message"] = str(exc)
            report.warnings.append("binary:llama_cpu_exec")

    report.checks["binaries"] = binaries


def _check_ports(report: VerifyReport) -> None:
    ports = {8080: "primary", 8081: "vision", 8082: "tool", 8083: "thinking", 8084: "creativity", 7860: "webui"}
    out: dict[str, Any] = {}
    for port, label in ports.items():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        in_use = sock.connect_ex(("127.0.0.1", port)) == 0
        sock.close()
        out[str(port)] = {"label": label, "status": "in_use" if in_use else "free"}
        if in_use:
            report.warnings.append(f"port:{port}")
    report.checks["ports"] = out


def _check_config(report: VerifyReport) -> None:
    path = ROOT / "config.json"
    state: dict[str, Any] = {"path": str(path), "required": True}
    if not path.is_file():
        state.update({"status": "missing", "error_type": "FileNotFoundError", "error_message": "config.json not found"})
        report.add_required(False, "config:config.json")
        report.checks["config"] = state
        return

    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        state.update({"status": "broken", "error_type": type(exc).__name__, "error_message": str(exc)})
        report.add_required(False, "config:config.json")
        report.checks["config"] = state
        return

    state["status"] = "ok"
    state["deployment_mode"] = cfg.get("deployment_mode", "standard")
    report.checks["config"] = state


def _check_windows_deployment(report: VerifyReport) -> None:
    try:
        from runtime.windows_deployment import assess_windows_deployment

        deployment = assess_windows_deployment(ROOT, ROOT / "config.json")
        report.checks["windows_deployment"] = {
            "status": "ok",
            "recommended_profile": deployment.recommended_profile,
            "blocking_issues": list(deployment.blocking_issues),
            "warnings": list(deployment.warnings),
        }
        for issue in deployment.blocking_issues:
            report.add_required(False, f"windows:{issue}")
        report.warnings.extend(f"windows:{w}" for w in deployment.warnings)
    except Exception as exc:
        report.checks["windows_deployment"] = {
            "status": "broken",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }
        report.warnings.append("windows_deployment:probe_failed")


def run_verification() -> VerifyReport:
    report = VerifyReport()
    _check_python(report)
    _check_packages(report)
    _check_binaries(report)
    _check_ports(report)
    _check_config(report)
    _check_windows_deployment(report)
    return report


def _print_human_summary(report: VerifyReport) -> None:
    print("EOS verification summary")
    print("=" * 60)
    print(f"Required failures: {len(report.required_failures)}")
    print(f"Warnings: {len(report.warnings)}")
    print()

    if report.required_failures:
        print("Required checks that failed:")
        for item in report.required_failures:
            print(f"  - {item}")
    else:
        print("All required checks passed.")

    if report.warnings:
        print("\nAdvisories:")
        for item in report.warnings:
            print(f"  - {item}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EOS post-install verification")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON report")
    args = parser.parse_args(argv)

    report = run_verification()
    _print_human_summary(report)
    if args.json:
        print(json.dumps({
            "ok": report.ok,
            "required_failures": report.required_failures,
            "warnings": report.warnings,
            "checks": report.checks,
        }, indent=2))
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
