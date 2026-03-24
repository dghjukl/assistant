# EOS — Code-Documentation Reconciliation Audit
**Date:** 2026-03-23
**Method:** Bidirectional, evidence-grounded. Every claim is sourced to a file and line or explicit config key. Tests are used as corroborating evidence.
**Scope:** Full repository. Not a revalidation of prior `EOS_audit_report.md` — that report's findings are treated as historical context only. All conclusions are drawn from current source state.
**Rule applied:** Every discrepancy has a declared winner. No softening language. The losing side is identified as deficient.

---

## PRIOR AUDIT STATUS (INFORMATIONAL ONLY)

The prior `EOS_audit_report.md` (also 2026-03-23) identified defects DEF-01 through DEF-08, GAP-01 through GAP-06, CFG-01 through CFG-05, and DOCS-01 through DOCS-04. The following are **confirmed resolved** in the current codebase and are not re-reported below:

- DEF-01 (topology attribute misuse): RESOLVED — `_server_health_loop()` now correctly calls `state.is_absent()`, `state.is_ready()`, `state.status.value`.
- DEF-02 (topology endpoint sentinel): RESOLVED — `ServerState.endpoint` field populated during `build_topology_from_config()`.
- DEF-03 (stt.py bare imports): RESOLVED — all imports in guarded `try/except` blocks; `STT_AVAILABLE` and `STT_IMPORT_ERROR` exported.
- DEF-04 (vision.py bare imports): RESOLVED — `VISION_AVAILABLE` and `VISION_IMPORT_ERROR` exported.
- DEF-05 (service_discovery probe): RESOLVED — `ServiceProbe.endpoint` field present; `_probe_stt()` uses guarded import pattern.
- DEF-06 (admin_degradation_status): RESOLVED — correctly uses `state.is_absent()`, `state.is_ready()`, `state.status.value`.
- DEF-07 (cli config path wiring): RESOLVED — `_find_config()` → `create_app(config_path=...)` end-to-end; no env var side-channel.
- DEF-08 (boot.py private alias): RESOLVED — `_resolve_model_path` aliases to `server_runtime` shared helpers.
- GAP-01 (duplicate middleware): RESOLVED — comment in `server.py` correctly states AccessControlMiddleware runs first.
- GAP-02 (port pre-check): RESOLVED — `is_port_bound()` called before launch in `on_demand.py`.
- GAP-03 (on_demand imports boot private): RESOLVED — imports from `runtime.server_runtime`.
- GAP-04 (asyncio.Lock in init): RESOLVED — acceptable in Python 3.10+.
- GAP-05 (auth middleware no origin check): RESOLVED — `AdminAuthMiddleware` calls `_admin_origin_allowed(request)`.
- GAP-06 (model disambiguation): RESOLVED — alphabetical first-pick with `_warn_on_ambiguous_choice()` warning.
- CFG-01 (psutil missing from requirements): RESOLVED — in requirements.txt and pyproject.toml.
- CFG-02 (google_oauth legacy fallback): RESOLVED — no fallback path; docstring confirms.
- CFG-03 (start-standard.bat "hardened" label): RESOLVED — label corrected in `launchers/start-standard.bat`.
- CFG-04 / DOCS-01 through DOCS-04: RESOLVED or superseded by current findings below.

---

## SECTION 1 — CODE DEFICITS

*Items where the code is the deficient side. Documentation (or profile configs) is taken as the source of truth.*

---

### CD-01 — launch_catalog.py: standard bundle assigns wrong server roles
**Classification:** CODE DEFICIT
**Severity:** Critical — every standard-bundle launch starts wrong servers

**Evidence — what the code does:**
`runtime/launch_catalog.py`: `bundle_for("standard").roles == ("primary", "tool", "vision")`
Confirmed by `tests/unit/test_launch_catalog.py` line 13: `assert bundle_for("standard").roles == ("primary", "tool", "vision")`

**Evidence — what is correct:**
`configs/profiles/config.standard.json` (the authoritative profile spec):
- `servers.primary.enabled = true` (resident)
- `servers.tool.enabled = true` (resident)
- `servers.thinking.enabled = true` (on_demand)
- `servers.vision.enabled = false` — comment: "Perception only. Not active in standard profile."
- `servers.creativity.enabled = false`

`docs/PROFILES.md`: "Standard: Main model + Tool extraction + Thinking helper"
`docs/POWER_USER_GUIDE.md`: "start-standard.bat — Main model + Tool extraction + Thinking helper"

**Verdict:** The profile config and both documentation sources agree: standard is `primary + tool + thinking`. The `vision` role appears in standard's `server_activation.baseline_roles` (see CD-03) but is explicitly disabled in the servers section. launch_catalog.py assigns `vision` instead of `thinking` to the standard bundle. **Code is wrong.**

**Impact:** Running `python -m runtime.launch_profile standard` (or `start-standard.bat`) attempts to start the vision server, skips the thinking server. Any cognition, initiative, or investigation subsystem that requests the thinking server finds none.

---

### CD-02 — launch_catalog.py: full bundle is identical to standard bundle
**Classification:** CODE DEFICIT
**Severity:** Critical — full-bundle launch is indistinguishable from standard

**Evidence — what the code does:**
`runtime/launch_catalog.py`: both standard and full bundles resolve to `roles = ("primary", "tool", "vision")`. There is no difference. A user who launches `start-full.bat` gets exactly what `start-standard.bat` provides.

**Evidence — what is correct:**
`configs/profiles/config.full.json`:
- Comment: "Primary (GPU) + Tool (CPU) + Thinking (CPU) + Creativity (CPU). No vision."
- `servers.thinking.enabled = true` (on_demand)
- `servers.creativity.enabled = true` (on_demand)
- `servers.vision.enabled = false` — comment: "Not active in this profile. Use config.vision.json variants for vision support."

`docs/PROFILES.md`: "Full: All of standard + Creativity subsystem"
`docs/POWER_USER_GUIDE.md`: "start-full.bat — All of standard + Creativity + Image understanding (Vision)" *(note: the vision claim in POWER_USER_GUIDE.md is itself wrong — see DD-04, but the creativity addition is correct)*

**Verdict:** The profile config unambiguously shows full = `primary + tool + thinking + creativity`, with no vision. launch_catalog.py makes full and standard identical and assigns neither thinking nor creativity to either. **Code is wrong on both counts.**

**Impact:** `start-full.bat` provides no additional cognitive capacity over `start-standard.bat`. The creativity subsystem is never started via the launch system. Idle cognition, investigation, and initiative subsystems that rely on thinking or creativity servers operate without them.

---

### CD-03 — config.standard.json: internal contradiction between servers and baseline_roles
**Classification:** CODE DEFICIT
**Severity:** High — internal profile config inconsistency

**Evidence:**
`configs/profiles/config.standard.json`:
- `servers.vision.enabled = false`
- `server_activation.baseline_roles = ["primary", "tool", "vision"]`

These two entries directly contradict each other. Vision is declared disabled in the servers section but listed as a baseline resident role in the activation policy section.

`configs/profiles/config.full.json` has the same contradiction: vision disabled in servers but listed as `baseline_roles = ["primary", "tool", "vision"]`.

`configs/profiles/config.hardened.json` has the same contradiction.

**Verdict:** The correct baseline_roles for standard and full (given their servers-enabled state) should be `["primary", "tool"]` with `thinking` (and for full, also `creativity`) declared as `auxiliary_roles`. Vision belongs only in the vision bundle profile. **The baseline_roles field in these profiles is stale — likely copied from a template when the profiles were differentiated.** Code (profile config) is wrong.

**Impact:** `ServerActivationManager` or any component that reads `server_activation.baseline_roles` from the config would attempt to manage vision as a baseline-resident role even though vision is disabled. The resulting behavior depends on which field wins at runtime — an unverified code path (see UNVERIFIED AREA UV-01).

---

### CD-04 — admin_tool_registry() diagnostic: reports only 11 legacy tools, not full toolpack registry
**Classification:** CODE DEFICIT
**Severity:** High — admin panel gives an incomplete and misleading tool inventory

**Evidence — what the code does:**
`webui/app_runtime.py` — `admin_tool_registry()` function (registered at `/admin/tool-registry-diagnostics`): reads from `tools.dispatcher.TOOL_SCHEMA`, which is `{name: entry["args"] for name, entry in TOOL_REGISTRY.items()}`. `TOOL_REGISTRY` in `tools/dispatcher.py` contains exactly 11 tools: `web_search`, `read_file`, `list_dir`, `screen_capture`, `webcam_capture`, `query_memory`, `save_memory`, `list_events`, `write_file`, `create_event`, `send_discord`.

`app_state.tool_registry` (type `ToolRegistry` from `runtime/tool_registry.py`) is a separate, modern governance-model registry populated at startup from the configured toolpacks. `config.standard.json` lists 21 toolpacks; each registers multiple `ToolSpec` entries. `app_state.tool_registry` is NOT consulted by `admin_tool_registry()`.

**Evidence — what is correct:**
`runtime/tool_registry.py` provides `ToolRegistry.summary()`, `all_tools()`, `all_enabled()`, `by_pack()` — a complete API for querying the modern tool set. The admin diagnostic endpoint ignores this entirely.

**Verdict:** The diagnostic endpoint reports the legacy 11-tool extraction schema, not the full toolpack inventory. An operator consulting `/admin/tool-registry-diagnostics` sees at most 11 entries regardless of how many toolpacks are loaded. **Code is wrong.** The modern `app_state.tool_registry` must be consulted.

---

### CD-05 — admin_shadow_databases() default db_path fallback uses wrong filename
**Classification:** CODE DEFICIT
**Severity:** Medium — silent wrong-database access if config read fails

**Evidence:**
`webui/app_runtime.py` — `admin_shadow_databases()` function (from prior read context): uses `cfg.get("db_path", "data/entity_app_state.db")` as a fallback default.

All profile configs specify `"db_path": "data/entity_state.db"`:
- `configs/profiles/config.standard.json` line 155: `"db_path": "data/entity_state.db"`
- `configs/profiles/config.full.json` line 155: `"db_path": "data/entity_state.db"`
- `configs/profiles/config.hardened.json` line 165: `"db_path": "data/entity_state.db"`
- `config.json` (canonical): `"db_path": "data/entity_state.db"`

**Verdict:** The fallback default `"data/entity_app_state.db"` does not match the actual filename used in every profile config. If config read fails, the function silently opens (or attempts to open) the wrong database. **Code is wrong.** Fallback should be `"data/entity_state.db"`.

---

### CD-06 — launch_profile.py argparse description contains stale "hardened" label
**Classification:** CODE DEFICIT
**Severity:** Low — misleading help text in CLI tool

**Evidence:**
`runtime/launch_profile.py`: `parser.description = "Start a hardened EOS launch profile."`

`launchers/start-standard.bat` was corrected (prior CFG-03 fix) and no longer says "hardened." The argparse description in the Python module was not updated in the same pass.

**Verdict:** "Hardened" implies the launch_profile module only applies to security-locked configurations. It handles all profiles (minimal, standard, full, vision). **Code is wrong.** Description should be updated to something accurate (e.g., "Start an EOS launch profile (minimal, standard, full, or vision)").

---

### CD-07 — test_launch_catalog.py asserts incorrect bundle compositions as correct
**Classification:** CODE DEFICIT
**Severity:** High — test enforces wrong behavior, blocks correct fix

**Evidence:**
`tests/unit/test_launch_catalog.py` line 13: `assert bundle_for("standard").roles == ("primary", "tool", "vision")`

This assertion validates the broken state described in CD-01 and CD-02. When launch_catalog.py is corrected to assign `("primary", "tool", "thinking")` to standard, this test will fail and block CI unless updated simultaneously.

**Verdict:** The test is aligned with the code (not the profile configs or documentation). It must be updated when the code is fixed. **Test (code) is wrong.** A correctly written test would assert `bundle_for("standard").roles == ("primary", "tool", "thinking")` and `bundle_for("full").roles == ("primary", "tool", "thinking", "creativity")`.

---

### CD-08 — verify.py does not check psutil availability
**Classification:** CODE DEFICIT
**Severity:** Medium — post-install verifier gives false all-clear

**Evidence:**
`verify.py` REQUIRED_PACKAGES and OPTIONAL_PACKAGES lists do not include `psutil`. `psutil>=5.9.0` is in `requirements.txt` and `pyproject.toml`. The server health loop, `OnDemandServerManager`, and resource threshold checking all depend on psutil at runtime.

`release_gate.py` Gate 5 (`check_requirements_completeness`) verifies psutil is in requirements.txt, but `verify.py` does not probe for it. A clean install that fails to install psutil will pass `python verify.py` and then crash at runtime.

**Verdict:** The verification script is incomplete. psutil must be added to REQUIRED_PACKAGES (or at minimum OPTIONAL_PACKAGES with a warning). **Code (verify.py) is wrong.**

---

## SECTION 2 — DOCUMENTATION DEFICITS

*Items where documentation is the deficient side. Code (or profile configs) is taken as the source of truth.*

---

### DD-01 — MODELS.md does not document multi-GGUF alphabetical selection or the warning it emits
**Classification:** DOCUMENTATION DEFICIT
**Severity:** High — silent behavior surprise on every startup with multiple models present

**Evidence — what the code does:**
`runtime/server_runtime.py`: `resolve_model_path()` selects the first file alphabetically when multiple GGUFs exist in a directory, then calls `_warn_on_ambiguous_choice()` which emits a WARNING-level log. This behavior is silent to the operator at the console unless they are watching logs.

The models directory currently contains **two** GGUFs: `Qwen3-8B-Q6_K.gguf` and `Qwen3.5-9B.Q5_K_M.gguf`. This means the warning fires on every startup in the current state.

**Evidence — what docs say:**
`docs/MODELS.md`: Documents that a directory path will pick "whichever file is found." Does not mention: (a) the alphabetical selection rule, (b) what happens when multiple files exist, or (c) the warning log entry.

**Verdict:** The code behavior is clearly intentional (explicit `_warn_on_ambiguous_choice()` function). Documentation omits it entirely despite it being the active behavior in the shipped state. **Documentation is wrong/incomplete.** MODELS.md must document the alphabetical selection rule and direct operators to check logs for the ambiguity warning.

---

### DD-02 — POWER_USER_GUIDE.md minimal bundle claim: "Main model + Tool extraction only" is wrong
**Classification:** DOCUMENTATION DEFICIT
**Severity:** High — operators expect a tool server that will not start

**Evidence — what the code does:**
`runtime/launch_catalog.py`: `bundle_for("minimal").roles == ("primary",)` — only the primary model. No tool server.

**Evidence — what docs say:**
`docs/POWER_USER_GUIDE.md`: "start-minimal.bat — Main model + Tool extraction only"

**Verdict:** Minimal launches only the primary model. There is no tool extraction server in the minimal bundle. An operator who relies on POWER_USER_GUIDE.md will expect tool extraction capability that does not exist. **Documentation is wrong.** POWER_USER_GUIDE.md must be corrected to "Main model only. No tool extraction, thinking, or vision."

**Note:** PROFILES.md correctly states minimal is the "bare minimum" and does not claim tool extraction. POWER_USER_GUIDE.md contradicts PROFILES.md on this point.

---

### DD-03 — POWER_USER_GUIDE.md full bundle claim: "Vision" included is wrong
**Classification:** DOCUMENTATION DEFICIT
**Severity:** Medium — wrong server expectation for full bundle

**Evidence — what the code does (profile spec):**
`configs/profiles/config.full.json` line 20: `"enabled": false` for vision. Comment: "Not active in this profile. Use config.vision.json variants for vision support."

**Evidence — what docs say:**
`docs/POWER_USER_GUIDE.md`: "start-full.bat — All of standard + Creativity + Image understanding (Vision)"

**Verdict:** The full profile explicitly disables vision. Vision is a separate bundle (`vision` in BUNDLE_KEYS). POWER_USER_GUIDE.md incorrectly adds vision to the full bundle description. **Documentation is wrong.** The correct description is "All of standard (primary + tool + thinking) + Creativity subsystem. No vision server."

---

### DD-04 — INSTALL.md Step 4 launch path conflicts with README and QUICK_START
**Classification:** DOCUMENTATION DEFICIT
**Severity:** Low — new user confusion about preferred entry point

**Evidence:**
`docs/INSTALL.md` Step 4: "Double-click `launchers\Launch EOS.bat`" given as the primary recommended path.
`README.md`: `start-standard.bat` listed as the canonical first launch.
`docs/QUICK_START.md`: References `start-standard.bat` directly.

Neither `INSTALL.md` nor any other doc explains the relationship between `launchers\Launch EOS.bat` and `start-standard.bat`.

**Verdict:** The three documents give inconsistent first-launch instructions. README.md and QUICK_START.md are in agreement; INSTALL.md is the outlier. **INSTALL.md is wrong.** It should align Step 4 with README.md's guidance or explicitly explain when `Launch EOS.bat` is the preferred entry.

---

### DD-05 — INSTALL.md calls start-standard.bat a "hardened default" — label no longer accurate
**Classification:** DOCUMENTATION DEFICIT
**Severity:** Low — misleading safety implication

**Evidence:**
`docs/INSTALL.md`: refers to start-standard.bat as a "hardened default."
`configs/profiles/config.standard.json`: `autonomy_defaults.action = false, autonomy_defaults.initiative = false` (action-restricted), but has `discord.enabled=true`, `google.enabled=true`, full toolpacks loaded — 21 packs including `network_tools`, `fs_tools`, `git_tools`, `process_tools`, `system_cmd_tools`. This is not a "hardened" configuration by any reasonable security definition.
`configs/profiles/config.hardened.json` is the actual hardened profile — 8 packs only, no network, no process tools.

**Verdict:** Standard is not hardened. The hardened profile is an explicit separate artifact. Calling standard "hardened" misrepresents the security posture to an operator installing the system. **Documentation is wrong.**

---

### DD-06 — POWER_USER_GUIDE.md autonomy_defaults description conflicts with profile configs
**Classification:** DOCUMENTATION DEFICIT
**Severity:** Medium — security-relevant default misrepresented

**Evidence — what the profile configs do:**
`configs/profiles/config.standard.json`: `autonomy_defaults.action = false`, `autonomy_defaults.initiative = false`
`configs/profiles/config.hardened.json`: `autonomy_defaults.action = false`, `autonomy_defaults.initiative = false`

**Evidence — what docs say:**
`docs/POWER_USER_GUIDE.md` autonomy section: documents autonomy_defaults as trust-first (all four dimensions = true), matching `config.json` (the canonical config). The POWER_USER_GUIDE documents the canonical config behavior, not the profile config behavior.

`config.json` (canonical): `autonomy_defaults` all true — this IS what's deployed when no profile is applied.

**Verdict:** The documentation accurately describes the *canonical config* (`config.json`) behavior. However, it fails to document that the *profile configs* (`config.standard.json`, `config.hardened.json`) both restrict action and initiative by default. An operator launching via a profile gets different defaults than the documentation implies. **Documentation is incomplete.** POWER_USER_GUIDE.md must distinguish between canonical-config defaults and profile-enforced defaults, and explicitly note that the standard and hardened profiles restrict action and initiative.

---

## SECTION 3 — BOTH SIDES DEFICIENT

*Items where neither the code nor the documentation can be declared the winner without design intent resolution.*

---

### BD-01 — The standard bundle composition is disputed by four sources simultaneously
**Classification:** BOTH DEFICIENT
**Severity:** Critical — the most pervasive single defect in the repository

**All four sources disagree:**

| Source | Standard bundle composition |
|--------|-----------------------------|
| `launch_catalog.py` | primary, tool, **vision** |
| `test_launch_catalog.py` | primary, tool, **vision** (asserts same) |
| `configs/profiles/config.standard.json` (servers enabled) | primary, tool, **thinking** (vision=disabled) |
| `docs/PROFILES.md` | Main model + Tool extraction + **Thinking helper** |
| `docs/POWER_USER_GUIDE.md` | Main model + Tool extraction + **Thinking helper** |

The profile configs and both documentation sources agree on the same answer (thinking, not vision). The code and its test agree on a different wrong answer (vision, not thinking). **launch_catalog.py and test_launch_catalog.py are the losing side** (see CD-01, CD-07), but the profile configs also have an internal inconsistency (baseline_roles includes vision while servers disables it — see CD-03). Both require fixes simultaneously. The design intent cannot be confirmed from code alone because the code is inconsistent with itself.

**Resolution required:** Confirm that standard = `(primary, tool, thinking)` is the intended composition and fix launch_catalog.py, the test, and the baseline_roles field in all profile configs. If vision is intentionally a resident baseline role in the vision bundle only, the baseline_roles in standard/full configs must be updated to reflect the actual enabled servers.

---

### BD-02 — release_gate.py Gate 7 (check_runtime_config_parity) structurally fails against canonical config.json
**Classification:** BOTH DEFICIENT
**Severity:** High — release gate and operational design philosophy are irreconcilable

**Evidence — what Gate 7 checks:**
`release_gate.py` `check_runtime_config_parity()`: reads `config.json` and asserts:
- `autonomy_defaults.action == false`
- `autonomy_defaults.initiative == false`
- `computer_use.enabled == false`
- `computer_use.default_mode == "off"`

**Evidence — what config.json actually contains:**
`config.json`:
- `autonomy_defaults.action = true`
- `autonomy_defaults.initiative = true`
- `computer_use.enabled = true`
- `computer_use.default_mode = "command_only"`

**Evidence — documented design intent:**
`docs/POWER_USER_GUIDE.md`: explicitly documents all four autonomy dimensions as trust-first defaults. This is intentional. The canonical config is meant for the operator/owner who trusts the system.

**Both sides are deficient:** Gate 7 was written with a security-first assumption that `config.json` would be locked down. The actual `config.json` is a trust-first operator config. Neither is wrong in isolation — the gate is appropriate for a release artifact, but the canonical config is appropriate for a trusted-owner deployment. The two design philosophies collide.

**Resolution required:** Either (a) Gate 7 should check the hardened *profile* config rather than `config.json`, or (b) a separate "release candidate" config should be designated for gate validation, or (c) the gate should be documented as expected to fail on the development/owner `config.json` and only checked against configs intended for distribution. The current state means Gate 7 always reports failure when run against the live deployment — this renders the gate non-operational.

---

### BD-03 — "Hardened" label is used inconsistently across four locations with different meanings
**Classification:** BOTH DEFICIENT
**Severity:** Low — conceptual confusion, safety-label ambiguity

**Evidence:**
1. `runtime/launch_profile.py` argparse: `"Start a hardened EOS launch profile."` — implies all profiles are hardened (wrong)
2. `configs/profiles/config.hardened.json` — a specific locked-down profile; correct use of "hardened"
3. `docs/INSTALL.md` — calls start-standard.bat a "hardened default" (wrong; standard is not the hardened profile)
4. INSTALL.md and other docs use "hardened" to mean "sensible default" rather than "security-locked"

**Both sides are deficient:** The code label (launch_profile.py) is stale; the documentation label (INSTALL.md) misuses the term. The concept needs a single definition across all surfaces.

---

## SECTION 4 — CONTRACT / INTERFACE MISMATCHES

---

### CM-01 — admin_enable_tool / admin_disable_tool: dual tool state tracking with unverified synchronization
**Classification:** CONTRACT MISMATCH
**Severity:** High — potential operator-visible enable/disable state divergence

**Evidence:**
`webui/app_state.py` line 42: `tool_states: dict[str, bool]` — a flat dict in AppState.
`runtime/tool_registry.py`: `ToolRegistry.set_enabled(name, enabled)` — an independent enable/disable mechanism on the modern registry.
`webui/app_runtime.py`: `admin_enable_tool()` and `admin_disable_tool()` route handlers (not fully read): expected to update `app_state.tool_states`. Whether they also call `app_state.tool_registry.set_enabled()` is **unverified** (see UV-02).

If the admin enable/disable API only updates `tool_states` without updating `app_state.tool_registry`, the admin panel shows one enable-state while the executor sees another. An operator who disables a tool through the admin UI may find it still executes.

---

### CM-02 — TOOL_SCHEMA / legacy dispatcher vs. modern toolpack registry: two parallel tool APIs with no integration
**Classification:** CONTRACT MISMATCH
**Severity:** Medium — dual governance systems with no declared arbitration rule

**Evidence:**
- `tools/dispatcher.py`: `TOOL_REGISTRY` (11 tools) with permission classes (perception/cognition/action). These are dispatched through the Tool Server via NL-to-JSON extraction.
- `runtime/tool_registry.py`: `ToolRegistry` populated from toolpacks (21 packs in standard profile, each with multiple ToolSpecs). Uses governance model with risk_level, trust_level, ConfirmationPolicy.

There is no code path that bridges these two systems. The legacy dispatcher governs what gets extracted via the Tool Server; the modern toolpack registry governs what gets executed via `ToolExecutor`. A tool named `read_file` exists in BOTH systems independently.

No documentation explains the relationship, arbitration order, or deprecation timeline of the legacy system.

---

### CM-03 — verify.py port check flags running services as warnings, not pass/fail
**Classification:** CONTRACT MISMATCH
**Severity:** Low — verification semantics inversion

**Evidence:**
`verify.py` `_check_ports()`: if a port is already in use, it calls `report.warnings.append(f"port:{port}")`. A port-in-use state is an advisory. But if the EOS webui (port 7860) or a model server (8080–8084) is already bound when the verifier runs, this likely means a conflicting prior EOS instance is running — which is a launch blocker, not a warning.

The verify.py exit code is 0 (pass) even when ports are occupied. A user who runs `python verify.py` before reinstallation will see a passing result despite a blocking port conflict.

---

### CM-04 — config.full.json: server_activation.baseline_roles includes vision despite vision being disabled
**Classification:** CONTRACT MISMATCH
**Severity:** High — activation manager receives contradictory instruction (same root as CD-03, manifests separately in full profile)

**Evidence:**
`configs/profiles/config.full.json`:
- `servers.vision.enabled = false`
- `server_activation.baseline_roles = ["primary", "tool", "vision"]` (from prior template, not updated for full profile)

The full profile should have `baseline_roles = ["primary", "tool"]` with `auxiliary_roles = ["thinking", "creativity"]`, matching the enabled server definitions. This is the same structural defect as CD-03 but present in a separate profile file.

---

### CM-05 — Route conflict risk: /admin/tools/{tool_name}/enable vs /admin/tools/pending
**Classification:** CONTRACT MISMATCH
**Severity:** Low — latent routing fragility

**Evidence:**
`webui/routes/admin_api.py`:
- Line 48 (POST): `/admin/tools/{tool_name}/enable`
- Line 49 (POST): `/admin/tools/{tool_name}/disable`
- Line 107 (GET): `/admin/tools/pending`
- Line 108 (POST): `/admin/tools/confirm/{confirmation_id}`
- Line 109 (POST): `/admin/tools/deny/{confirmation_id}`

FastAPI resolves static path segments over parameterized ones for the same HTTP method. `/admin/tools/pending` (GET) does not conflict with `{tool_name}/enable` (POST). However, a future endpoint added as `POST /admin/tools/pending` would silently shadow the intended route for a tool named "pending". The current state is safe but fragile. Additionally, `confirm` and `deny` as path segments under `/admin/tools/confirm/` could conflict if a tool named "confirm" is ever enabled/disabled.

---

## SECTION 5 — ORDERED FIX PLAN

Priority ordering: P0 = release blocker / data corruption risk, P1 = functional failure, P2 = operational correctness, P3 = polish.

---

### P0 — Critical (Fix before any release)

**FIX-01** (addresses CD-01, CD-02, CD-07, BD-01):
Correct `runtime/launch_catalog.py` bundle definitions:
- `standard`: `roles = ("primary", "tool", "thinking")`
- `full`: `roles = ("primary", "tool", "thinking", "creativity")`
- `minimal`: `roles = ("primary",)` — no change, already correct
- `vision`: verify separately; should include `vision` role
Then update `tests/unit/test_launch_catalog.py` line 13 to assert the corrected compositions.

**FIX-02** (addresses CD-03, CM-04):
Correct `server_activation.baseline_roles` in all three profile configs:
- `configs/profiles/config.standard.json`: change to `["primary", "tool"]`
- `configs/profiles/config.full.json`: change to `["primary", "tool"]`
- `configs/profiles/config.hardened.json`: change to `["primary"]`
And add `thinking` (and for full: `creativity`) to `auxiliary_roles` in each profile.

---

### P1 — High (Fix before operator-facing deployment)

**FIX-03** (addresses DD-01):
Update `docs/MODELS.md` to document:
1. When a directory path is given, EOS selects the first GGUF alphabetically.
2. When multiple GGUFs are present, a WARNING is emitted to the log.
3. Operator action: remove or rename unwanted GGUFs; check startup logs for the ambiguity warning.
4. Document that the current models directory contains two GGUFs (Qwen3-8B-Q6_K.gguf and Qwen3.5-9B.Q5_K_M.gguf) and note which is selected.

**FIX-04** (addresses DD-02, DD-03, DD-06):
Update `docs/POWER_USER_GUIDE.md`:
- minimal: "Main model only. No tool extraction, thinking, or vision."
- standard: "Main model + Tool extraction + Thinking helper (on-demand). No vision."
- full: "Main model + Tool extraction + Thinking helper (on-demand) + Creativity (on-demand). No vision."
- Add a note distinguishing `config.json` (trust-first, all autonomy enabled) from profile configs (standard/hardened both restrict action and initiative by default).

**FIX-05** (addresses CD-04):
Update `runtime/tool_registry.py`-consulting endpoint — or specifically update `admin_tool_registry()` in `webui/app_runtime.py` to:
1. Query `app_state.tool_registry.all_tools()` (modern registry) when available.
2. Fall back to legacy `TOOL_SCHEMA` only if `app_state.tool_registry` is None.
3. Return a unified response distinguishing legacy tools vs. toolpack-registered tools.

**FIX-06** (addresses BD-02):
Resolve the Gate 7 / canonical config conflict. Recommended approach: change `check_runtime_config_parity()` in `release_gate.py` to read from `configs/profiles/config.hardened.json` rather than `config.json`. The hardened profile is the release-appropriate baseline and already contains all required locked values. Document that `config.json` is the owner/dev config and is not the release artifact.

---

### P2 — Medium (Fix for operational correctness)

**FIX-07** (addresses CM-01):
Read `admin_enable_tool()` and `admin_disable_tool()` implementations fully. If they do not call `app_state.tool_registry.set_enabled()`, add that call. Ensure `tool_states` dict and `tool_registry` internal state remain synchronized on every enable/disable operation.

**FIX-08** (addresses CD-05):
In `webui/app_runtime.py` `admin_shadow_databases()`: change default fallback from `"data/entity_app_state.db"` to `"data/entity_state.db"`.

**FIX-09** (addresses CD-08):
Add `psutil` to `verify.py` REQUIRED_PACKAGES (import name `psutil`, pip name `psutil`).

**FIX-10** (addresses DD-04, DD-05):
Update `docs/INSTALL.md`:
- Step 4: replace `launchers\Launch EOS.bat` with `start-standard.bat` as primary recommended path, or add an explanation of when each entry point applies.
- Remove the phrase "hardened default" from the start-standard.bat description.

---

### P3 — Low (Polish)

**FIX-11** (addresses CD-06):
Update `runtime/launch_profile.py` argparse description from `"Start a hardened EOS launch profile."` to `"Start an EOS launch profile (minimal, standard, full, or vision)."` or equivalent accurate description.

**FIX-12** (addresses BD-03):
Audit all uses of "hardened" across docs and code. Establish a single definition: "hardened" means the `config.hardened.json` profile only. Update INSTALL.md, launch_profile.py, and any other surface that uses "hardened" informally.

**FIX-13** (addresses CM-03):
In `verify.py` `_check_ports()`: promote port-occupied status from `warnings` to `required_failures` for the webui port (7860) and the primary model port (8080), since both occupied simultaneously indicates a conflicting running instance. Optional ports (8081–8084) may remain as warnings.

**FIX-14** (addresses CM-02 documentation gap):
Add a section to `docs/POWER_USER_GUIDE.md` or a new `docs/TOOLS.md` explaining: (a) the legacy dispatcher (tools/dispatcher.py, 11 tools) handles NL-to-JSON extraction via the Tool Server; (b) the modern toolpack registry (runtime/tool_registry.py + runtime/toolpacks/) handles governance and execution; (c) the current arbitration relationship between the two systems.

---

## SECTION 6 — UNVERIFIED AREAS / ASSUMPTIONS

The following areas could not be fully verified from the files read. Each represents a risk or assumption that should be confirmed during fix implementation.

**UV-01 — ServerActivationManager behavior when baseline_roles conflicts with servers.enabled:**
The `ServerActivationManager` (referenced in `app_state.py` as `server_activation_manager`) was not read in full. It is unknown which source wins when `server_activation.baseline_roles` lists "vision" but `servers.vision.enabled = false`. If `enabled` takes precedence, the defect is cosmetic. If `baseline_roles` takes precedence, vision would be started despite being disabled. Priority: High. Read `runtime/server_activation_manager.py` (or equivalent) before FIX-02.

**UV-02 — admin_enable_tool / admin_disable_tool synchronization with ToolRegistry:**
These handler bodies were not read in full. Whether `app_state.tool_registry.set_enabled()` is called alongside `app_state.tool_states` updates is unknown. FIX-07 requires reading these functions before implementing the fix.

**UV-03 — Total toolpack tool count:**
The summary states "80+ tools" in the modern registry. This was not verified from source. `runtime/toolpacks/` directory contents were not read. The actual tool count per pack and total toolpack-registered tool count are unknown.

**UV-04 — config.minimal.json existence:**
No minimal profile config file was found in `configs/profiles/`. The glob returned: base, base-creativity, base-thinking, full, hardened, standard, vision — but no `config.minimal.json`. If `launch_profile standard` reads the standard profile config, it is unclear how the minimal profile is configured. Either the minimal bundle uses defaults without a profile config, or the file was not created, or it exists elsewhere.

**UV-05 — Credential file in git history:**
`AI personal files/client_secret_28673004202-rspaevt6uuvjm9h7ts2k1bgjr1327rjo.apps.googleusercontent.com.json` is present in the workspace and is excluded by `.gitignore`. However, if the file was committed before the gitignore entry was added, it may remain in git history. `release_gate.py` Gate 4 checks tracked files for credential patterns but does not audit git history. A `git log --all --full-history -- "AI personal files/"` check was not performed.

**UV-06 — release_gate.py Gate 4 credential pattern matching against currently staged content:**
Gate 4 uses `git ls-files` to find tracked files. The credential file is `.gitignore`d. Whether earlier commits included this file was not verified. If they did, a `git filter-branch` or `git filter-repo` cleanup is required; this is outside the scope of this audit but flagged as a potential credential exposure.

**UV-07 — vision bundle composition in launch_catalog.py:**
`BUNDLE_KEYS = ("minimal", "standard", "full", "vision")`. The `vision` bundle's `roles` tuple was not independently confirmed. It likely includes `("primary", "tool", "vision")` or similar, but was not read from source. The vision bundle is the appropriate home for the vision server role — this should be confirmed before finalizing FIX-01.

**UV-08 — admin_tools_pending / tool confirmation system integration with ToolRegistry:**
Routes `/admin/tools/pending`, `/admin/tools/confirm/{id}`, `/admin/tools/deny/{id}` exist in admin_api.py (lines 107–109). Whether the pending confirmation system is integrated with `ToolRegistry.confirmation_policy` (ConfirmationPolicy.SOFT_CONFIRM / HARD_CONFIRM) or is a separate mechanism was not verified.

---

## CONSISTENCY CONFIRMED (INFORMATIONAL)

The following areas were checked and found consistent between code and documentation:

- **Middleware execution order**: AccessControlMiddleware classifies origin first; AdminAuthMiddleware uses that classification. Starlette reverse-registration order is correctly applied and documented in `server.py` comment.
- **Google OAuth credential path**: `config/google/*.json` glob — matches config.json, all profile configs, and `docs/CREDENTIALS.md`. No legacy fallback path.
- **CLI `--config` argument**: End-to-end wiring confirmed (`cli.py` → `create_app(config_path=...)` → `app.state.config_path`).
- **psutil in requirements**: Present in both `requirements.txt` and `pyproject.toml` with `>=5.9.0` constraint.
- **STT and Vision guarded imports**: Both use `try/except Exception` blocks with `AVAILABLE` and `IMPORT_ERROR` exports.
- **release_gate.py Gate 5** (requirements completeness): pydantic, fastapi, keyring, jsonschema all present in requirements.txt.
- **autonomy_defaults in config.json**: All four dimensions true — matches POWER_USER_GUIDE.md documentation of trust-first design for the canonical config.
- **Tool Server model string**: `"model": "lfm2-tool"` in dispatcher — no documentation conflict found.
- **start-standard.bat label**: Correctly updated, no longer says "hardened."
- **Port assignments**: 8080=primary, 8081=vision, 8082=tool, 8083=thinking, 8084=creativity, 7860=webui — consistent across config.json, profile configs, and docs.
- **`server_activation.auxiliary_roles`**: Correctly lists `["thinking", "creativity"]` in config.json and all profile configs.
- **BUNDLE_KEYS exported surface**: `legacy_surfaces` not present in `export_catalog()` (prior DEAD-01 resolved).

---

*End of EOS Code-Documentation Reconciliation Audit — 2026-03-23*
