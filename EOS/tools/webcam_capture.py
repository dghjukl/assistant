"""Tool: Webcam Capture — grabs webcam frame and routes to vision service."""
from __future__ import annotations
from typing import TYPE_CHECKING
from services.vision import describe_webcam, capture_webcam

if TYPE_CHECKING:
    from runtime.topology import RuntimeTopology


async def get_webcam_description(prompt: str | None, topology: "RuntimeTopology") -> str:
    return await describe_webcam(topology, prompt)


async def save_webcam_frame(path: str = "webcam_frame.jpg") -> str:
    img = capture_webcam()
    if img is None:
        return "No webcam available"
    img.save(path)
    return f"Webcam frame saved to: {path}"
