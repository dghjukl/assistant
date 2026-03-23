# Legacy launchers

These scripts are **deprecated compatibility shims**.

They still exist so older shortcuts and operator habits do not break, but they are no longer the place where launch policy should evolve.

## First-class launch surfaces

Use these instead:

- `launchers\Launch EOS.bat` for the Windows launcher UI
- `launchers\start-standard.bat` for the recommended non-interactive backend bundle
- `launchers\start-minimal.bat` / `launchers\start-full.bat` for explicit bundle choices
- `launchers\start-vision-gpu.bat` only as an additive advanced helper
- `start-eos.bat` only after the desired backends are already running

## Source of truth

Launch role metadata, bundle composition, and legacy-surface status now live in `runtime/launch_catalog.py`.
Legacy scripts should only delegate to those first-class entry points.
