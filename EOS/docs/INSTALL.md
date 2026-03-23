# EOS — Installation Guide

## Step 1 — Install Python

Install Python 3.11 or newer from https://www.python.org/downloads/ and enable **Add Python to PATH**.

---

## Step 2 — Run setup

Use one of these scripts:

- `setup\Setup-Full.ps1` — downloads the full recommended model/binary set
- `setup\Setup-Lite.ps1` — downloads supporting assets but expects you to provide large models yourself

---

## Step 3 — Verify the installation

Run:

```
python verify.py
```

This checks Python packages, binaries, model files, port availability, and the canonical `config.json`.

---

## Step 4 — Launch EOS

### Recommended flow

1. Start the backend bundle you want:
   - `launchers\start-minimal.bat`
   - `launchers\start-standard.bat`
   - `launchers\start-full.bat`
2. Start the runtime bootstrap:
   - `start-eos.bat`

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
| Google Calendar / Gmail / Drive | Download an OAuth JSON and put it in `AI personal files\` |
| Computer Use | Enable in admin panel → Control & Permissions → Capabilities |
| Vision | Run `launchers\start-vision-gpu.bat` alongside your normal backend bundle |

Full instructions for Discord and Google: **[CREDENTIALS.md](CREDENTIALS.md)**

Full instructions for all capabilities: **[USER_GUIDE.md](USER_GUIDE.md)**

---

## Troubleshooting

**"python is not recognized"**
Python is not on your PATH. Re-install Python and enable the PATH option.

**A launcher window closes immediately**
Open a command prompt and run the same `.bat` script manually to read the error output.

**A backend is missing from EOS startup summary**
Run `status-eos.bat` to see whether it is `active`, `degraded`, or `unavailable`.

**Port already in use**
Another application is using one of the required ports (8080–8084 or 7860).

**Vision unavailable**
Make sure `models\vision\` contains both the main `.gguf` and the matching `mmproj*.gguf`.

**Discord or Google not connecting**
Check that the credential file is in `AI personal files\` and that `"enabled": true` is set in `config.json`. See [CREDENTIALS.md](CREDENTIALS.md) for the exact file names and locations.
