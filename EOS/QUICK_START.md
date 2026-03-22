# EOS — Quick Start

This is the shortest path to a running system. It assumes no prior setup and uses the standard backend bundle.

---

## Prerequisites

**Python 3.11** must be installed before running anything else.

1. Download from https://www.python.org/downloads/
2. During installation, check **"Add Python to PATH"**

To verify Python is on your PATH, open a command prompt and run:

```
python --version
```

---

## Step 1 — Run Setup

Right-click `Setup-Full.ps1` → **"Run with PowerShell"**

If Windows asks about execution policy, click **Open** or type `Y` when prompted.

This downloads approximately 13 GB of models and binaries into the EOS folder. It only needs to run once.

---

## Step 2 — Verify the install

Open a command prompt in the EOS folder and run:

```
python verify.py
```

This checks Python packages, binaries, model files, port availability, and `config.json` validity. Fix any errors it reports before continuing.

---

## Step 3 — Start the backend servers

For the recommended stack, run:

- `start-standard.bat`

That opens separate windows for:

- Main model (port 8080)
- Tool helper (port 8082)
- Thinking helper (port 8083)

Wait for each window to print a ready message before proceeding.

---

## Step 4 — Start EOS

Run:

- `start-eos.bat`

EOS discovers the running services and prints a summary like:

```
Main model: active
Tool helper: active
Thinking helper: active
Creativity helper: unavailable (fallback to main)
Vision: unavailable
STT: active
TTS: active

Effective capabilities:

chat: available
tools: available
reasoning: available
creativity: degraded
vision: unavailable
voice: available
```

When it prints `Starting WebUI at http://127.0.0.1:7860/`, EOS is ready.

---

## Step 5 — Open the interface

Navigate to **http://127.0.0.1:7860/** in your browser.

The admin panel is at **http://127.0.0.1:7860/admin**.

---

## Step 6 — Set up optional capabilities

The core system is running. The following capabilities need additional setup:

| Capability | What you need | Instructions |
|---|---|---|
| **Discord bot** | Bot token in `AI personal files\Discord.txt` | [CREDENTIALS.md — Discord](CREDENTIALS.md#discord-bot) |
| **Google Calendar / Gmail / Drive** | OAuth JSON in `AI personal files\` | [CREDENTIALS.md — Google](CREDENTIALS.md#google-workspace-calendar-gmail-drive) |
| **Computer Use** | Enable in admin panel, approve apps | [USER_GUIDE.md — Computer Use](USER_GUIDE.md#computer-use) |
| **Vision** | Run `start-vision-gpu.bat` (GPU required) | [MODELS.md — Vision](MODELS.md#vision-model--modelsvision) |
| **Voice (STT/TTS)** | Already active if `Setup-Full.ps1` ran successfully | [USER_GUIDE.md — Voice](USER_GUIDE.md#voice-input-and-output) |
| **Worldview** | Drop documents into `data\worldview\sources\` | [USER_GUIDE.md — Worldview](USER_GUIDE.md#worldview-system) |

None of these are required. Skip anything you do not need.

---

## Status check anytime

To inspect the current system state without restarting anything, run:

- `status-eos.bat`

or:

```
python eos.py --status
```

---

## If something fails

- Re-run `Setup-Full.ps1`
- Re-run `python verify.py` and read the output carefully
- Use `status-eos.bat` to see which services are active, degraded, or unavailable
- Check [INSTALL.md](INSTALL.md) for detailed troubleshooting
