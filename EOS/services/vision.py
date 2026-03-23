"""
EOS — Vision Service
Captures screen/webcam frames and routes to the vision server.
The vision server produces structured perception output ONLY.
Its output is injected into context as a VisionObservation, never spoken directly.

Per RUNTIME_INVARIANTS: vision is perception, not cognition.
"""
from __future__ import annotations

import base64
import io
from typing import TYPE_CHECKING

import httpx

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import mss
except ImportError:
    mss = None

try:
    import numpy as np
except ImportError:
    np = None

try:
    from PIL import Image
except ImportError:
    Image = None

if TYPE_CHECKING:
    from runtime.topology import RuntimeTopology


VISION_AVAILABLE = all(dep is not None for dep in (cv2, mss, np, Image))
VISION_IMPORT_ERROR = None if VISION_AVAILABLE else "Missing optional vision dependencies: cv2, mss, numpy, pillow"


def _vision_unavailable(reason: str | None = None) -> str:
    return f"[Vision unavailable: {reason or VISION_IMPORT_ERROR or 'missing dependencies'}]"


# ── Frame capture ─────────────────────────────────────────────────────────────

def capture_screen(monitor: int = 1) -> "Image.Image":
    """Capture the primary screen and return a PIL Image."""
    if not VISION_AVAILABLE:
        raise RuntimeError(VISION_IMPORT_ERROR or "Vision dependencies are unavailable")
    with mss.mss() as sct:
        monitors = sct.monitors
        if monitor >= len(monitors):
            monitor = 1
        raw = sct.grab(monitors[monitor])
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    return img


def capture_webcam(device: int = 0) -> "Image.Image | None":
    """Grab a single frame from the webcam."""
    if not VISION_AVAILABLE:
        return None
    cap = cv2.VideoCapture(device)
    if not cap.isOpened():
        return None
    ret, frame = cap.read()
    cap.release()
    if not ret:
        return None
    return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))


def image_to_base64(img: "Image.Image", max_size: tuple[int, int] = (1024, 768)) -> str:
    """Resize and base64-encode as JPEG."""
    if Image is None:
        raise RuntimeError(VISION_IMPORT_ERROR or "Vision dependencies are unavailable")
    img.thumbnail(max_size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# ── Vision API call ───────────────────────────────────────────────────────────

async def describe(
    image: Image.Image,
    topology: "RuntimeTopology",
    prompt: str = "Describe what you see in this image clearly and specifically.",
    timeout: int = 60,
) -> str:
    """
    Send an image to the vision server and return structured perception output.
    Checks vision_available via topology before making the call.
    Output is a description to be injected as context — not conversational.
    """
    if not VISION_AVAILABLE:
        return _vision_unavailable()
    if not topology.vision_available:
        return "[Vision not available in this deployment]"

    routing = topology.route_image_input()
    if routing["route"] in ("reject", "error"):
        return f"[{routing.get('reason', 'Vision unavailable')}]"

    endpoint = routing["endpoint"]
    b64      = image_to_base64(image)

    payload = {
        "model": "vision",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type":      "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "max_tokens":  512,
        "temperature": 0.2,   # low temperature: perception should be factual
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(
                f"{endpoint}/v1/chat/completions",
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except httpx.ConnectError:
            return "[Vision server not responding]"
        except Exception as exc:
            return f"[Vision error: {exc}]"


async def describe_screen(
    topology: "RuntimeTopology",
    prompt: str | None = None,
) -> str:
    if not VISION_AVAILABLE:
        return _vision_unavailable()
    img = capture_screen()
    return await describe(
        img, topology,
        prompt or "What is on the screen? Describe all visible content, text, and applications."
    )


async def describe_webcam(
    topology: "RuntimeTopology",
    prompt: str | None = None,
) -> str:
    if not VISION_AVAILABLE:
        return _vision_unavailable()
    img = capture_webcam()
    if img is None:
        return "[No webcam detected or camera unavailable]"
    return await describe(
        img, topology,
        prompt or "Describe what you see through the webcam."
    )


async def analyze_image_file(
    path: str,
    topology: "RuntimeTopology",
    prompt: str = "Describe this image.",
) -> str:
    if not VISION_AVAILABLE or Image is None:
        return _vision_unavailable()
    img = Image.open(path).convert("RGB")
    return await describe(img, topology, prompt)
