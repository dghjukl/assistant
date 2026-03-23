# EOS — Startup Bundles and Entry-Point Hierarchy

EOS uses a single canonical config file: `config.json`. There are no separate live profile configs.

The canonical launch hierarchy is:

1. **Recommended backend bundle:** `launchers\start-standard.bat`
2. **Recommended runtime bootstrap:** `start-eos.bat`
3. **Fallback diagnostics:** `status-eos.bat`
4. **Advanced control:** per-server launchers or direct `python eos.py`

Missing backends degrade gracefully — they do not prevent startup.

For capability governance (autonomy, computer use, workspace permissions, etc.) see the admin panel at **http://127.0.0.1:7860/admin → Control & Permissions → Capabilities**, or see [USER_GUIDE.md](USER_GUIDE.md). These controls are runtime supervision and safety backstops, not the primary product definition.

---

## Recommended default

Use:

- `launchers\start-standard.bat`
- `start-eos.bat`

That gives the main model, tool helper, and thinking helper with runtime discovery and graceful fallback.

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
