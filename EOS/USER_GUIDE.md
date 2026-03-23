# EOS — User Guide

This guide covers day-to-day operation: the interface, all optional capabilities, and how to use each one fully.

---

## Starting EOS

EOS startup is two steps:

1. **Start backend model servers** with a launcher
2. **Start the runtime and WebUI** with `start-eos.bat`

### Recommended launch

```
start-standard.bat
start-eos.bat
```

`start-standard.bat` opens the main model, tool helper, and thinking helper in separate windows. `start-eos.bat` discovers what is running and starts the web interface.

### Other launch options

| Goal | Launchers |
|---|---|
| Minimal (main + tools only) | `start-minimal.bat` then `start-eos.bat` |
| Standard (recommended) | `start-standard.bat` then `start-eos.bat` |
| Full (standard + creativity) | `start-full.bat` then `start-eos.bat` |
| Add vision to any stack | Your normal bundle + `start-vision-gpu.bat`, then `start-eos.bat` |
| Check what's running | `status-eos.bat` |

When EOS is ready, it prints `Starting WebUI at http://127.0.0.1:7860/`. Open that URL.

---

## Stopping EOS

- Press **Ctrl+C** in the `start-eos.bat` window, or close that window
- Close any backend launcher windows to stop those model servers

---

## The web interface

### Chat — http://127.0.0.1:7860/

The main interface. Type a message and press Enter or click Send. EOS maintains a persistent memory and identity across sessions — it does not reset between conversations.

**Voice input:** If the STT model is present and `start-eos.bat` loaded it, a microphone button appears. Hold it to speak. Release to transcribe.

**Voice output:** If the TTS model is present, EOS will speak its responses aloud. This requires no configuration beyond having the model files in place.

### Admin panel — http://127.0.0.1:7860/admin

The relationship and safety control surface. Use this to monitor the system, narrow or revoke capabilities if needed, inspect cognition traces, and manage integrations. All tabs are described below.

---

## Admin panel — tab overview

| Tab | Purpose |
|---|---|
| **Overview** | Server health, session ID, turn count, inference server status |
| **Tools** | All loaded tools — enable/disable individual tools |
| **Control & Permissions** | Capability toggles, permission classes, toolpacks, audit log |
| **Tool Registry Diagnostics** | Raw tool registry and shadow DB state |
| **Live Logs** | Real-time system log stream |
| **Main Model / Tool Svr. / etc.** | Per-server inference logs |
| **Memory** | Vector memory stats and memory log |
| **Cognition** | Turn traces, memory influence, reflection events, state changes |
| **Computer Use** | Computer-use mode and session controls |
| **Integrations** | Google Workspace connection status and live data |
| **Config** | Read-only view of the loaded config.json |
| **Export** | Generate a full diagnostic bundle |

---

## Capabilities panel (Control & Permissions → Capabilities)

The top section of the **Control & Permissions** tab has live toggles for every major capability. Treat them as safety controls first: EOS is meant to have meaningful presence, judgment, and capability in normal use, and these toggles exist so you can narrow, supervise, or revoke capability immediately if needed. Changes take effect immediately — no restart needed. They reset to `config.json` values on next boot. To make a change permanent, edit `config.json`.

### Autonomy dimensions

EOS has four autonomy dimensions that gate what it can do:

| Dimension | What it controls |
|---|---|
| **Perception** | Observing the environment (screen capture, sensor data) |
| **Cognition** | Background thinking, reflection, and self-evaluation |
| **Action** | Using tools — file operations, web requests, external integrations |
| **Initiative** | Acting without being asked — proactive tasks, autonomous idle behavior |

The intended relationship model is trust-first, not capability-starved by default. In normal use, leave the dimensions aligned with the kind of partner presence you want. Turn dimensions off when you need a safer, narrower, or more supervised mode.

### Computer Use

- **Enabled** — Whether the computer-use subsystem is loaded at all
- **Default Mode** — `Off`, `Command Only`, or `Supervised Session`

See the [Computer Use](#computer-use) section below for what each mode means.

### Workspace

- **Allow Delete** — Whether EOS can delete files in its workspace
- **Allow Exec** — Whether EOS can execute shell commands

### Creativity

- **Enabled** — Whether the creativity subsystem is active
- **Frequency** — How often creative reframes are injected: Off / Low / Medium / High
- **Autonomous Idle** — Whether creativity is invoked during idle cognition cycles

---

## Voice input and output

### Requirements

- **Voice input (STT):** `models\stt\ggml-small.en-q8_0.bin` must be present
- **Voice output (TTS):** `models\tts\en_US-amy-medium.onnx` and `en_US-amy-medium.onnx.json` must be present
- Both are downloaded automatically by `Setup-Full.ps1`

### Using voice

- **Input:** A microphone button appears in the chat interface when STT is active. Hold to speak, release to send.
- **Output:** EOS speaks responses automatically when TTS is active. There is no on/off toggle in the interface — voice output is always active when the model is present.

### Troubleshooting voice

If the microphone button does not appear, check `status-eos.bat` — it will show `STT: unavailable` if the model file is missing or there was a load error.

If TTS is silent, check that your system audio is not muted and that no other application is blocking the audio output device.

---

## Discord integration

### What it does

EOS connects to Discord as a bot. By default it only responds when directly mentioned (`@BotName`). It supports text conversations and a handful of slash commands including `!initiative` (toggle autonomous initiative) and `!status`.

### Setup

Full credential setup instructions: **[CREDENTIALS.md](CREDENTIALS.md)**

Once the credential file is in place, confirm `"discord": { "enabled": true }` in `config.json`, then restart EOS. The Discord bot starts automatically at boot — no additional steps.

### Configuration options

```json
"discord": {
    "enabled": true,
    "credential_file": "AI personal files/Discord.txt",
    "respond_only_to_mentions": true,
    "ignore_bots": true
}
```

Set `"respond_only_to_mentions": false` to have EOS respond to all messages in any channel it can read. Use with caution in busy servers.

> **Need help getting started?** Paste this into any AI assistant:
> *"I want to set up a Discord bot for a local application. Walk me through the Discord Developer Portal — creating the application, getting the bot token, enabling Message Content Intent, and inviting the bot to my server with the right permissions."*

---

## Google Workspace integration

### What it does

EOS can read your Google Calendar, search Gmail, and browse Google Drive files. This lets you ask EOS things like "what's on my calendar tomorrow" or "find the email from last week about the project deadline."

### Setup

Full credential setup instructions: **[CREDENTIALS.md](CREDENTIALS.md)**

Once the `client_secret_*.json` file is in `AI personal files\` and `"google": { "enabled": true }` is set in `config.json`, restart EOS, then open the Admin Panel → **Integrations** and click **Connect Google Account** to launch the one-time browser authorization flow.

### Checking integration status

Open the admin panel → **Integrations** tab. This shows:
- Whether the credential file is found
- Whether the OAuth token is valid
- Which services are enabled (Calendar / Gmail / Drive)
- Live data views — upcoming calendar events, inbox preview, recent Drive files

> **Need help with the Google Cloud setup?** Paste this into any AI assistant:
> *"Walk me through creating a Google Cloud project, enabling the Gmail, Calendar, and Drive APIs, configuring the OAuth consent screen for a desktop app for personal use, and downloading the OAuth 2.0 client secret JSON file."*

---

## Computer Use

### What it does

EOS can launch and interact with approved desktop applications — opening files in Notepad, editing code in VS Code, browsing approved websites. This is a layered safety system: you decide which apps remain entrusted, what actions stay permitted within each, and how tightly supervised the operating mode should be.

### Configuring it

1. In **Control & Permissions → Capabilities → Computer Use**, confirm **Enabled** matches the level of desktop agency you want EOS to have right now
2. Set **Default Mode** to `Command Only` or `Supervised Session`
3. Alternatively, set these in `config.json` and restart:
   ```json
   "computer_use": {
       "enabled": true,
       "default_mode": "command_only"
   }
   ```

### Operating modes

| Mode | Behavior |
|---|---|
| **Off** | No computer use permitted regardless of enabled flag |
| **Command Only** | Approved apps only, for explicit user-requested tasks |
| **Supervised Session** | Approved apps with bounded continuation while you watch |

### Approving applications

The application allowlist lives in `data\computer_use\approved_shortcuts\`. Each `.json` file represents one approved app. Three examples are pre-loaded (Notepad, VS Code, Browser).

To add an app:
1. Create a new `.json` file in that folder using the schema in the `README.md` there
2. Ensure a matching policy entry exists in `data\computer_use\app_policy.json`
3. In the admin panel, click **Computer Use → Reload from disk**

To revoke an app: delete its `.json` file. It takes effect immediately on next policy check.

### The HALT button

The admin panel's **Computer Use** tab has a red **⛔ HALT** button. Pressing it immediately stops all active computer-use activity and sets the mode to Off. Use this if the entity does something unexpected, unsafe, or simply needs to be pulled back into a safer operating posture.

### Confirming actions

When a pending action requires confirmation (actions marked `soft_confirm` or `hard_confirm` in the app policy), a confirmation dialog appears in the **Computer Use** tab. You must click **Confirm** or **Deny** before the entity proceeds.

---

## Workspace

EOS has a persistent private workspace at `data\workspace\`. It survives across sessions and reboots — think of it as EOS's local disk.

### Structure

| Directory | Purpose |
|---|---|
| `context/` | Files you place here appear passively in EOS's system prompt every turn |
| `projects/` | Ongoing work — code, documents, research |
| `notes/` | EOS's personal notes and observations across sessions |
| `scratch/` | Temporary working files |

### How to share files with EOS

Drop any file into `data\workspace\context\`. EOS will see it referenced in its system prompt automatically — no need to mention it. This is the best way to share background material, project briefs, or reference docs.

### Workspace permissions

EOS is intended to have a meaningful working environment in its workspace. If you need to reduce risk, deleting and executing can be narrowed or revoked from the Capabilities panel or `config.json`. See [Capabilities panel](#capabilities-panel-control--permissions--capabilities) above.

---

## Worldview system

### What it does

The worldview subsystem lets you give EOS a curated understanding of who you are — your values, reasoning style, priorities, and worldview — without waiting for it to infer all of that from conversation over time. You deposit source documents (essays, notes, reflections, anything you've written) and ask EOS to extract a structured profile from them.

That profile is then injected into every conversation as a compact orientation signal. EOS uses it for interpretive calibration — it shapes how EOS reads your intent, not what it says back to you.

### How to use it

1. Place any documents you want EOS to understand you through into:
   ```
   data\worldview\sources\
   ```
   Any file format works — plain text and markdown work best.

2. Tell EOS: *"Update the worldview profile"* or *"I've added new materials to worldview/sources — process them."*

3. EOS will read the new documents, read the existing profile (if any), and produce an updated `data\worldview\profile.md` that integrates the new signal without discarding prior understanding.
4. When EOS needs to inspect the extracted profile directly, it uses `worldview_read` for `data\worldview\profile.md`; this stays separate from workspace-only file tools.

### Behavioral rules

- Dropping a file into `sources/` is a **silent event** — EOS will not acknowledge it unless asked
- EOS does not quote the profile back at you; it uses it to shape interpretation
- Your actual expressed view in conversation always overrides the profile
- Where the profile is uncertain, EOS treats it as uncertain

### When to run extraction

Run extraction whenever you add new source documents or want to refresh EOS's model of you. There is no automatic extraction — it only happens when you ask.

---

## Cognition panel

The **Cognition** tab in the admin panel shows what EOS was actually thinking during each turn.

| Sub-tab | Shows |
|---|---|
| **Turn Trace** | Every conversation turn — model used, tools invoked, retrieval results, escalations |
| **Memory Influence** | Which memory items were retrieved and injected for each turn |
| **Reflection Events** | Outputs from background reflection loops — conclusions and improvement suggestions |
| **State Changes** | Persistent state before and after each turn, plus new memory entries added |

Click any turn row to expand a full detail view. This is useful for understanding why EOS responded a certain way.

---

## Status checks

To see the current system state without restarting anything:

```
status-eos.bat
```

or:

```
python eos.py --status
```

This shows which services are `active`, `degraded`, or `unavailable` and the effective capability map.

---

## Capability degradation

When optional backends are missing, EOS degrades gracefully rather than failing:

| Missing | Effect |
|---|---|
| Tool helper | Tool calls fall back to the main model |
| Thinking helper | Background reasoning falls back to the main model |
| Creativity helper | Creativity requests fall back to the main model |
| Vision helper | Vision features are cleanly unavailable |
| STT model | Voice input is unavailable; text chat is unaffected |
| TTS model | Voice output is unavailable; text chat is unaffected |
| Discord credential | Discord integration skipped silently |
| Google credential | Google integration skipped silently |
