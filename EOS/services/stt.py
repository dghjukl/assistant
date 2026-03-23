"""
EOS — Speech-to-Text Service
Uses faster-whisper for CPU inference.
Config is passed as a dict (cfg['stt']).
"""
from __future__ import annotations

import asyncio
import queue
import threading
from pathlib import Path

try:
    import numpy as np
except ImportError:
    np = None

try:
    import sounddevice as sd
except ImportError:
    sd = None

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

STT_AVAILABLE = all(dep is not None for dep in (np, sd, WhisperModel))
STT_IMPORT_ERROR = None if STT_AVAILABLE else "Missing optional STT dependencies: numpy, sounddevice, faster_whisper"

_model: WhisperModel | None = None
_model_cfg: dict = {}


def _require_stt_dependencies() -> None:
    if STT_AVAILABLE:
        return
    raise RuntimeError(STT_IMPORT_ERROR or "Speech-to-text dependencies are unavailable")


def _get_model(cfg: dict) -> WhisperModel:
    _require_stt_dependencies()
    global _model, _model_cfg
    stt_cfg = cfg.get("stt", {})
    if _model is None or stt_cfg != _model_cfg:
        print("[STT] Loading Whisper model...")
        _model = WhisperModel(
            stt_cfg.get("fw_model",   "small.en"),
            device=stt_cfg.get("fw_device",  "cpu"),
            compute_type=stt_cfg.get("fw_compute", "int8"),
        )
        _model_cfg = stt_cfg
        print("[STT] Model ready.")
    return _model


def transcribe_array(audio: "np.ndarray", cfg: dict) -> str:
    """Transcribe a float32 audio array. Returns transcript string."""
    _require_stt_dependencies()
    model = _get_model(cfg)
    stt_cfg = cfg.get("stt", {})
    segments, _ = model.transcribe(
        audio,
        language=stt_cfg.get("language", "en"),
        beam_size=5,
        vad_filter=True,
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


class MicListener:
    """
    Continuously listens to the microphone.
    Accumulates audio until silence, then calls on_transcript(text).
    """

    def __init__(self, on_transcript, cfg: dict):
        self.on_transcript = on_transcript
        self._cfg          = cfg
        self._audio_q      = queue.Queue()
        self._stop_event   = threading.Event()
        self._thread: threading.Thread | None = None

        stt_cfg = cfg.get("stt", {})
        self.sample_rate     = stt_cfg.get("sample_rate", 16000)
        self.chunk_samples   = int(self.sample_rate * stt_cfg.get("chunk_ms", 30) / 1000)
        self.silence_chunks  = int(stt_cfg.get("silence_ms", 800) / stt_cfg.get("chunk_ms", 30))
        self.silence_threshold = 300  # RMS amplitude below this = silence

    def _sd_callback(self, indata, frames, time_info, status):
        self._audio_q.put(indata.copy())

    def _process_loop(self):
        buffer: list = []
        silence_count = 0
        speaking = False

        while not self._stop_event.is_set():
            try:
                chunk = self._audio_q.get(timeout=0.1)
            except queue.Empty:
                continue

            rms = np.sqrt(np.mean(chunk.astype(np.float32) ** 2))

            if rms > self.silence_threshold:
                speaking = True
                silence_count = 0
                buffer.append(chunk)
            elif speaking:
                buffer.append(chunk)
                silence_count += 1
                if silence_count >= self.silence_chunks:
                    audio = (
                        np.concatenate(buffer, axis=0)
                        .flatten()
                        .astype(np.float32) / 32768.0
                    )
                    buffer = []
                    silence_count = 0
                    speaking = False
                    try:
                        text = transcribe_array(audio, self._cfg)
                        if text:
                            self.on_transcript(text)
                    except Exception as exc:
                        print(f"[STT] Transcription error: {exc}")

    def start(self):
        _require_stt_dependencies()
        _get_model(self._cfg)  # pre-load
        self._thread = threading.Thread(target=self._process_loop, daemon=True)
        self._thread.start()
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self._cfg.get("stt", {}).get("channels", 1),
            dtype="int16",
            blocksize=self.chunk_samples,
            callback=self._sd_callback,
        )
        self._stream.start()
        print("[STT] Microphone listener started.")

    def stop(self):
        self._stop_event.set()
        if hasattr(self, "_stream"):
            self._stream.stop()
            self._stream.close()
        if self._thread:
            self._thread.join(timeout=2)
        print("[STT] Microphone listener stopped.")
