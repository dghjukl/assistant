"""
EOS — Post-Install Verification Script
=======================================
Run after Setup-Full.ps1 (or Setup-Lite.ps1) to confirm EOS is ready to launch.

Usage:
    python verify.py

Exit codes:
    0 — all required checks passed (warnings may still be printed)
    1 — one or more required checks failed
"""
from __future__ import annotations

import importlib
import json
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()

# ── Formatting helpers ────────────────────────────────────────────────────────

CYAN   = "\033[36m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def ok(msg: str)   -> None: print(f"  {GREEN}[OK]     {RESET}{msg}")
def warn(msg: str) -> None: print(f"  {YELLOW}[WARN]   {RESET}{msg}")
def fail(msg: str) -> None: print(f"  {RED}[FAIL]   {RESET}{msg}")
def info(msg: str) -> None: print(f"  {CYAN}         {RESET}{msg}")
def section(title: str) -> None:
    print(f"\n{BOLD}{CYAN}── {title} {'─' * max(0, 52 - len(title))}{RESET}")


_failures: list[str] = []
_warnings: list[str] = []

def require(passed: bool, label: str, fix: str = "") -> None:
    if passed:
        ok(label)
    else:
        fail(label)
        if fix:
            info(f"Fix: {fix}")
        _failures.append(label)

def advisory(passed: bool, label: str, fix: str = "") -> None:
    if passed:
        ok(label)
    else:
        warn(label)
        if fix:
            info(f"Fix: {fix}")
        _warnings.append(label)


# ── 1. Python version ─────────────────────────────────────────────────────────

section("Python")
major, minor = sys.version_info[:2]
require(
    major == 3 and minor >= 10,
    f"Python {major}.{minor} (need 3.10+)",
    "Install Python 3.11 from https://www.python.org/downloads/"
)


# ── 2. Required Python packages ───────────────────────────────────────────────

section("Python Packages")

REQUIRED_PACKAGES = [
    ("fastapi",              "fastapi"),
    ("uvicorn",              "uvicorn"),
    ("httpx",                "httpx"),
    ("chromadb",             "chromadb"),
    ("sentence_transformers","sentence_transformers"),
    ("sounddevice",          "sounddevice"),
    ("numpy",                "numpy"),
    ("mss",                  "mss"),
    ("PIL",                  "Pillow"),
    ("cv2",                  "opencv-python-headless"),
    ("multipart",            "python-multipart"),
    ("websockets",           "websockets"),
]

OPTIONAL_PACKAGES = [
    ("faster_whisper",       "faster-whisper"),
    ("discord",              "discord.py"),
    ("googleapiclient",      "google-api-python-client"),
    ("google.auth",          "google-auth"),
]

for import_name, pip_name in REQUIRED_PACKAGES:
    try:
        importlib.import_module(import_name)
        ok(pip_name)
    except ImportError:
        fail(f"{pip_name} — not importable")
        info(f"Fix: pip install {pip_name}")
        _failures.append(f"package:{pip_name}")

for import_name, pip_name in OPTIONAL_PACKAGES:
    try:
        importlib.import_module(import_name)
        ok(f"{pip_name} (optional)")
    except ImportError:
        warn(f"{pip_name} (optional) — not installed")
        info(f"Fix: pip install {pip_name}")
        _warnings.append(f"package:{pip_name}")


# ── 3. Binaries ───────────────────────────────────────────────────────────────

section("Binaries")

cpu_bin  = ROOT / "llama-CPU" / "llama-server.exe"
gpu_bin  = ROOT / "llama-b8149-bin-win-cuda-13.1-x64" / "llama-server.exe"
piper_bin = ROOT / "Piper" / "piper" / "piper.exe"

require(
    cpu_bin.is_file(),
    f"llama-server (CPU) at {cpu_bin.relative_to(ROOT)}",
    "Re-run Setup-Full.ps1 to download llama.cpp"
)

advisory(
    gpu_bin.is_file(),
    f"llama-server (GPU/CUDA) at {gpu_bin.relative_to(ROOT)}",
    "Re-run Setup-Full.ps1 with an NVIDIA GPU present, or use the CPU launchers"
)

advisory(
    piper_bin.is_file(),
    f"Piper TTS at {piper_bin.relative_to(ROOT)}",
    "Re-run Setup-Full.ps1 to download Piper"
)

# Test that the CPU binary actually executes
if cpu_bin.is_file():
    try:
        result = subprocess.run(
            [str(cpu_bin), "--version"],
            capture_output=True, timeout=10
        )
        ok("llama-server (CPU) executes without error")
    except Exception as exc:
        warn(f"llama-server (CPU) exists but could not execute: {exc}")
        info("Windows Defender may be blocking it — check Protection History in Windows Security")
        _warnings.append("llama-server CPU execution")


# ── 4. Model directories ──────────────────────────────────────────────────────

section("Model Files")

def has_gguf(directory: Path, exclude_prefix: str = "") -> bool:
    if not directory.is_dir():
        return False
    return any(
        f.suffix == ".gguf" and not f.name.startswith(exclude_prefix)
        for f in directory.iterdir()
    )

def has_mmproj(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    return any(f.name.startswith("mmproj") and f.suffix == ".gguf"
               for f in directory.iterdir())

primary_dir  = ROOT / "models" / "primary"
tool_dir     = ROOT / "models" / "tool"
thinking_dir = ROOT / "models" / "thinking"
creativity_dir = ROOT / "models" / "creativity"
vision_dir   = ROOT / "models" / "vision"
stt_file     = ROOT / "models" / "stt" / "ggml-small.en-q8_0.bin"
tts_file     = ROOT / "models" / "tts" / "en_US-amy-medium.onnx"
tts_cfg      = ROOT / "models" / "tts" / "en_US-amy-medium.onnx.json"

require(
    has_gguf(primary_dir),
    "Primary model in models/primary/",
    "Re-run Setup-Full.ps1 or place a primary GGUF in models/primary/"
)

advisory(
    has_gguf(tool_dir),
    "Tool model in models/tool/",
    "Re-run Setup-Full.ps1 (tool model is optional but recommended)"
)

advisory(
    has_gguf(thinking_dir),
    "Thinking model in models/thinking/",
    "Re-run Setup-Full.ps1 (thinking model is optional)"
)

advisory(
    has_gguf(creativity_dir),
    "Creativity model in models/creativity/",
    "Place any instruct GGUF in models/creativity/ to enable this subsystem"
)

vision_main   = has_gguf(vision_dir, exclude_prefix="mmproj")
vision_mmproj = has_mmproj(vision_dir)
advisory(
    vision_main and vision_mmproj,
    "Vision model + mmproj in models/vision/ (vision mode only)",
    "Re-run Setup-Full.ps1 to download both vision files"
)
if vision_dir.is_dir():
    if vision_main and not vision_mmproj:
        warn("Vision main model found but mmproj is missing — vision mode will fail")
        info("Re-run Setup-Full.ps1 to download mmproj-Qwen2.5-VL-3B-Instruct-f16.gguf")
        _warnings.append("vision mmproj missing")
    elif vision_mmproj and not vision_main:
        warn("Vision mmproj found but main model is missing — vision mode will fail")
        info("Re-run Setup-Full.ps1 to download Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf")
        _warnings.append("vision main model missing")

advisory(
    stt_file.is_file(),
    "STT model at models/stt/ggml-small.en-q8_0.bin",
    "Re-run Setup-Full.ps1 (voice input optional)"
)

advisory(
    tts_file.is_file() and tts_cfg.is_file(),
    "TTS model at models/tts/en_US-amy-medium.onnx + .json",
    "Re-run Setup-Full.ps1 (voice output optional)"
)


# ── 5. Config files ───────────────────────────────────────────────────────────

section("Config Files")

EXPECTED_CONFIGS = [
    "config.json",
]

for cfg_name in EXPECTED_CONFIGS:
    cfg_path = ROOT / cfg_name
    if not cfg_path.is_file():
        advisory(False, f"{cfg_name} present", f"Config file missing: {cfg_name}")
        continue
    try:
        with cfg_path.open() as f:
            json.load(f)
        ok(f"{cfg_name} — valid JSON")
    except json.JSONDecodeError as exc:
        fail(f"{cfg_name} — invalid JSON: {exc}")
        _failures.append(f"config:{cfg_name}")


# ── 6. Credential files ───────────────────────────────────────────────────────

section("Credential Files (presence only — content not checked)")

discord_file = ROOT / "AI personal files" / "Discord.txt"
google_glob  = list((ROOT / "AI personal files").glob("client_secret_*.json")) \
               if (ROOT / "AI personal files").is_dir() else []

advisory(
    discord_file.is_file(),
    "Discord credential: AI personal files/Discord.txt",
    "See CREDENTIALS.md — required only if Discord is enabled in config"
)

advisory(
    len(google_glob) > 0,
    "Google credential: AI personal files/client_secret_*.json",
    "See CREDENTIALS.md — required only if Google is enabled in config"
)


# ── 7. Port availability ──────────────────────────────────────────────────────

section("Port Availability")

PORTS = {
    8080: "primary model server",
    8082: "tool model server",
    8083: "thinking model server",
    8084: "creativity model server",
    7860: "WebUI",
}

VISION_PORTS = {8081: "vision model server"}

for port, label in {**PORTS, **VISION_PORTS}.items():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    in_use = sock.connect_ex(("127.0.0.1", port)) == 0
    sock.close()
    advisory(
        not in_use,
        f"Port {port} ({label}) — {'IN USE — may conflict' if in_use else 'free'}",
        f"Close the application using port {port} before launching EOS" if in_use else ""
    )


# ── 8. Data directories ───────────────────────────────────────────────────────

section("Data Directories")

DATA_DIRS = [
    ROOT / "data",
    ROOT / "data" / "memory_store",
    ROOT / "data" / "computer_use",
    ROOT / "data" / "worldview",
]

for d in DATA_DIRS:
    advisory(
        d.is_dir(),
        f"{d.relative_to(ROOT)}/",
        f"Re-run Setup-Full.ps1 to create directory structure"
    )


# ── Summary ───────────────────────────────────────────────────────────────────

print(f"\n{'=' * 56}")

if _failures:
    print(f"\n{RED}{BOLD}  FAILED — {len(_failures)} required check(s) did not pass:{RESET}")
    for f in _failures:
        print(f"  {RED}• {f}{RESET}")
    print()
    if _warnings:
        print(f"{YELLOW}  {len(_warnings)} advisory warning(s) also present.{RESET}")
    print(f"\n  {RED}EOS may not start correctly. Fix the issues above and re-run verify.py.{RESET}\n")
    sys.exit(1)
else:
    print(f"\n{GREEN}{BOLD}  All required checks passed.{RESET}")
    if _warnings:
        print(f"{YELLOW}  {len(_warnings)} advisory warning(s) — optional features may be limited.{RESET}")
        for w in _warnings:
            print(f"  {YELLOW}• {w}{RESET}")
    print(f"\n  {GREEN}EOS is ready. Start the desired backend launchers, then run start-eos.bat.{RESET}\n")
    sys.exit(0)
