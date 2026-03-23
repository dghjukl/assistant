# EOS — Installation Guide

This guide defines the canonical install entry point and when to use alternatives.

## Canonical install path

For most users, the intended install path is:

1. Install Python 3.11
2. Run `setup\Setup-Full.ps1`
3. Run `python verify.py`
4. Run `python verify.py` again if you changed models manually
5. Run `launchers\Launch EOS.bat` to let the Windows launcher auto-detect the safest profile for this machine
6. Or run `launchers\start-standard.bat` for a non-interactive hardened default
7. Run `start-eos.bat` if you launched backends manually

Use a different path only if you need lighter downloads, manual model control, or custom backend behavior.

---

## Product naming and runtime naming

EOS is the platform. The running intelligence inside EOS is the entity. The entity may use a chosen name, but that name is not the product name and should not replace EOS in docs, install flow, launcher labels, or packaging.

---

## Step 1 — Install Python

Install Python 3.11 or newer from https://www.python.org/downloads/ and enable **Add Python to PATH**.

---

## Step 2 — Run setup

### Recommended

- `setup\Setup-Full.ps1` — downloads the full recommended model and binary set

### Alternate

- `setup\Setup-Lite.ps1` — downloads supporting assets but expects you to provide large models yourself

Choose `Setup-Lite` only if you explicitly want manual model control or need a smaller initial download.

---

## Step 3 — Verify the installation

Run:

```
python verify.py
```

This checks Python packages, binaries, model files, port availability, and the canonical `config.json`.

---

## Step 4 — Launch EOS

### Recommended launch path

1. Start `launchers\Launch EOS.bat`.
   - It inspects installed models, available runtimes, NVIDIA GPU presence, and supported degraded modes.
   - It pre-selects a constrained set of sane launch choices instead of expecting you to know which combination is safe.
2. If you prefer no UI, start the hardened backend bundle:
   - `launchers\start-standard.bat`
3. Start the runtime bootstrap only when you launched backends manually:
   - `start-eos.bat`

### When to deviate from the recommended launch path

- `launchers\start-minimal.bat` — use when you need a lower-resource stack
- `launchers\start-full.bat` — use when you want creativity support in the default bundle
- `launchers\start-vision-gpu.bat` — add vision to any normal stack
- per-server launchers — use when you need exact hardware or backend control
- `python eos.py` — use for direct CLI control

### Per-server launchers

You can also launch one backend at a time:

- `launchers\start-main-cpu.bat`
- `launchers\start-main-gpu.bat`
- `launchers\start-tools-cpu.bat`
- `launchers\start-tools-gpu.bat`
- `launchers\start-thinking-cpu.bat`
- `launchers\start-thinking-gpu.bat`
- `launchers\start-creativity-cpu.bat`
- `launchers\start-creativity-gpu.bat`
- `launchers\start-vision-gpu.bat`

`start-eos.bat` and `python eos.py` never start model servers. They only discover running services and assemble runtime capabilities.

Open **http://127.0.0.1:7860/** when the WebUI is up.

---

## Step 5 — Optional capabilities

After the core system is running, set up any optional capabilities you want:

| Capability | What to do |
|---|---|
| Discord bot | Create a bot token and put it in `AI personal files\Discord.txt` |
| Google Calendar / Gmail / Drive | Download an OAuth JSON and put it in `config\google\` or set an explicit `google.client_secret_path` |
| Computer Use | Enable in admin panel → Control & Permissions → Capabilities |
| Vision | Run `launchers\start-vision-gpu.bat` alongside your normal backend bundle |

Full instructions for Discord and Google: **[CREDENTIALS.md](CREDENTIALS.md)**

Full instructions for all capabilities: **[USER_GUIDE.md](USER_GUIDE.md)**

---

## Troubleshooting

**"python is not recognized"**  
Python is not on your PATH. Re-install Python and enable the PATH option.

**A launcher window closes immediately**  
Run `python verify.py` first, then use `launchers\Launch EOS.bat` so EOS can explain the missing model, runtime, or unsupported profile before launch.

**A backend is missing from EOS startup summary**  
Run `status-eos.bat` to see whether it is `active`, `degraded`, or `unavailable`.

**Port already in use**  
Another application is using one of the required ports (8080–8084 or 7860).

**Vision unavailable**  
Make sure `models\vision\` contains both the main `.gguf` and the matching `mmproj*.gguf`.

**Discord or Google not connecting**  
Check that the credential file is in `config\google\` (or that `google.client_secret_path` points to it) and that `"enabled": true` is set in `config.json`. See [CREDENTIALS.md](CREDENTIALS.md) for the exact file names and locations.
