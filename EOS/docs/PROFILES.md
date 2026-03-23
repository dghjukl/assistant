# EOS — Startup Bundles and Capability Expectations

EOS uses a single canonical config file: `config.json`. There are no separate profile configs.

Instead, you choose which backends to launch. The capabilities that are available at runtime depend on which backends are running. Missing backends degrade gracefully — they do not prevent startup.

For capability governance (autonomy, computer use, workspace permissions, etc.) see the admin panel at **http://127.0.0.1:7860/admin → Control & Permissions → Capabilities**, or see [USER_GUIDE.md](USER_GUIDE.md). These controls are intended as runtime supervision and safety backstops, not as the primary definition of the relationship.

---

## Bundle overview

| Bundle | Launchers | Backends started |
|---|---|---|
| Minimal | `launchers\start-minimal.bat` | main + tools |
| Standard | `launchers\start-standard.bat` | main + tools + thinking |
| Full | `launchers\start-full.bat` | main + tools + thinking + creativity |

Vision is additive: start `launchers\start-vision-gpu.bat` alongside whichever bundle you want.

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
| Tools | tools degraded, fallback to main |
| Thinking | reasoning degraded, fallback to main |
| Creativity | creativity degraded |
| Vision | vision unavailable |
| STT | voice degraded/unavailable |
| TTS | voice degraded/unavailable |

---

## Recommended default

Use:

- `launchers\start-standard.bat`
- `start-eos.bat`

That gives the main model, tool helper, and thinking helper with runtime discovery and graceful fallback.
