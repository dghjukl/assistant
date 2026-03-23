from __future__ import annotations

from webui import app_runtime


async def on_startup() -> None:
    await app_runtime.startup_event()


async def on_shutdown() -> None:
    await app_runtime.shutdown_event()
