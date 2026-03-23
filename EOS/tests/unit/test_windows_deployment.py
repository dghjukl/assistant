from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.launch_profile import LaunchPlanError, build_launch_plan
from runtime.windows_deployment import assess_windows_deployment


def _write_file(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_config(tmp_path: Path) -> Path:
    config = {
        "deployment_mode": "standard",
        "primary": {"is_multimodal": False},
        "servers": {
            "primary": {
                "enabled": True,
                "required": True,
                "host": "127.0.0.1",
                "port": 8080,
                "model_path": "models/primary",
                "binary_cpu": "llama-CPU/llama-server.exe",
                "binary_gpu": "llama-gpu/llama-server.exe",
            },
            "tool": {
                "enabled": True,
                "required": False,
                "host": "127.0.0.1",
                "port": 8082,
                "model_path": "models/tool",
                "binary_cpu": "llama-CPU/llama-server.exe",
                "binary_gpu": "llama-gpu/llama-server.exe",
            },
            "thinking": {
                "enabled": True,
                "required": False,
                "host": "127.0.0.1",
                "port": 8083,
                "model_path": "models/thinking",
                "binary_cpu": "llama-CPU/llama-server.exe",
                "binary_gpu": "llama-gpu/llama-server.exe",
            },
            "creativity": {
                "enabled": True,
                "required": False,
                "host": "127.0.0.1",
                "port": 8084,
                "model_path": "models/creativity",
                "binary_cpu": "llama-CPU/llama-server.exe",
                "binary_gpu": "llama-gpu/llama-server.exe",
            },
            "vision": {
                "enabled": True,
                "required": False,
                "host": "127.0.0.1",
                "port": 8081,
                "model_path": "models/vision",
                "mmproj_path": "models/vision",
                "binary_gpu": "llama-gpu/llama-server.exe",
            },
        },
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


@pytest.fixture
def deployment_root(tmp_path: Path) -> Path:
    _write_file(tmp_path / "llama-CPU" / "llama-server.exe")
    _write_file(tmp_path / "llama-gpu" / "llama-server.exe")
    _write_file(tmp_path / "models" / "primary" / "Qwen3-8B-Q6_K.gguf")
    _write_file(tmp_path / "models" / "tool" / "lfm2-tool.gguf")
    _write_file(tmp_path / "models" / "thinking" / "lfm2-thinking.gguf")
    _write_file(tmp_path / "models" / "creativity" / "creative.gguf")
    _write_file(tmp_path / "models" / "vision" / "vision.gguf")
    _write_file(tmp_path / "models" / "vision" / "mmproj-vision.gguf")
    for name in [
        "start-main-cpu.bat",
        "start-main-gpu.bat",
        "start-tools-cpu.bat",
        "start-tools-gpu.bat",
        "start-thinking-cpu.bat",
        "start-thinking-gpu.bat",
        "start-creativity-cpu.bat",
        "start-creativity-gpu.bat",
        "start-vision-gpu.bat",
    ]:
        _write_file(tmp_path / "launchers" / name, "@echo off\n")
    _write_config(tmp_path)
    return tmp_path


def test_assessment_prefers_compatibility_profile_without_gpu(monkeypatch, deployment_root: Path):
    monkeypatch.setattr(
        "runtime.windows_deployment.detect_nvidia_gpu",
        lambda: (False, None, "nvidia-smi not found"),
    )
    monkeypatch.setattr("runtime.windows_deployment.detect_total_memory_gb", lambda: 12.0)

    assessment = assess_windows_deployment(deployment_root, deployment_root / "config.json")

    assert assessment.recommended_profile == "compatibility"
    assert assessment.setup_complete is True
    assert assessment.roles["primary"].recommended_accel == "cpu"
    assert any("CPU-first deployment tier" in line for line in assessment.summary)
    assert any(profile.key == "recommended" and profile.supported for profile in assessment.profiles)


def test_assessment_prefers_recommended_profile_with_gpu(monkeypatch, deployment_root: Path):
    monkeypatch.setattr(
        "runtime.windows_deployment.detect_nvidia_gpu",
        lambda: (True, "RTX 4070", None),
    )
    monkeypatch.setattr("runtime.windows_deployment.detect_total_memory_gb", lambda: 32.0)

    assessment = assess_windows_deployment(deployment_root, deployment_root / "config.json")

    assert assessment.recommended_profile == "recommended"
    assert assessment.roles["vision"].recommended_accel == "gpu"
    assert assessment.roles["primary"].recommended_accel == "gpu"


def test_build_launch_plan_uses_cpu_fallback_when_machine_is_cpu_tier(monkeypatch, deployment_root: Path):
    monkeypatch.setattr(
        "runtime.windows_deployment.detect_nvidia_gpu",
        lambda: (False, None, "nvidia-smi not found"),
    )
    monkeypatch.setattr("runtime.windows_deployment.detect_total_memory_gb", lambda: 12.0)
    assessment = assess_windows_deployment(deployment_root, deployment_root / "config.json")

    plan = build_launch_plan("standard", assessment, root=deployment_root)

    assert [item.role for item in plan] == ["primary", "tool", "thinking"]
    assert all(item.accel == "cpu" for item in plan)


def test_build_launch_plan_rejects_incomplete_setup(monkeypatch, deployment_root: Path):
    monkeypatch.setattr(
        "runtime.windows_deployment.detect_nvidia_gpu",
        lambda: (False, None, "nvidia-smi not found"),
    )
    (deployment_root / "models" / "primary" / "Qwen3-8B-Q6_K.gguf").unlink()
    assessment = assess_windows_deployment(deployment_root, deployment_root / "config.json")

    with pytest.raises(LaunchPlanError, match="Setup is incomplete"):
        build_launch_plan("minimal", assessment, root=deployment_root)
