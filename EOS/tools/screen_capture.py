"""Tool: Screen Capture — captures screen and routes to vision service."""
from __future__ import annotations
from typing import TYPE_CHECKING
from services.vision import describe_screen, capture_screen

if TYPE_CHECKING:
    from runtime.topology import RuntimeTopology


async def get_screen_description(prompt: str | None, topology: "RuntimeTopology") -> str:
    return await describe_screen(topology, prompt)


async def save_screenshot(path: str = "screenshot.png") -> str:
    img = capture_screen()
    img.save(path)
    return f"Screenshot saved to: {path}"
