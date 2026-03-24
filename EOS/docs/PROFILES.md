# EOS — Startup Bundles and Entry-Point Hierarchy

EOS uses a single canonical config file: `config.json`. There are no separate live profile configs.

The files under `configs/profiles/` are shipped reference variants. `config.base.json` is the shared baseline, and release-surface changes must be synchronized across every profile variant before release.

The canonical launch hierarchy is:

1. **Recommended Windows launcher:** `launchers\Launch EOS.bat`
2. **Recommended non-interactive backend bundle:** `launchers\start-standard.bat`
3. **Recommended runtime bootstrap:** `start-eos.bat`
4. **Fallback diagnostics:** `status-eos.bat`
5. **Advanced control:** per-server launchers or direct `python eos.py`

The authoritative launch-role metadata, bundle composition, and legacy-surface status now live in `runtime/launch_catalog.py`. Batch files and the launcher UI should reflect that catalog rather than redefining launch policy independently.

Missing backends degrade gracefully — they do not prevent startup.

For capability governance (autonomy, computer use, workspace permissions, etc.) see the admin panel at **http://127.0.0.1:7860/admin → Control & Permissions → Capabilities**, or see [USER_GUIDE.md](USER_GUIDE.md). These controls are runtime supervision and safety backstops, not the primary product definition.

---

## Recommended default

Use:

- `launchers\Launch EOS.bat` for the easiest Windows path
- or `launchers\start-standard.bat` for the non-interactive default
- `start-eos.bat` only if you launched backends yourself

The Windows launcher detects whether the machine should run the recommended tier, a CPU-first compatibility tier, or a fuller installed stack. Degraded modes are presented as supported profiles, not implicit failure states.

---

## When to deviate

| Need | Use |
|---|---|
| Lowest-resource practical stack | `launchers\start-minimal.bat` |
| Extra creativity support | `launchers\start-full.bat` |
| Vision support | add `launchers\start-vision-gpu.bat` |
| Exact hardware/backend control | per-server launchers |
| Discovery without starting WebUI | `status-eos.bat` or `python eos.py --status` |
| Direct CLI bootstrap | `python eos.py` |

---

## Bundle overview

| Bundle | Launchers | Resident at boot | On-demand (elastic) |
|---|---|---|---|
| Minimal | `launchers\start-minimal.bat` | main only | — |
| Standard | `launchers\start-standard.bat` | main · tools · vision (if enabled) | thinking · creativity |
| Full | `launchers\start-full.bat` | main · tools · thinking · creativity · vision (if enabled) | — |

**Resident** servers are started at boot and kept running.
**On-demand** servers are not started at boot. The executive starts them when a task requires it and resources permit, then stops them after an idle timeout.

Vision is part of the standard and full resident baseline when enabled in `config.json`. Use `launchers\start-vision-gpu.bat` to add vision as a standalone GPU helper without launching a full bundle.

---

## Per-server launchers

| Role | CPU | GPU |
|---|---|---|
| Main | `launchers\start-main-cpu.bat` | `launchers\start-main-gpu.bat` |
| Tools | `launchers\start-tools-cpu.bat` | `launchers\start-tools-gpu.bat` |
| Thinking | `launchers\start-thinking-cpu.bat` | `launchers\start-thinking-gpu.bat` |
| Creativity | `launchers\start-creativity-cpu.bat` | `launchers\start-creativity-gpu.bat` |
| Vision | — | `launchers\start-vision-gpu.bat` |

---

## Expected capability behavior

| Missing backend | Runtime effect |
|---|---|
| Main | chat unavailable |
| Tools | tool extraction unavailable; entity responds without structured tool calls |
| Thinking | executive routes deliberation to the main model instead; reasoning quality may be lower |
| Creativity | creativity subsystem skipped; no fallback, degrades cleanly |
| Vision | vision unavailable |
| STT | voice degraded/unavailable |
| TTS | voice degraded/unavailable |

Note: in standard mode, thinking and creativity may show as `degraded` at boot with detail `managed on-demand; currently inactive`. This is expected, not a fault.


---

## Legacy note

Older launcher names still exist for compatibility, but they are deprecated wrappers around the launchers above. Treat `launchers\legacy\` as advanced/compatibility-only surface area, not as a second profile system.
