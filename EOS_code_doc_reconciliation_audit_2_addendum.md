# EOS Audit Addendum — Architecture Clarification & Revised Findings
**Date:** 2026-03-23
**Supersedes:** Selected findings in `EOS_code_doc_reconciliation_audit_2.md`
**Reason:** Confirmed architecture of subservient server model changes source-of-truth hierarchy and invalidates several prior conclusions.

Read this addendum alongside the original audit. Where this document contradicts the original, this document is authoritative.

---

## ARCHITECTURAL CONTEXT (New — Read First)

The following facts were confirmed from source code and direct clarification. Every revised finding below flows from these.

**Source-of-truth hierarchy (authoritative to least):**
1. `config.json` — canonical runtime config; what is actually deployed
2. `runtime/launch_catalog.py` — defines resident server sets per bundle
3. `runtime/server_activation.py` + `runtime/on_demand.py` + `runtime/boot.py` — activation policy, elastic lifecycle, boot logic
4. `configs/profiles/*.json` — **reference variants only, not live runtime configs; expected to drift**
5. Documentation (docs/\*.md) — operator-facing description; currently partially stale

**Server residency model (confirmed from config.json, boot.py, on_demand.py):**

| Role | Residency | Activation | Boot behavior |
|------|-----------|-----------|---------------|
| primary | resident | persistent | Launched at boot; fatal if absent |
| tool | resident | persistent | Launched at boot; optional (graceful degradation) |
| vision | resident | persistent | Launched at boot if enabled; optional |
| thinking | auxiliary | on_demand | **Skipped at boot**; elastic spin-up by policy |
| creativity | auxiliary | on_demand | **Skipped at boot**; elastic spin-up by policy |

**Boot.py enforcement (lines 100–108):**
- `enabled=false` → `mark_absent(role, intentional=True)`, skipped
- `enabled=true + activation_mode=on_demand` → `mark_absent(role, intentional=True)`, skipped
- `enabled=true + activation_mode=persistent` → launched, health-checked

**On-demand spin-up path (thinking):**
`runtime/thinking_faculty.py` → `manager.ensure("thinking", task_type="deep_reasoning", escalation=True)` → `ServerActivationPolicy.evaluate()` → if allowed: `_start(role)` via `launch_server()` + health wait.

**On-demand spin-up path (creativity):**
`runtime/creativity_service.py` → `manager.ensure("creativity", ...)` → same policy path.

**Resident readiness-check (tool):**
`tools/dispatcher.py` → `manager.ensure("tool", ...)` → `role not in _managed_roles` branch → returns `srv.endpoint if srv.is_ready() else None`. **No launch logic fires.** This is a readiness fetch, not spin-up.

**`OnDemandServerManager._managed_roles`** = only roles where `policy.config.role_policies[role].activation_mode == "on_demand"`. Under canonical config: `{"thinking", "creativity"}`. Tool is not managed.

**`configs/profiles/*.json` status:** These are reference configuration variants documented for operator customization. They are not loaded by the standard batch-file launch sequence (which uses `config.json`). The user has confirmed that drift between profile reference variants and the canonical config is acceptable and expected.

---

## SECTION A — RETRACTIONS

The following findings from the original audit are **fully retracted**. The code behavior they cited as deficient is correct; the audit's source-of-truth selection was wrong.

---

### RETRACT CD-01 — launch_catalog.py standard bundle assigns wrong server roles
**Original claim:** Standard bundle should be `(primary, tool, thinking)`; code assigns `(primary, tool, vision)`; code is wrong.

**Correction:** The canonical config (`config.json`) has `server_activation.baseline_roles = ["primary", "tool", "vision"]`. Vision is in the resident baseline. `launch_catalog.py` is correct. The profile variant `configs/profiles/config.standard.json` which had vision disabled is a reference variant and is not authoritative. The documentation sources (PROFILES.md, POWER_USER_GUIDE.md) that say thinking is in the standard bundle are **wrong** — see new DD-NEW-01 below.

---

### RETRACT CD-07 — test_launch_catalog.py asserts incorrect bundle compositions as correct
**Original claim:** `assert bundle_for("standard").roles == ("primary", "tool", "vision")` is wrong and blocks the fix.

**Correction:** The test is correct per canonical config. It correctly asserts what the standard bundle does. No fix required.

---

## SECTION B — REVISED FINDINGS

The following original findings are revised — not retracted — with updated severity, framing, or scope.

---

### REVISED CD-02 — full bundle identical to standard (still valid; root cause updated)
**Original framing:** Full and standard are identical; both should be `(primary, tool, vision)` per code but docs say full adds creativity/thinking.

**Revised framing:** The catalog correctly assigns `(primary, tool, vision)` as the resident set for both standard and full — that is accurate. The defect is: **the two bundles provide no functional difference to the operator**. Under the canonical `config.json`, both bundles launch identical resident servers, and both have thinking and creativity available on-demand (since `config.json` lists both in `auxiliary_roles`). The catalog descriptions attempt to differentiate them in text ("Resident baseline stack only" vs "optional vision preloaded when supported") but the roles are identical and the descriptions are not accurate.

**Actual functional distinction of bundles:**
The difference between standard and full should come from the config variant used at launch (config.standard.json vs config.full.json — the reference variants). Full's reference variant enables creativity on-demand; standard's does not. But since both batch files use `config.json`, the distinction is lost at runtime.

**Revised verdict:** CODE DEFICIT — the full bundle, as launched by `start-full.bat`, is operationally identical to standard because both use `config.json` and the catalog roles are the same. There is no mechanism to differentiate them. A user who chooses "full" expecting additional capability gets nothing different.

**Revised fix:** Either (a) `start-full.bat` should explicitly point to `config.full.json` rather than `config.json`, or (b) the catalog should express which on-demand roles each bundle makes *available* (not just resident), so standard makes thinking available and full makes thinking + creativity available, and the activation policy can be tuned per bundle.

---

### REVISED CD-03 — profile config baseline_roles contradiction (downgraded)
**Original severity:** Critical — internal profile config inconsistency causing servers to start incorrectly.

**Revised severity:** Low — informational.

**Reason:** Profile configs are reference variants, not authoritative runtime configs. `build_topology_from_config()` uses `servers.<role>.enabled` as the actual gate for what starts; `baseline_roles` is metadata for the activation policy. Since the canonical `config.json` is what runs, the stale `baseline_roles` in the reference variants does not affect live deployments. The contradiction is real but cosmetic in practice.

**Revised verdict:** Still wrong (stale data that could mislead someone editing a profile variant), but Low priority. Fix when profile configs are next updated for another reason.

---

### REVISED BD-01 — Bundle composition dispute (reclassified from Both-Deficient to Documentation Deficit)
**Original framing:** Four sources in three-way disagreement; no winner possible without design intent.

**Revised framing:** Design intent is now clear from the canonical config and the clarified source hierarchy. The dispute resolves entirely as a documentation deficit:

- **CODE IS CORRECT:** `launch_catalog.py` standard = `(primary, tool, vision)` — matches canonical config
- **DOCS ARE WRONG:** PROFILES.md says standard = "Main model + Tool extraction + Thinking helper" — wrong; thinking is on-demand, vision is resident
- **REFERENCE VARIANTS ARE STALE:** `config.standard.json` has thinking=enabled/vision=disabled — valid reference variant for a no-vision deployment, but not the canonical deployment config
- **TEST IS CORRECT:** `test_launch_catalog.py` asserts the right behavior

This finding is reclassified and moved to the documentation deficit section below as DD-NEW-01 and DD-NEW-02.

---

### REVISED CM-04 — config.full.json baseline_roles (downgraded)
**Original severity:** High — activation manager receives contradictory instruction.

**Revised severity:** Low — same reasoning as REVISED CD-03. Reference variant, not authoritative runtime config. No live impact.

---

## SECTION C — NEW FINDINGS

---

### NEW DD-NEW-01 — PROFILES.md and POWER_USER_GUIDE.md: standard bundle description wrong on two counts
**Classification:** DOCUMENTATION DEFICIT
**Severity:** High — operators choosing and configuring bundles are given incorrect information

**Evidence — what docs say:**
`docs/PROFILES.md`: "Standard: Main model + Tool extraction + Thinking helper"
`docs/POWER_USER_GUIDE.md`: "start-standard.bat — Main model + Tool extraction + Thinking helper"

**Evidence — what is correct:**
Standard bundle resident set (`launch_catalog.py` + canonical `config.json`): **primary + tool + vision**.
Thinking is available on-demand (auxiliary, elastic) — it is NOT in the resident set, not started at boot.

**Two specific errors:**
1. Docs say thinking is in standard; **thinking is on-demand, not resident**. It will spin up when the orchestrator requests it via `ThinkingFaculty.deliberate()`, subject to policy evaluation, but it is not "included" in the standard bundle in any meaningful pre-start sense.
2. Docs omit vision entirely from the standard description; **vision is a resident baseline server in standard** when enabled.

**Verdict:** Documentation is wrong. Fix PROFILES.md and POWER_USER_GUIDE.md. The correct description of standard is: "Main model (resident) + Tool extraction (resident) + Vision (resident, when enabled). Thinking is available on-demand when requested by the executive; not started at boot."

---

### NEW DD-NEW-02 — PROFILES.md and POWER_USER_GUIDE.md: full bundle description wrong or incomplete
**Classification:** DOCUMENTATION DEFICIT
**Severity:** High

**Evidence — what docs say:**
`docs/PROFILES.md`: "Full: All of standard + Creativity subsystem"
`docs/POWER_USER_GUIDE.md`: "start-full.bat — All of standard + Creativity + Image understanding (Vision)"

**Evidence — what is correct:**
Full bundle resident set: `(primary, tool, vision)` — **identical to standard**. The differentiation between full and standard is:
- Standard (per config.standard.json reference variant): thinking on-demand, creativity disabled
- Full (per config.full.json reference variant): thinking on-demand, creativity on-demand

But since both `start-standard.bat` and `start-full.bat` use `config.json`, and `config.json` has both thinking and creativity as auxiliary on-demand, the distinction is currently absent at runtime (see REVISED CD-02).

**Two errors:**
1. POWER_USER_GUIDE says full adds Vision — vision is in BOTH standard and full resident sets; it is not exclusive to full.
2. Both docs treat creativity as a resident addition ("all of standard + Creativity"), when creativity is on-demand and elastic, not a booted server.

---

### NEW DD-NEW-03 — No documentation explains the resident vs. on-demand distinction to operators
**Classification:** DOCUMENTATION DEFICIT
**Severity:** High — without this, all bundle descriptions are misleading

**Evidence:**
No documentation (PROFILES.md, POWER_USER_GUIDE.md, README.md, INSTALL.md) explains that EOS has two classes of helper server:
- **Resident helpers** (tool, vision): started at boot, always available, part of the fixed resident set
- **Elastic helpers** (thinking, creativity): **not started at boot**; spun up on policy-gated demand; idle-stopped after timeout; may be denied activation if resources are constrained

An operator reading the current docs cannot understand:
- Why thinking isn't listed in running processes after boot
- Why creativity comes and goes during operation
- That tool extraction and vision are always on (once started)
- That the thinking server launching mid-operation is expected behavior, not a fault

**Verdict:** A "Cognitive Architecture" or "Server Lifecycle" section must be added to POWER_USER_GUIDE.md or a new ARCHITECTURE.md. It must explain the resident/elastic split, the on-demand policy gate, the idle timeout, and the cooldown mechanism. This context is prerequisite for all bundle descriptions to be meaningful.

---

### NEW CD-NEW-01 — ThinkingFaculty.deliberate() fallback-to-primary does not set degraded=True
**Classification:** CODE DEFICIT
**Severity:** Medium — orchestrator cannot determine artifact quality source

**Evidence:**
`runtime/thinking_faculty.py` lines 205–213:
```python
if endpoint:
    model = "lfm25-thinking"
else:
    logger.debug("[ThinkingFaculty] Thinking server absent — routing to primary")
    endpoint = self._topology.primary_endpoint()
    model = "qwen3"
```

When thinking server is absent (either not started, or policy denied activation), the faculty silently routes to the primary Qwen3 server. The `ThinkingArtifact` returned has `degraded=False` (the default). `degraded=True` is only set in the `except` block on HTTP failure.

**Contrast with CreativityService:**
`runtime/creativity_service.py` lines 293–295: if endpoint is None → `return CreativityArtifact(degraded=True)`. Creativity correctly marks absence as degraded. No fallback to primary.

**Impact:** The orchestrator (`runtime/orchestrator.py`) consumes the `ThinkingArtifact`. Its docstring says "QWEN is responsible for interpreting the artifact and deciding what (if anything) to include in the user-facing response." But QWEN cannot know whether the artifact reflects dedicated reasoning-model output (LFM2.5-Thinking, temperature=0.3 on a structured system prompt) versus a Qwen3 primary-model response to the same prompt. The quality difference is significant. Confidence scores embedded in the artifact may be systematically miscalibrated when coming from primary.

**Verdict:** `ThinkingFaculty.deliberate()` must set `artifact.degraded = True` when routing to primary instead of the thinking server. The `degraded` field exists precisely for this signal.

---

### NEW CD-NEW-02 — normalize_activation_config baseline_default hardcodes vision as default baseline regardless of enabled status
**Classification:** CODE DEFICIT
**Severity:** Low — latent defect, only fires when config lacks explicit server_activation.baseline_roles

**Evidence:**
`runtime/server_activation.py` lines 164–165:
```python
baseline_default = [role for role in ("primary", "tool", "vision") if role in servers]
auxiliary_default = [role for role in ("thinking", "creativity") if role in servers]
```

This default is computed by checking `if role in servers` — presence in the servers dict — without checking `servers[role].get("enabled", False)`. A config that defines vision in the servers section (even with `enabled=false`) and omits `server_activation.baseline_roles` will have vision included in the computed baseline_default.

**When it fires:** Only when `server_activation.baseline_roles` is absent from the config AND vision is defined but disabled. The canonical `config.json` has `baseline_roles` explicitly set, so this default is not reached in normal operation. Latent risk exists in minimal or custom configs.

**Same issue in `runtime/startup_health.py` line 11:**
```python
baseline = activation.get("baseline_roles") or ["primary", "tool", "vision"]
```
Hardcoded fallback includes vision regardless of whether vision is enabled in the config.

---

### NEW CD-NEW-03 — OnDemandServerManager.start_idle_loop() returns a Task with no internal reference retention
**Classification:** CODE DEFICIT
**Severity:** Low — latent silent failure risk

**Evidence:**
`runtime/on_demand.py` lines 287–290:
```python
def start_idle_loop(self) -> asyncio.Task:
    task = asyncio.create_task(self._idle_loop())
    logger.info("[Elastic] Idle loop started for roles: %s", ...)
    return task
```

The manager does not store `task` as an instance attribute. The caller (`webui/app_runtime.py` or equivalent startup code) must retain the reference. If the caller stores it in a local variable that goes out of scope, Python's garbage collector may cancel the task. When this happens:
- On-demand servers (thinking, creativity) will no longer be stopped when idle
- Processes accumulate; VRAM/RAM leak over a long session
- No error is logged — the loop silently stops

**Verdict:** `self._idle_task = asyncio.create_task(self._idle_loop())` should be stored internally. The caller can still retrieve it via a property if needed. Should not rely on the caller to retain the reference.

---

### NEW CM-NEW-01 — Full vs standard bundle distinction is expressed nowhere in the system; operators have no mechanism to choose between them meaningfully
**Classification:** CONTRACT MISMATCH
**Severity:** Medium

**Evidence:**
- `launch_catalog.py`: standard and full both = `("primary", "tool", "vision")`, identical roles
- Both `start-standard.bat` and `start-full.bat` use `config.json`
- `config.json` has both thinking and creativity as auxiliary_roles → both are on-demand in either launch
- There is no config-level switch, no batch-file argument, and no catalog metadata that distinguishes what "full" provides over "standard" in the current deployed state

The operator who reads documentation saying "full adds creativity" and then runs `start-full.bat` gets exactly what `start-standard.bat` provides. No extra capability is unlocked.

**Contract that is broken:** The `LaunchBundle` dataclass has `legacy_tier: str = "first_class"` and rich `description` fields, implying bundles are meaningfully distinct. They are not, in the current state.

**Verdict:** This is simultaneously a code problem (catalog makes identical bundles), a batch-file problem (both use the same config), and a documentation problem (docs describe different behaviors). FIX-01 in the original audit (correcting bundle compositions) should be revised: the fix is not to change the `roles` tuple but to make standard and full launch with different configs or different on-demand availability policies.

---

## SECTION D — FINDINGS CONFIRMED UNCHANGED

The following original findings are **not affected** by the architecture clarification and remain in full force:

- **CD-04** — admin_tool_registry() reports only 11 legacy tools, not full toolpack registry *(still valid)*
- **CD-05** — admin_shadow_databases() default db_path fallback uses `entity_app_state.db` *(still valid)*
- **CD-06** — launch_profile.py argparse description says "hardened" *(still valid)*
- **CD-08** — verify.py does not check psutil *(still valid)*
- **DD-01** — MODELS.md omits multi-GGUF alphabetical selection and warning *(still valid)*
- **DD-02** — POWER_USER_GUIDE minimal bundle: "Main model + Tool extraction" is wrong; minimal = primary only *(still valid)*
- **DD-04** — INSTALL.md Step 4 launch path conflicts with README and QUICK_START *(still valid)*
- **DD-05** — INSTALL.md calls start-standard.bat a "hardened default" *(still valid)*
- **DD-06** — POWER_USER_GUIDE autonomy_defaults description conflicts with profile configs *(still valid)*
- **BD-02** — release_gate.py Gate 7 structurally fails against canonical config.json *(still valid)*
- **BD-03** — "Hardened" label inconsistency *(still valid)*
- **CM-01** — admin_enable_tool/disable_tool dual state tracking with ToolRegistry *(still valid)*
- **CM-02** — Two parallel tool API systems (legacy dispatcher vs. modern toolpack registry) *(still valid)*
- **CM-03** — verify.py port check reports blocker as warning *(still valid)*
- **CM-05** — Route conflict risk for tools/pending path *(still valid)*

---

## SECTION E — UPDATED FIX PLAN

Original FIX-01 through FIX-14 from the original audit are superseded by this revised ordering where affected.

---

### P0 — Critical (Fix before any release)

**FIX-01-REVISED** (addresses REVISED CD-02, NEW CM-NEW-01):
Establish the standard/full distinction at the batch-file level. Recommended: `start-full.bat` should pass `--config "%ROOT%\configs\profiles\config.full.json"` (or a canonical full config) rather than `config.json`. Alternatively, expose a `--on-demand-roles` override in launch_profile so the catalog can express which on-demand roles are made available per bundle. **Do not** change the catalog `roles` tuple — that correctly lists resident roles only.

**FIX-02-REVISED** (addresses CD-03 / REVISED; now LOW):
Defer — the profile config baseline_roles stale data is low risk. Fix opportunistically when profile configs are next updated.

---

### P1 — High (Fix before operator-facing deployment)

**FIX-03-NEW** (addresses DD-NEW-01, DD-NEW-02, DD-NEW-03):
Rewrite `docs/PROFILES.md` and `docs/POWER_USER_GUIDE.md` bundle descriptions entirely. Required content:
- Explain the resident/elastic distinction (always running vs. policy-gated on-demand)
- Standard resident set: primary + tool + vision. Elastic on-demand: thinking
- Full resident set: primary + tool + vision. Elastic on-demand: thinking + creativity
- Minimal: primary only (no tool, vision, thinking, or creativity)
- Add explicit statement that thinking and creativity are NOT started at boot; they spin up when the executive requests them and stop when idle

**FIX-04-NEW** (addresses CD-NEW-01 — High):
In `runtime/thinking_faculty.py`, `deliberate()`: when routing to primary instead of the thinking server, set `artifact.degraded = True` before returning. The existing `degraded` field is the correct mechanism. Consider also adding a `source` field to `ThinkingArtifact` to allow the orchestrator to log which backend was used.

**FIX-05** (formerly FIX-03; addresses DD-01):
Update `docs/MODELS.md` to document alphabetical GGUF selection and the warning emitted when multiple files exist — unchanged, still required.

**FIX-06** (formerly FIX-04; addresses DD-02):
Update `docs/POWER_USER_GUIDE.md` minimal bundle description to "Main model only. No tool extraction, vision, thinking, or creativity" — unchanged, still required.

**FIX-07** (formerly FIX-05; addresses CD-04):
Update `admin_tool_registry()` diagnostic to consult `app_state.tool_registry` for modern toolpack tools — unchanged, still required.

**FIX-08** (formerly FIX-06; addresses BD-02):
Resolve Gate 7 / canonical config conflict — unchanged, still required.

---

### P2 — Medium (Fix for operational correctness)

**FIX-09-NEW** (addresses CD-NEW-03):
In `runtime/on_demand.py`: change `start_idle_loop()` to store the task as `self._idle_task = asyncio.create_task(...)` and return `self._idle_task`. Prevents silent idle-loop cancellation by garbage collector.

**FIX-10** (formerly FIX-07; addresses CM-01):
Read and confirm admin_enable_tool/disable_tool synchronization with ToolRegistry — unchanged.

**FIX-11** (formerly FIX-08; addresses CD-05):
Fix `admin_shadow_databases()` default fallback from `entity_app_state.db` to `entity_state.db` — unchanged.

**FIX-12** (formerly FIX-09; addresses CD-08):
Add psutil to `verify.py` REQUIRED_PACKAGES — unchanged.

**FIX-13** (formerly FIX-10; addresses DD-04, DD-05):
Update INSTALL.md launch path and remove "hardened default" label — unchanged.

---

### P3 — Low (Polish)

**FIX-14-NEW** (addresses CD-NEW-02):
In `normalize_activation_config()`: change `baseline_default` computation to check `enabled=true` before including a role:
```python
baseline_default = [r for r in ("primary", "tool", "vision") if servers.get(r, {}).get("enabled", False)]
```
Same fix in `startup_health.py` hardcoded fallback.

**FIX-15** (formerly FIX-11; addresses CD-06): Remove "hardened" from launch_profile.py argparse description — unchanged.

**FIX-16** (formerly FIX-12; addresses BD-03): Audit all uses of "hardened" across docs and code — unchanged.

**FIX-17** (formerly FIX-13; addresses CM-03): Promote port-occupied to failure in verify.py — unchanged.

**FIX-18** (formerly FIX-14; addresses CM-02): Document legacy dispatcher vs. toolpack registry relationship — unchanged.

---

## SECTION F — UPDATED UNVERIFIED AREAS

**UV-01 — RESOLVED (Answered):** ServerActivationManager baseline_roles vs. enabled flags — both reviewed. `enabled=false` wins in `boot.py`; baseline_roles is metadata only. Not a functional issue in live deploys.

**UV-02 — STILL OPEN:** Whether admin_enable_tool/disable_tool call `tool_registry.set_enabled()`.

**UV-03 — STILL OPEN:** Exact toolpack tool count (80+ unverified from source).

**UV-04 — CONFIRMED:** No `config.minimal.json` exists in `configs/profiles/`. Minimal bundle uses canonical config.json defaults with only primary started (minimal catalog roles = `("primary",)`).

**UV-05, UV-06 — STILL OPEN:** Credential file git history check.

**UV-07 — CONFIRMED:** Vision bundle `roles = ("vision",)` only — verified from launch_catalog.py line 80. Dedicated vision helper, additive to the normal backend.

**UV-08 — STILL OPEN:** Tool confirmation system integration with ToolRegistry.

**UV-09 — NEW:** Whether `start_idle_loop()` is called in app_runtime.py and whether the returned Task is retained. If the task is discarded (stored in a local variable), idle teardown is silently broken. Read `webui/app_runtime.py` startup_event to confirm.

**UV-10 — NEW:** Whether `ThinkingFaculty` is ever called outside `runtime/orchestrator.py`. The docstring enforces "instantiated ONLY by runtime.orchestrator" and "Background engines MUST NOT import this module directly." This is a governance contract enforced only by documentation, not by Python's import system. Whether any background engine violates this contract is unverified.

---

*End of Addendum — 2026-03-23*
