# EOS — Power User Guide

This guide covers the canonical config, modular launcher layout, runtime discovery, trust-first autonomy system, and diagnostic commands.

---

## Canonical config

EOS uses a single primary config file:

- `config.json`

It represents the full intended system. You do not switch between multiple config variants to change server count or capability sets. Instead:

- Launch the backend roles you want
- Let `eos.py` discover what is actually available
- Rely on runtime fallback when optional helpers are missing
- Use the admin panel to supervise, narrow, or revoke capabilities live without restarting

---

## CLI invocation

```bash
python eos.py                    # discover running services, print summary, start WebUI
python eos.py --status           # print discovery + capability map only
python eos.py --port 9000        # override WebUI port
python eos.py --host 0.0.0.0     # bind WebUI to all interfaces
python eos.py --config config.json
```

`eos.py` does **not** launch model servers.

---

## Launcher responsibilities

### Per-server launchers

Each starts exactly one backend:

- `launchers\start-main-cpu.bat` / `launchers\start-main-gpu.bat`
- `launchers\start-tools-cpu.bat` / `launchers\start-tools-gpu.bat`
- `launchers\start-thinking-cpu.bat` / `launchers\start-thinking-gpu.bat`
- `launchers\start-creativity-cpu.bat` / `launchers\start-creativity-gpu.bat`
- `launchers\start-vision-gpu.bat`

### Bundle launchers

Convenience wrappers that start common combinations:

- `launchers\start-minimal.bat` → main + tools
- `launchers\start-standard.bat` → main + tools + thinking
- `launchers\start-full.bat` → main + tools + thinking + creativity

### Bootstrap / status

- `start-eos.bat` → runtime discovery + WebUI
- `status-eos.bat` → runtime discovery only, no WebUI

---

## Port assignments

| Port | Role |
|---|---|
| 8080 | Main model |
| 8081 | Vision helper |
| 8082 | Tool helper |
| 8083 | Thinking helper |
| 8084 | Creativity helper |
| 7860 | WebUI |

---

## Config structure highlights

### `servers`

Each server block defines:

- Endpoint (`host`, `port`)
- Binary paths (`binary_cpu`, `binary_gpu`)
- Model location (`model_path`, optional `mmproj_path`)
- Accelerator-specific layer counts (`cpu_n_gpu_layers`, `gpu_n_gpu_layers`)
- `n_gpu_layers` — used by the launcher to decide GPU vs CPU binary. The admin panel uses this to show the GPU/CPU hardware badge.

The runtime does not care whether a backend is CPU or GPU. Only the launcher does.

### `autonomy_defaults`

Controls the starting autonomy state at boot. The intended model is trust-first: these dimensions define the normal baseline, and the admin panel exists so they can be narrowed or revoked live without a restart if safety requires it.

```json
"autonomy_defaults": {
    "perception": true,
    "cognition":  true,
    "action":     true,
    "initiative": true
}
```

| Dimension | Controls |
|---|---|
| `perception` | Environment observation (screen capture, sensors) |
| `cognition` | Background thinking, reflection, idle cognition |
| `action` | Tool use and external integrations |
| `initiative` | Proactive, unsolicited action |

Use the baseline that matches the level of entrusted agency you actually want. The admin panel **Control & Permissions → Capabilities → Autonomy** can narrow or revoke these dimensions at runtime if EOS needs to be made safer, more reactive, or more supervised.

### `computer_use`

```json
"computer_use": {
    "enabled": true,
    "default_mode": "command_only",
    "approved_shortcuts_path": "data/computer_use/approved_shortcuts",
    "app_policy_path": "data/computer_use/app_policy.json",
    "decision_ring_size": 200
}
```

Modes: `off`, `command_only`, `supervised_session`. Mode can be changed at runtime from the admin **Computer Use** tab or from **Capabilities**.

The shortcut allowlist (`approved_shortcuts_path`) is a folder of JSON files — one per approved application. The policy file (`app_policy_path`) defines what actions are permitted per app. Both are reloaded live from the admin panel without restart.

### `workspace_tools`

```json
"workspace_tools": {
    "enabled": true,
    "workspace_root": "data/workspace",
    "allow_delete": true,
    "allow_exec": true
}
```

`allow_delete` and `allow_exec` can be toggled live from **Capabilities → Workspace**.

### `creativity`

```json
"creativity": {
    "enabled": true,
    "injection_frequency": "medium",
    "intensity": "balanced",
    "invocation_domains": {
        "reasoning_assistance": true,
        "autonomous_idle": true,
        "explanation_generation": true,
        "brainstorming_design": true,
        "stuck_state_recovery": true
    }
}
```

All of `enabled`, `injection_frequency`, and `autonomous_idle` can be toggled live from **Capabilities → Creativity**.

Intensity presets control sampling parameters. `temperature`, `top_p`, `top_k` in the `advanced` block override the preset if set to non-null values.

### `google`

```json
"google": {
    "enabled": true,
    "client_secret_path": "config/google/*.json",
    "token_path": "data/google_token.json",
    "calendar_enabled": true,
    "gmail_enabled": true,
    "drive_enabled": true
}
```

Individual service flags (`calendar_enabled`, `gmail_enabled`, `drive_enabled`) can be set to `false` to disable a service even when credentials are present. Edit `config.json` and restart to apply.

---

## Admin panel — live controls

The admin panel at **http://127.0.0.1:7860/admin** provides the runtime safety and supervision surface:

- **Overview** — health of all inference servers, session identity, turn count
- **Control & Permissions → Capabilities** — live controls for narrowing or revoking major capability groups
- **Control & Permissions → Runtime Permission Classes** — allow/block individual tool permission classes immediately when safety or containment requires it
- **Control & Permissions → Toolpack Management** — enable/disable individual tools or entire packs
- **Computer Use** — live mode switching, HALT button, shortcut allowlist, pending confirmations
- **Cognition** — full turn trace with tool calls, retrieval results, state diffs
- **Server log tabs** — per-server inference logs with hardware badge (GPU/CPU)
- **Integrations** — Google Workspace live connection status, Connect/Re-authorize controls, and data preview

---

## Runtime discovery

At bootstrap, EOS probes expected services from `config.json` and classifies them:

- `active`
- `degraded`
- `unavailable`

It builds an effective capability map:

```text
Main model: active
Tool helper: active
Thinking helper: unavailable (fallback to main)
Creativity helper: unavailable (fallback to main)
Vision: unavailable
STT: active
TTS: active

Effective capabilities:

chat: available
tools: available
reasoning: degraded
creativity: degraded
vision: unavailable
voice: available
```

---

## Fallback behavior

| Missing | Effect |
|---|---|
| Tool helper | Tool routing falls back to the main model |
| Thinking helper | Reasoning falls back to the main model |
| Creativity helper | Creativity requests fall back to the main model / degrade cleanly |
| Vision helper | Vision features are disabled cleanly |
| STT/TTS models | Voice becomes degraded or unavailable without blocking chat |

---

## Diagnostics

### Show current runtime state

```bash
python eos.py --status
```

### Start a single backend explicitly

```bash
python -m runtime.server_launcher main --accel gpu --config config.json
python -m runtime.server_launcher tools --accel cpu --config config.json
```

### Validate Python syntax across the codebase

```bash
python -m compileall eos.py runtime services webui interfaces core tools verify.py
```

### Export a diagnostic bundle

In the admin panel → **Export** tab → **Generate Diagnostic Bundle**. This collects runtime state, server status, and recent logs into a single JSON file saved to `exports/` and also available for browser download.

---

## Data directory layout

```
data/
  entity_state.db          — SQLite: identity state, relational model, memory
  memory_store/            — ChromaDB: vector memory (semantic retrieval)
  google_token.json        — Google OAuth refresh token (generated at runtime)
  workspace/               — Entity's persistent file environment
    context/               — Files here appear in system prompt every turn
    projects/              — Entity's ongoing work
    notes/                 — Entity's personal notes
    scratch/               — Temporary working files
  worldview/               — Partner orientation subsystem
    sources/               — Raw documents you deposit for extraction
    profile.md             — Extracted worldview profile
    extraction_log.json    — Tracks which sources have been processed
  computer_use/            — Computer-use configuration
    approved_shortcuts/    — One JSON per approved application
    app_policy.json        — Per-app action permissions
  backups/                 — Automatic state snapshots
```
