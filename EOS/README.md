# EOS — Entity Operating System

EOS is the **platform**. The running intelligence inside EOS is the **entity**. That entity can have its own chosen name and identity, but that chosen name is **not** the product name.

EOS is a local runtime for a persistent AI entity: memory, identity continuity, tools, optional voice, optional integrations, and a browser-based control surface — all running on your machine.

---

## Canonical explanation

Use this framing everywhere:

- **EOS** = the product, platform, installer, launcher set, and WebUI
- **Entity** = the runtime intelligence / active instance operating inside EOS
- **Entity name** = the name that instance chooses or is configured to use, not the name of the product

That distinction matters because users interact with the entity, but they install, launch, configure, and evaluate EOS.

---

## Canonical default path

For a first-time user, the intended path is:

1. **Install** with `setup\Setup-Full.ps1`
2. **Verify** with `python verify.py`
3. **Launch the recommended backend bundle** with `launchers\start-standard.bat`
4. **Bootstrap EOS** with `start-eos.bat`
5. **Use the WebUI** at `http://127.0.0.1:7860/`

### When to deviate from the default

Deviate only when you specifically need one of these:

- **Lower download size / manual model choice** → use `setup\Setup-Lite.ps1`
- **Lowest runtime footprint (main model only)** → use `launchers\start-minimal.bat`
- **Balanced default (main + tools + vision baseline, elastic auxiliary cognition)** → use `launchers\start-standard.bat`
- **Preload full resident stack (including thinking + creativity)** → use `launchers\start-full.bat`
- **Vision support** → keep `vision` baseline-resident when installed, or add `launchers\start-vision-gpu.bat`
- **Precise hardware or backend control** → use the per-server launchers
- **Diagnostics without startup** → use `status-eos.bat` or `python eos.py --status`

If you are not sure, do **not** deviate. Use the default path above.

---

## What EOS does

EOS runs one or more GGUF language models through llama.cpp and presents them as one coherent local system. It provides:

- a persistent entity with memory and identity continuity
- a web-based workspace and admin surface
- optional speech-to-text and text-to-speech
- optional Discord integration and a first-class credential-gated Google Workspace subsystem
- optional computer use and workspace tooling
- graceful fallback when optional helper backends are unavailable

EOS is **Windows-only** in its supported install flow because the bundled llama-server and Piper TTS binaries are Windows executables.

---

## Entry-point hierarchy

There are multiple valid scripts in the repo, but they are not equal.

### 1) Recommended install entry point

- `setup\Setup-Full.ps1`

This is the canonical install path for most users.

### 2) Recommended backend launch entry point

- `launchers\start-standard.bat`

This is the canonical backend bundle for most users.

### 3) Recommended runtime launch entry point

- `start-eos.bat`

This is the canonical runtime bootstrap. It never starts model servers; it discovers what is already running, builds a capability map, and launches the WebUI.

### 4) Alternative and advanced entry points

- `Launch EOS.bat` / `launchers\Launch EOS.bat` → convenience GUI for choosing backend roles before bootstrapping EOS
- `launchers\start-minimal.bat` / `launchers\start-full.bat` → resource or capability tradeoffs
- `launchers\start-*-cpu.bat` / `launchers\start-*-gpu.bat` → manual per-backend control
- `python eos.py` → direct CLI bootstrap for advanced users
- `status-eos.bat` / `python eos.py --status` → diagnostics only

Legacy launchers under `launchers\legacy\` are now compatibility shims only. They remain callable, but new launch behavior should be added through `runtime/launch_catalog.py`, the first-class `start-*.bat` scripts, and the launcher UI.

---

## Startup architecture

EOS uses a **single canonical config**: `config.json`.

That file describes the full intended system — main, tool, thinking, creativity, vision, STT, and TTS. Resident baseline services and elastic auxiliary services are configured explicitly through `server_activation`. Startup behavior is modular:

- **Per-server launchers** start exactly one backend each
- **Bundle launchers** start resident baseline combinations only; auxiliary reasoning servers stay policy-driven and on-demand by default
- **`start-eos.bat` / `python eos.py`** discover running services and start the WebUI
- **`status-eos.bat` / `python eos.py --status`** report current capability state without starting the WebUI

This keeps backend ownership separate from runtime discovery.

---

## System requirements

| Component | Minimum | Recommended |
|---|---|---|
| OS | Windows 10 64-bit | Windows 11 64-bit |
| Python | 3.10+ | 3.11 |
| RAM | 16 GB | 32 GB |
| GPU | None (CPU-only works) | NVIDIA with 8 GB+ VRAM |
| Disk | 20 GB free | 30 GB free |
| CUDA | Not required | CUDA 13.1 compatible driver |

---

## Where to go next

| Document | What it covers |
|---|---|
| [docs/QUICK_START.md](docs/QUICK_START.md) | Canonical first-run path |
| [docs/INSTALL.md](docs/INSTALL.md) | Canonical install path, alternatives, troubleshooting |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | Day-to-day operation and capabilities |
| [docs/POWER_USER_GUIDE.md](docs/POWER_USER_GUIDE.md) | Advanced launchers, CLI, config, diagnostics |
| [docs/PROFILES.md](docs/PROFILES.md) | Bundle and per-server launcher reference |
| [docs/CREDENTIALS.md](docs/CREDENTIALS.md) | Discord and Google credential setup |

---

## Version

`1.0.0` — see [VERSION](VERSION)
