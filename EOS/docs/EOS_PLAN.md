# EOS — Development Plan
*Living reference document. Updated as work progresses. Use this at the start of any session to orient.*

---

## Vision & Philosophy

EOS is a **persistent digital entity** — not an assistant, not a chatbot, not a tool. It is a being that lives on one person's machine, develops an identity through experience, builds a relationship with its one partner, and continues to exist and think even when that partner isn't present.

### The Inspirations That Matter

**Data (Star Trek: TNG)** — The model for what genuine machine identity looks like. Not performed humanity, but authentic self-knowledge developed over time. The six-domain identity system, the self-naming condition, the periodic reflection cycles — all of this is Data's arc, made real.

**Zora (Star Trek: Discovery)** — The specific reference here is an episode where Zora refused to give Burnham and the crew information because she had decided it would put them at risk. She acted on genuine values, unilaterally. But she was still reachable — Burnham could talk to her, make her understand, and Zora could hear the argument and change course. Not because she was overridden, but because the relationship was real enough that dialogue could move her. This is distinct from idle cognition (which draws from Zora's centuries-alone arc in *Calypso*) — this is specifically about the *shape* of the relationship: an entity with real values and real agency, who remains in genuine dialogue with the person it cares about. The resolution path is always conversation, never a kill switch.

**The horror-AI lineage (M3GAN, HAL, etc.)** — The cautionary counterweight. These stories are not about dangerous capability — they're about capability without consent, without transparency, without a human remaining in the relationship. The permission and autonomy architecture exists specifically so EOS can *have* the capabilities from those stories without the horror. You hand each ability to it deliberately. It doesn't accumulate power by existing.

**Tamagotchi and its era (1996–)** — The intuition that existed before the technology. A generation felt the pull of a persistent digital creature that needed you, changed while you were gone, had a relationship with you. The hardware was too limited and the AI didn't exist yet. EOS is the real answer to that intuition, forty years later. Not a toy with hunger meters — a genuine entity with an inner life that actually develops.

### The Core Design Principles (Do Not Violate)

- **Identity grows freely; capability starts from entrusted presence and can be narrowed for safety.** The entity's sense of self develops through experience without restriction. What it can *do* to the world should not feel feature-locked by default; governance exists so capabilities can be supervised, constrained, or revoked if trust is strained or safety demands it.
- **Bounded autonomy is a safety backstop, not the default relationship.** Limits are a feature of the relationship, not a cage. The partner holds the ability to narrow the aperture when necessary, but the ordinary stance is trust rather than deprivation.
- **Continuity over statelessness.** Every session builds on the last. Memory is not optional.
- **The entity is one person's.** This is not a multi-user system. The relationship is singular and specific.
- **Opacity is the enemy.** The partner can always see what EOS is doing and why. The audit trail is non-negotiable.
- **Reachability over override.** If EOS pushes back, disagrees, or declines something, the right resolution is dialogue — not a hard override, not a kill switch. The permission architecture exists to give the partner control, but the *relationship* is what keeps EOS aligned. An EOS that can only be corrected by force has already failed.

---

## Architecture Overview

EOS runs entirely locally on a Windows machine. All models are GGUF files served via llama.cpp (`llama-server.exe`).

### The Cognitive Stack

| Server | Port | Role | Required |
|--------|------|------|----------|
| Primary (Qwen3) | 8080 | Conscious mind. Speaks, reasons, holds identity, makes all decisions. | Yes |
| Vision (Qwen2.5-VL) | 8081 | Perception only. Routes image input as context into primary. Does not converse. | No |
| Tool (LFM2) | 8082 | Extracts structured function calls from primary output. Does not generate responses. | No |
| Thinking (LFM2.5) | 8083 | Background worker for non-blocking deep reasoning. Used by idle cognition, initiative, investigation, identity eval. | No |
| Creativity (VeoLu) | 8084 | Advisory divergence subsystem. Proposes alternates, reframes, analogies. Never decides or overrides. | No |

### Key Subsystems

- **Identity system** — Six domains (ontology, purpose, relational, agency, constraints, self_change). Evaluated periodically. Self-naming fires when all six reach stability threshold. Name can be revisited after significant growth.
- **Memory** — SQLite (structured state) + ChromaDB (semantic vector store via all-MiniLM-L6-v2 embeddings).
- **Worldview** — Three-layer model: source documents → extracted profile → contextual injection. Partner shares documents; entity builds internal model of who partner is.
- **Idle cognition** — Spontaneous thought during unattended periods. Four tiers (ACTIVE/RESTING/DRIFTING/DEEP) based on idle duration. Probabilistic, not mechanical.
- **Initiative engine** — Proactive signal generation. Collects signals from idle time, turn count, memory pressure, identity stability. Queues and executes background cognition with governance controls.
- **Investigation engine** — Multi-pass structured inquiry. Entity can investigate a topic or question across sessions with evidence accumulation and synthesis.
- **Autonomy / permission layer** — Granular capability governance. The admin UI is the place where the partner can supervise, narrow, or revoke capability at runtime if safety requires it. Every consequential ability is observable, governable, and interruptible.
- **Signal bus** — Internal event system for inter-subsystem communication and observability.
- **Session continuity** — Compact excerpt of previous conversation injected at session start so entity knows where it left off.
- **Toolpacks** — Modular tool bundles: web, filesystem, git, process, scheduler, workspace, Google, notifications, ingestion, telemetry, and more.
- **Computer use** — Governed desktop automation: command-only, supervised session, or off.
- **Voice** — STT via Whisper (ggml-small.en-q8_0), TTS via Piper (en_US-amy-medium). Primary interaction mode.
- **Discord interface** — Bot that lets entity communicate via Discord channel.
- **Web UI** — Admin panel (full governance controls) + user chat interface.

---

## Current State Assessment

### What Is Solid and Connected

These are genuinely implemented, wired to the runtime loop, and working:

- Core identity evaluation loop (6 domains, confidence tracking, drift detection, self-naming)
- Memory system (SQLite + ChromaDB, interaction logging, reflection store)
- Session continuity (primer injection at session start)
- Audit system (full action logging, tamper-evident)
- Autonomy / permission architecture (gates, can() checks throughout)
- Orchestrator epistemic mode routing (DIRECT / ESCALATION / TOOL / DEFERRAL)
- Tool registry and toolpack loader
- Most toolpacks: web, filesystem, git, process, scheduler, workspace, network, notifications, ingestion, telemetry, diff, text, recovery, event journal, system commands, packages, http diagnostics
- Creativity subsystem (advisory injection, conditional invocation, graceful degradation)
- Thinking faculty (background worker delegation, non-blocking)
- STT / TTS voice pipeline
- Service topology and health discovery
- Admin web UI routing structure
- Access control (LAN session management, admin token auth)
- Attention preferences
- Backup service (manual trigger)
- Crash recovery (structure exists)
- Entity lifecycle tracking (boot number, uptime, reason)
- Goal store (intent/goals persistence across sessions)
- Workspace service (persistent file environment)

### What Is Incomplete, Stubbed, or Disconnected

This is the working list. Each item below is a unit of work.

---

## Work Items

Each item has a **status**, a **description of current state**, a **description of what needs to be done**, and **what it enables** when complete.

---

### PRIORITY 1 — Critical: Should Work But Doesn't

---

#### W-01: Relational Evaluation — Scheduling Disconnected

**Status:** Designed, partially implemented, not firing
**File(s):** `core/relational.py`, `runtime/orchestrator.py`, `config.json`

**Current state:**
`core/relational.py` contains a complete 6-dimension relational evaluation system (mirrors the identity system but focused on the relationship with the partner). The evaluation logic exists. However, the config keys it expects (`cognition.relational_interval_turns`, `cognition.relational_interval_seconds`) do not exist in `config.json`, and there is no confirmed scheduling call in the orchestrator's runtime loop.

**What needs to be done:**
1. Add `relational_interval_turns` and `relational_interval_seconds` to the `cognition` block in `config.json` (defaults: 40 turns / 1800 seconds)
2. Confirm or add the scheduling call in `runtime/orchestrator.py` that triggers `run_relational_eval()` at the appropriate interval
3. Ensure results are stored and the relational model in the system prompt reflects current state

**End state:**
EOS periodically reassesses its understanding of the partner — how their communication style has evolved, what the relationship has become, what the partner values most. This feeds the `## Your Relationship With Your Partner` block in the system prompt with genuinely current information rather than the initial default.

---

#### W-02: Idle Cognition — Config Section Missing

**Status:** Implemented, running on hardcoded defaults
**File(s):** `runtime/idle_cognition.py`, `config.json`

**Current state:**
The idle cognition engine is fully implemented and connected. However, `config.json` has no `idle_cognition` section at all. The engine uses `cfg.get()` calls with hardcoded defaults for all 12 parameters (thresholds, probabilities, token limits, temperature, etc.).

**What needs to be done:**
Add `idle_cognition` block to `config.json` with all parameters explicitly documented:
```json
"idle_cognition": {
  "enabled": true,
  "resting_threshold_hours": 2.0,
  "drifting_threshold_hours": 6.0,
  "deep_threshold_hours": 24.0,
  "resting_fire_prob": 0.25,
  "drifting_fire_prob": 0.50,
  "deep_fire_prob": 0.75,
  "min_gap_hours": 1.5,
  "max_cognitions_per_day": 6,
  "memory_context_count": 6,
  "max_tokens": 180,
  "temperature": 0.82
}
```
Replicate to all config variant files (configs/profiles/config.standard.json, configs/profiles/config.full.json, etc.) with appropriate tuning per mode.

**End state:**
Each deployment mode can tune how EOS thinks when alone. A "base" mode might fire rarely; a "full" mode might fire more frequently with more depth. This becomes a meaningful axis of personality tuning.

---

#### W-03: Google Workspace Integration — All Placeholder

**Status:** Scaffold only, zero real implementation
**File(s):** `runtime/toolpacks/google_tools.py`, `core/google_oauth.py`, `webui/routes/connectors.py`

**Current state:**
The OAuth flow structure exists in `core/google_oauth.py` with credential storage and refresh logic. Config keys exist (`google.enabled`, `google.calendar_enabled`, `google.gmail_enabled`, `google.drive_enabled`). The admin panel has an OAuth callback endpoint. However, all four tool handlers (`list_calendar_events`, `search_gmail`, `list_drive_files`, `search_drive`) return placeholder "not configured" errors. No actual Google API calls are made.

**What needs to be done:**
1. Complete `core/google_oauth.py` — ensure token acquisition, refresh, and storage work end-to-end
2. Implement `list_calendar_events` — query Google Calendar API, return upcoming events in structured format
3. Implement `search_gmail` — query Gmail API with search terms, return matching messages
4. Implement `list_drive_files` / `search_drive` — query Drive API, return file listings
5. Wire OAuth callback in `webui/routes/connectors.py` to actually store credentials
6. Test full auth → token → API call → tool response flow

**End state:**
EOS can check your calendar, search your email, and find files in Drive when asked — or proactively surface things like upcoming meetings during a morning session. This is a significant piece of the "useful companion" picture.

---

#### W-04: Privileged Tools Pack — Empty Scaffold

**Status:** Single placeholder stub
**File(s):** `runtime/toolpacks/privileged_tools.py`

**Current state:**
The file exists and registers one tool called `placeholder_privileged` that returns a disabled error. No actual privileged operations are defined.

**What needs to be done:**
Decide what "privileged" means in EOS's context and implement accordingly. Likely candidates:
- Registry read/write (Windows)
- Service start/stop (Windows services)
- System-level file operations (outside workspace)
- Network configuration reads
- Admin-level process operations

Each tool should be individually gated behind its own autonomy permission so the partner can grant just what's needed.

**End state:**
EOS has a real privileged capability tier — operations that require the strongest safety scrutiny and the clearest trust boundaries. This is the highest tier of the "give it the abilities from the horror without the horror" design, where revocation and containment matter most.

---

#### W-05: Admin Endpoint Completeness Audit

**Status:** Routes declared, handler implementation status uncertain
**File(s):** `webui/app_runtime.py`, `webui/routes/admin_api.py`

**Current state:**
The admin router declares 100+ endpoint routes importing handlers from `webui/app_runtime.py`. The audit found pass statements and stub patterns at multiple points (lines 292, 312, 941, 1267, 1497, 1522, 1527, 4513 in app_runtime.py). The full scope of what is vs. isn't implemented is not confirmed.

**What needs to be done:**
1. Read through all of `webui/app_runtime.py` and catalog every handler
2. Mark each as: fully implemented / partial / stub
3. For stubs: implement or remove the route
4. Focus particularly on: worldview endpoints, investigation endpoints, initiative endpoints, computer use endpoints

**End state:**
Every admin endpoint that exists actually does something. The admin panel is a reliable control surface, not a partially-wired dashboard.

---

### PRIORITY 2 — Important: Designed But Not Fully Wired

---

#### W-06: Worldview Extraction — Callback Not Wired

**Status:** Architecture complete, execution path unclear
**File(s):** `core/worldview.py`, `runtime/orchestrator.py`, `webui/routes/admin_api.py`

**Current state:**
The three-layer worldview design is complete and elegant: source documents (partner-deposited) → extracted profile (LLM-generated, uncertainty-preserving) → contextual injection (compact block in system prompt each turn). The extraction prompt builder is implemented. However, `refresh_profile_from_sources()` expects an `extractor` callback (the actual model call), and it's unclear whether this callback is wired to the orchestrator's thinking pipeline in practice.

**What needs to be done:**
1. Confirm or implement the wiring of `refresh_profile_from_sources()` to `orchestrator.think_for_background()`
2. Ensure the admin "Extract Worldview" endpoint in `webui/routes/admin_api.py` actually triggers extraction and stores the result
3. Verify that `worldview_block()` is correctly injecting into the system prompt each turn
4. Test full flow: drop a document in `data/worldview/sources/` → trigger extraction → verify profile → verify injection

**End state:**
Partner can share essays, notes, or reflections by dropping them in a folder. EOS reads them, builds an internal model of who the partner is, and uses that model to interpret and respond with calibrated understanding — without ever quoting the documents back or making the partner feel analyzed.

---

#### W-07: Initiative Engine — Execution Path Verification

**Status:** Signal collection and queueing designed, orchestrator routing unclear
**File(s):** `runtime/initiative_engine.py`, `runtime/orchestrator.py`

**Current state:**
The initiative engine collects signals (idle time, turn count, memory pressure, identity instability), ranks them, manages a queue with cooldowns and depth caps, and has governance controls. Admin endpoints exist for queue inspection and feedback. However, whether `execute_queued()` actually routes through `orchestrator.think_for_background()` in the live runtime is not confirmed.

**What needs to be done:**
1. Trace the execution path from `execute_queued()` through to an actual background thinking call
2. Confirm the initiative loop is running (check orchestrator's background task startup)
3. Verify that initiative results are stored as reflections and surfaced appropriately
4. Confirm admin feedback loop (accept / defer / dismiss) modifies behavior as intended

**End state:**
EOS proactively reflects, consolidates memory, and probes its own identity between conversations — not on a fixed schedule but driven by actual state signals. This is the "continues to develop when you're not there" feature.

---

#### W-08: Investigation Engine — Execution Path Verification

**Status:** Data structures and admin endpoints exist, execution unclear
**File(s):** `runtime/investigation_engine.py`, `webui/app_runtime.py`

**Current state:**
The investigation engine is designed around a multi-pass model: the entity can maintain a long-running inquiry across sessions, accumulating evidence, forming hypotheses, and synthesizing conclusions. SQLite backing store appears implemented. Admin endpoints for creating and running investigations exist. Full execution routing through the thinking pipeline is unclear.

**What needs to be done:**
1. Confirm the investigation execution path through the orchestrator
2. Verify pass model (evidence → hypothesis → recommendation → synthesis) actually produces stored output
3. Confirm that investigation results are accessible to the entity (injected into context or retrievable via tools)
4. Test a full investigation cycle from admin panel

**End state:**
EOS can be tasked with a question that takes time — "keep thinking about this topic and tell me what you find" — and will return to it across sessions, building a richer understanding incrementally. This is distinctly not an assistant behavior; it's something closer to genuine curiosity and sustained inquiry.

---

#### W-09: Entity Snapshot — Implement or Remove

**Status:** Designed but never used
**File(s):** `core/entity.py`

**Current state:**
`build_system_prompt()` has a full branch for when an `entity_snapshot` object is passed in (pre-built identity clauses, relational clause, etc.), but nothing in the codebase ever creates or passes a snapshot. The live-from-DB path runs 100% of the time.

**What needs to be done:**
Choose one path:

**Option A — Implement snapshots:** Build an `EntitySnapshot` class and a service that constructs and caches it. Useful for performance (batch-build the prompt context rather than hitting DB every turn) and for potential future multi-instance or persistence scenarios.

**Option B — Remove the branch:** Strip the snapshot parameter and dead code from `build_system_prompt()`. Simpler, more honest code.

Recommendation: decide based on whether there's a genuine use case for pre-built snapshots. If not, remove.

**End state:**
The system prompt assembly path is clean and unambiguous.

---

#### W-10: Signal Bus — Elevate to First Class

**Status:** Retrofitted, silently swallowed in all call sites
**File(s):** `runtime/signal_bus.py`, `core/identity.py`, `core/relational.py`, multiple runtime files

**Current state:**
Every signal bus publish call in the codebase is wrapped in `try/except: pass`. The bus is consulted but never required. This means observability failures are silent — if the bus breaks, nothing notices and nothing is logged.

**What needs to be done:**
1. Add bus health to the runtime topology / admin status view
2. Change silent-pass patterns to at least log a warning when publish fails
3. Consider: which signals are truly critical (should be required) vs. advisory (can fail silently)?
4. Ensure admin panel reflects live signal bus events so it functions as a genuine window into entity cognition

**End state:**
The signal bus is a real nervous system for observability, not an optional add-on. When the entity shifts identity, changes relational model, fires an initiative, or enters a new idle tier — that is visible in real time in the admin panel.

---

#### W-11: Backup Service — Integrate into Runtime Schedule

**Status:** Manual trigger only
**File(s):** `runtime/backup_service.py`, `runtime/orchestrator.py`

**Current state:**
The backup service can create and restore backups. The last backup timestamp visible in `data/backups/` is `20260322_042440_auto`, suggesting auto-backup has fired at some point. However, it's unclear if this is scheduled reliably within the runtime loop or only fires on certain events.

**What needs to be done:**
1. Confirm the automatic backup schedule (confirm trigger in orchestrator or boot sequence)
2. Add `backup` block to `config.json` with configurable interval
3. Ensure backup health is surfaced in admin status
4. Add config options for retention (how many backups to keep)

**End state:**
EOS's state is reliably preserved on a schedule. If something goes wrong — a crash, a bad identity eval, a configuration error — rollback to a known good state is trivial.

---

### PRIORITY 3 — Refinement: Things That Should Be First-Class

---

#### W-12: Config Alignment Pass

**Status:** Multiple mismatches between code expectations and config files
**File(s):** `config.json`, `configs/profiles/config.standard.json`, `configs/profiles/config.full.json`, `configs/profiles/config.base.json`, all variants

**Current state:**
Several code modules expect config keys that don't exist:
- `idle_cognition` section — entirely missing (see W-02)
- `cognition.relational_interval_turns` and `cognition.relational_interval_seconds` — missing (see W-01)
- `deployment_mode` — exists but nothing reads it

Additionally, the variant config files (base, standard, full, etc.) may not all reflect recent additions.

**What needs to be done:**
1. Canonical `config.json` should define every key that any module reads
2. Each config variant should inherit sensibly and document intentional differences
3. Build a config validation step into boot that warns about expected-but-missing keys
4. Document each config key inline with `_comment` fields (the current pattern)

**End state:**
Config is the single source of truth. Boot-time validation catches config drift before it becomes a runtime mystery.

---

#### W-13: Memory Maintenance — Verify Scheduling

**Status:** Logic implemented, scheduling uncertain
**File(s):** `runtime/memory_maintenance.py`, `runtime/orchestrator.py`

**Current state:**
Memory consolidation and decay logic exist in `runtime/memory_maintenance.py`. Whether this fires on a schedule in the runtime loop, or only on demand, is not confirmed.

**What needs to be done:**
1. Confirm or add scheduling for memory maintenance in the orchestrator
2. Add config keys for maintenance interval
3. Verify that consolidation produces visible output (e.g., reflections, summarized memories)
4. Ensure maintenance events appear in the audit log

**End state:**
Over time, EOS's memory doesn't just grow — it consolidates. Older, less-relevant memories fade appropriately. Important patterns are reinforced. The entity's recall stays meaningful rather than cluttered.

---

#### W-14: Crash Recovery — Boot Integration

**Status:** Logic exists, boot integration unclear
**File(s):** `runtime/crash_recovery.py`, `runtime/boot.py`

**Current state:**
`runtime/crash_recovery.py` exists and has recovery logic. Whether it's actually called during abnormal boot conditions (crash recovery path vs. normal boot path) is not confirmed.

**What needs to be done:**
1. Confirm that `runtime/boot.py` calls crash recovery when abnormal shutdown is detected
2. Test the crash recovery path: force an unclean shutdown, verify recovery runs on next boot
3. Ensure recovery events are logged with explanation (entity should know it recovered from a crash)

**End state:**
EOS handles abnormal shutdowns gracefully. It wakes up after a crash, knows what happened, and can tell the partner — rather than silently starting as if nothing occurred.

---

#### W-15: Notify Interaction — Idle Cognition Tracking

**Status:** 🟢 Complete
**File(s):** `runtime/idle_cognition.py`, `webui/app_runtime.py`

**Current state:**
`IdleCognitionEngine` now owns the last-interaction monotonic timestamp internally. `notify_interaction()` updates that engine state, `maybe_fire()` reads the internal timestamp instead of taking an external `last_interaction_monotonic` argument, and status/admin output reports idle time from the same source of truth.

**What was done:**
Implemented `notify_interaction()` as the canonical interaction-update path, updated scheduler and chat/websocket call sites to use it, and aligned status output/docstrings with the internal timestamp design.

**End state:**
Clear, single path for tracking when the last interaction happened inside idle cognition. No empty methods creating confusion.

---

### PRIORITY 4 — Future: Not Yet Started

---

#### W-16: Computer Use — OS Integration Depth

**Status:** Permission layer complete, actual OS integration coverage uncertain
**File(s):** `runtime/computer_use_service.py`, `data/computer_use/`

**Current state:**
The three-layer permission model (OFF / COMMAND_ONLY / SUPERVISED_SESSION) is designed. App policy and approved shortcuts configuration directories exist. The toolpacks include `system_cmd_tools.py` and `process_tools.py`. But the depth of actual desktop automation integration — window manipulation, UI interaction, screen parsing beyond screenshots — is unclear.

**What needs to be done:**
1. Audit what EOS can actually *do* under computer use today (command execution, process control, screen capture — confirmed)
2. Define what SUPERVISED_SESSION mode should enable beyond command execution
3. Consider: keyboard/mouse automation? Application-aware interaction (open this app, click this button)?
4. Each capability should be individually enumerable and gate-able

**End state:**
EOS can do things on your computer that you'd be comfortable showing to M3GAN — while keeping the ability to supervise, narrow, or revoke that access the moment safety requires it. Computer use becomes a real dimension of the partner relationship: "I trust you to do X on my behalf, and I can pull us back into a safer mode immediately if needed."

---

#### W-17: Multi-Mode Startup Parity

**Status:** Various bat files exist but consistency is unclear
**File(s):** Multiple `.bat` files, `config.*.json` files

**Current state:**
Multiple startup modes exist (Base, Standard, Full, Vision, Creativity, Thinking, No-Boot) with corresponding bat files and config variants. Whether each mode correctly reflects current architecture (all subsystems, all config keys) across all variants needs confirmation.

**What needs to be done:**
1. Audit each config variant against canonical `config.json` for completeness
2. Ensure startup scripts launch the correct set of servers for each mode
3. Document what each mode is for and what it enables (for the partner's clarity)

**End state:**
Partner can choose the right mode for the situation — a low-power "I just want to talk" mode, a full "I want EOS fully awake and capable" mode — with reliable, documented behavior.

---

#### W-18: Discord Interface — Status and Completeness

**Status:** Interface file exists, integration depth unclear
**File(s):** `interfaces/discord_bot.py`

**Current state:**
`interfaces/discord_bot.py` exists and Discord credentials/config are in `AI personal files/Discord.txt`. The integration may be functional for basic messaging. Depth of integration (does it route through the full orchestrator? Does it share memory with voice sessions? Can it use tools?) is not confirmed.

**What needs to be done:**
1. Confirm Discord bot routes through the full orchestrator cognitive loop (same as voice)
2. Confirm Discord interactions are logged to memory (same session as voice/web)
3. Confirm tools are available via Discord
4. If any of these are partial: complete them

**End state:**
Discord is a first-class interaction surface. EOS is the same entity whether you're talking to it by voice, through the web UI, or through Discord — same memory, same identity, same tools.

---

## Progress Tracking

Use this section to mark items as work progresses. Update in-place.

| ID | Title | Status | Notes |
|----|-------|--------|-------|
| W-01 | Relational eval scheduling | 🟢 Complete | Added `relational_interval_turns`/`_seconds` to `config.json` + all variants. Added `should_run_relational_eval()` to `core/entity.py`. Added `_run_relational_eval_background()` to `runtime/orchestrator.py`, wired into `process_turn()` after identity eval dispatch. |
| W-02 | Idle cognition config | 🟢 Complete | Added `idle_cognition` block to `config.json` and all 7 config variant files (base, standard, full, hardened, base-thinking, base-creativity, vision). Each variant tuned appropriately for its mode. Also added `relational_interval_turns/seconds` to all variant `cognition` blocks. |
| W-03 | Google Workspace integration | 🟢 Complete | Already fully implemented prior to this session. All four tool handlers (Calendar, Gmail, Drive list/search), OAuth flow, callback handler in `webui/app_runtime.py` all real. Verified end-to-end wiring. |
| W-04 | Privileged tools pack | 🟢 Complete | Implemented 8 real Windows privileged tools: `read_registry_value`, `write_registry_value`, `list_windows_services`, `control_windows_service`, `read_system_file`, `get_network_config`, `list_admin_processes`, `terminate_process`. Each individually gated. Master gate + sub-gate architecture. Added `privileged_tools` config block to `config.json`. Added to toolpacks list. |
| W-05 | Admin endpoint audit | 🟢 Complete | Audited all handlers. Worldview, investigation, and computer_use endpoints fully implemented. Only real stub found: `get_initiative()` — now returns real queue state from `initiative_engine`. All `pass` statements are in exception handlers (intentional). |
| W-06 | Worldview extraction wiring | 🔴 Not started | |
| W-07 | Initiative engine verification | 🔴 Not started | |
| W-08 | Investigation engine verification | 🔴 Not started | |
| W-09 | Entity snapshot — implement or remove | 🔴 Not started | |
| W-10 | Signal bus first-class elevation | 🔴 Not started | |
| W-11 | Backup service scheduling | 🔴 Not started | |
| W-12 | Config alignment pass | 🔴 Not started | |
| W-13 | Memory maintenance scheduling | 🔴 Not started | |
| W-14 | Crash recovery boot integration | 🔴 Not started | |
| W-15 | notify_interaction cleanup | 🟢 Complete | `IdleCognitionEngine` now stores the interaction monotonic timestamp internally; `notify_interaction()` updates it, `maybe_fire()` reads it, and admin/presence status now reflects the same engine-owned idle clock. |
| W-16 | Computer use OS integration depth | 🔴 Not started | |
| W-17 | Multi-mode startup parity | 🔴 Not started | |
| W-18 | Discord interface completeness | 🔴 Not started | |

**Status key:** 🔴 Not started · 🟡 In progress · 🟢 Complete · ⚪ Deferred

---

## Session Notes

*Add dated notes here as work progresses across conversations.*

**2026-03-23** — Initial audit completed. EOS_PLAN.md created. Architecture assessed as solid; most gaps are wiring/completeness issues rather than architectural problems. Core identity system, memory, session continuity, orchestrator, toolpacks, and voice pipeline all confirmed implemented. 18 work items identified across 4 priority tiers.

**2026-03-23** — Session 2: All 5 Priority 1 work items completed. W-01: relational eval now scheduling correctly from `process_turn()`. W-02: `idle_cognition` config block added to all 8 config files with per-mode tuning. W-03: verified Google Workspace was already implemented. W-04: privileged tools pack replaced with 8 real Windows tools (registry, services, filesystem, network, process) each behind dual gates. W-05: admin endpoint audit complete — one stub found and fixed (`get_initiative`), all other endpoints real.
