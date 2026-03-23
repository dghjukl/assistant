# EOS — Entity Operating System

A local AI companion and non-human partner. Runs entirely on your machine — no cloud API required. Built around a persistent identity, long-term memory, and a trust-first autonomy model whose controls exist for safety, containment, and revocation when needed.

---

## What it is

EOS is a local AI system that runs one or more GGUF language models via llama.cpp. It provides a web-based chat interface, optional voice input and output, optional Discord and Google Workspace integration, a sandboxed computer-use subsystem, and a worldview subsystem for accelerated relational onboarding.

The system maintains identity and memory across sessions. It does not reset between conversations. Your conversation history, memory, and the entity's accumulated understanding of you all persist on your local machine — nothing leaves it.

Autonomy is layered and trust-first. EOS is meant to have meaningful presence, judgment, and capability in the normal case; the admin panel exists so capabilities can be narrowed, supervised, or revoked immediately if safety requires it, without restarting.

EOS is **Windows-only**. The llama-server and Piper TTS binaries are Windows executables.

---

## Startup architecture

EOS now uses a **single canonical config**: `config.json`.

That file describes the full intended system — main, tool, thinking, creativity, vision, STT, and TTS. Startup behavior is modular:

- **Per-server launchers** start exactly one backend each (`start-main-gpu.bat`, `start-tools-cpu.bat`, etc.)
- **Bundle launchers** start common backend combinations (`start-minimal.bat`, `start-standard.bat`, `start-full.bat`)
- **`start-eos.bat` / `python eos.py`** never start model servers; they discover what is already running, build a capability map, and launch the WebUI
- **`status-eos.bat` / `python eos.py --status`** reports the same capability map without restarting anything

This keeps server ownership separate from runtime discovery and fallback behavior.

---

## How it works at a high level

1. Start whichever backend servers you want with the per-role or bundle launchers.
2. Start EOS with `start-eos.bat`.
3. EOS probes each expected service from `config.json`, performs health checks, and builds a runtime capability map.
4. Missing helpers degrade gracefully at runtime:
   - missing tool / thinking / creativity helpers fall back to the main model
   - missing vision disables vision cleanly
   - missing STT or TTS degrades voice features without blocking chat

The WebUI remains the relationship and safety control point at `http://127.0.0.1:7860/`, with the admin panel at `http://127.0.0.1:7860/admin`.

---

## System requirements

| Component | Minimum | Recommended |
|---|---|---|
| OS | Windows 10 64-bit | Windows 11 64-bit |
| Python | 3.10 | 3.11 |
| RAM | 16 GB | 32 GB |
| GPU | None (CPU-only works) | NVIDIA with 8 GB+ VRAM |
| Disk | 20 GB free | 30 GB free |
| CUDA | Not required | CUDA 13.1 compatible driver |

---

## Common launch flows

| Goal | Launchers |
|---|---|
| Minimal chat + tools | `start-minimal.bat` then `start-eos.bat` |
| Recommended standard stack | `start-standard.bat` then `start-eos.bat` |
| Full stack | `start-full.bat` then `start-eos.bat` |
| Inspect current system state | `status-eos.bat` |
| Single backend only | Use the matching `start-*-cpu.bat` or `start-*-gpu.bat` |

Legacy `Start *.bat` wrappers still exist, but they now delegate to the modular launchers above.

---

## Install overview

1. Install Python 3.11 from https://www.python.org/downloads/ — check "Add Python to PATH"
2. Right-click `Setup-Full.ps1` → "Run with PowerShell" — downloads ~13 GB of models and binaries
3. Run `python verify.py` to confirm everything is in place
4. Start the desired backends
5. Run `start-eos.bat`
6. Open `http://127.0.0.1:7860/` in your browser

Full install instructions: [INSTALL.md](INSTALL.md)

Quick reference for first-time setup: [QUICK_START.md](QUICK_START.md)

---

## Where to go next

| Document | What it covers |
|---|---|
| [QUICK_START.md](QUICK_START.md) | Shortest path to a running system, with a checklist of optional capabilities |
| [INSTALL.md](INSTALL.md) | Full install instructions, alternatives, troubleshooting |
| [USER_GUIDE.md](USER_GUIDE.md) | Every capability explained: voice, Discord, Google, computer use, workspace, worldview, cognition panel |
| [POWER_USER_GUIDE.md](POWER_USER_GUIDE.md) | Canonical config deep-dive, autonomy system, launcher architecture, diagnostics |
| [MODELS.md](MODELS.md) | Model directory layout, filenames, sources, and swap instructions |
| [CREDENTIALS.md](CREDENTIALS.md) | Step-by-step Discord bot and Google OAuth setup with LLM prompt suggestions |
| [PROFILES.md](PROFILES.md) | Startup bundle overview and per-server launcher reference |

---

## Version

`1.0.0` — see [VERSION](VERSION)
