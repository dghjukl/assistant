"""
EOS — Text-to-Speech Service
Uses Piper via subprocess: text → stdin, raw PCM → sounddevice playback.
Config is passed as a dict (cfg['tts']).
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd

_speak_lock = threading.Lock()
_is_speaking = False


def speak(text: str, cfg: dict) -> None:
    """Synthesize text via Piper and play through default audio output. Blocking."""
    global _is_speaking

    if not text or not text.strip():
        return

    tts_cfg    = cfg.get("tts", {})
    piper_exe  = Path(tts_cfg.get("binary", "Piper/piper/piper.exe"))
    model_path = Path(tts_cfg.get("model_path", "models/tts/en_US-amy-medium.onnx"))

    if not piper_exe.is_file():
        print(f"[TTS] Piper not found at {piper_exe}")
        return

    cmd = [
        str(piper_exe),
        "--model", str(model_path),
        "--output-raw",
    ]

    with _speak_lock:
        _is_speaking = True
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            raw_audio, _ = proc.communicate(input=text.encode("utf-8"))
            if raw_audio:
                sample_rate = tts_cfg.get("sample_rate", 22050)
                audio = (
                    np.frombuffer(raw_audio, dtype=np.int16)
                    .astype(np.float32) / 32768.0
                )
                sd.play(audio, samplerate=sample_rate, blocking=True)
        except Exception as exc:
            print(f"[TTS] Error: {exc}")
        finally:
            _is_speaking = False


def is_speaking() -> bool:
    return _is_speaking


async def speak_async(text: str, cfg: dict) -> None:
    """Non-blocking async wrapper."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, speak, text, cfg)


def stop() -> None:
    """Interrupt current playback."""
    sd.stop()
    global _is_speaking
    _is_speaking = False


def _split_sentences(text: str) -> list[str]:
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s.strip() for s in sentences if s.strip()]


async def speak_streaming(text: str, cfg: dict) -> None:
    """Speak sentence-by-sentence for lower perceived latency on long responses."""
    for sentence in _split_sentences(text):
        await speak_async(sentence, cfg)
