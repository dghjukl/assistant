# EOS — Model Placement Guide

All model slots use **directory-based discovery**. Place any compatible `.gguf` file in the correct directory — the filename does not matter. EOS will find it automatically.

The only exceptions are the STT and TTS models, which use fixed filenames because they are loaded by external binaries (faster-whisper and Piper).

---

## Model Directories

### Primary Model — `models/primary/`

**Required.** EOS will not start without this.

This is the main language model — the one that reasons, holds identity, and produces all responses.

- Place any instruction-tuned GGUF here
- Default: `Qwen3-8B-Q6_K.gguf` (~6.3 GB) — downloaded by `setup\Setup-Full.ps1`
- Do **not** place mmproj files here
- Minimum recommended: 7B parameter model, Q4_K_M or higher quantization
- GPU layers: 99 (fully GPU by default) — adjust `n_gpu_layers` in the config if VRAM is limited

| Spec | Value |
|---|---|
| Downloaded by | setup\Setup-Full.ps1 |
| Source | https://huggingface.co/Qwen/Qwen3-8B-GGUF |
| File | Qwen3-8B-Q6_K.gguf |
| Size | ~6.3 GB |
| Required for | All profiles |

---

### Tool Model — `models/tool/`

**Optional.** Enables structured function/tool call extraction. Degrades gracefully if absent.

- Place any tool-capable GGUF here
- Default: `LFM2-1.2B-Tool-Q5_K_M.gguf` (~805 MB)
- CPU-only (0 GPU layers)

| Spec | Value |
|---|---|
| Downloaded by | setup\Setup-Full.ps1 |
| Source | https://huggingface.co/LiquidAI/LFM2-1.2B-Tool-GGUF |
| File | LFM2-1.2B-Tool-Q5_K_M.gguf |
| Size | ~805 MB |
| Required for | All profiles (optional, graceful degradation) |

---

### Thinking Model — `models/thinking/`

**Optional.** Enables background deep reasoning dispatched by idle cognition, initiative, reflection, and escalated executive reasoning. When installed, it is treated as an elastic auxiliary backend: not resident by default, started on demand when posture and resources allow.

- Place any instruct GGUF here
- Default: `LFM2.5-1.2B-Thinking-Q5_K_M.gguf` (~805 MB)
- CPU-only (0 GPU layers)

| Spec | Value |
|---|---|
| Downloaded by | setup\Setup-Full.ps1 |
| Source | https://huggingface.co/LiquidAI/LFM2.5-1.2B-Thinking-GGUF |
| File | LFM2.5-1.2B-Thinking-Q5_K_M.gguf |
| Size | ~805 MB |
| Required for | Base+Thinking, Full profiles (optional, graceful degradation) |

---

### Creativity Model — `models/creativity/`

**Optional.** Enables the divergence subsystem — alternate interpretations, reframing, analogies, and ideation. Advisory only; does not make decisions. When installed, it is treated as an elastic auxiliary backend and is only activated on demand when policy and resources allow.

- Place any instruct-tuned GGUF here
- Bring your own model — any compatible instruct GGUF works
- CPU-only (0 GPU layers)
- Recommended: a small-to-mid instruct model (1B–7B) with higher temperature tolerance

| Spec | Value |
|---|---|
| Downloaded by | Not downloaded automatically — bring your own |
| Suggested models | Any instruct GGUF in the 1B–7B range |
| Size | Your choice |
| Required for | Base+Creativity, Full profiles (optional, graceful degradation) |

> **Note:** The creativity slot does not require a specific model. Any instruct GGUF placed in `models/creativity/` will be used. The system controls its behavior through the temperature and sampling parameters in the config's `creativity` section.

---

### Vision Model — `models/vision/`

**Optional (required for Vision Mode only).** Enables screen and image perception. Not used in non-vision profiles.

This slot requires **two files** in the same directory:
1. A main model GGUF (any filename **not** starting with `mmproj`)
2. A multimodal projector GGUF (filename **must** start with `mmproj`)

| Spec | Value |
|---|---|
| Downloaded by | setup\Setup-Full.ps1 |
| Main model source | https://huggingface.co/ggml-org/Qwen2.5-VL-3B-Instruct-GGUF |
| Main model file | Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf (~1.93 GB) |
| Projector source | Same repo |
| Projector file | mmproj-Qwen2.5-VL-3B-Instruct-f16.gguf (~1.34 GB) |
| Total size | ~3.3 GB |
| Required for | Vision Mode only |

---

### STT Model — `models/stt/ggml-small.en-q8_0.bin`

**Optional.** Enables voice input (speech-to-text). Degrades gracefully if absent.

This is the only model with a **fixed filename**. It must be named exactly `ggml-small.en-q8_0.bin`.

| Spec | Value |
|---|---|
| Downloaded by | setup\Setup-Full.ps1 |
| Source | https://huggingface.co/ggerganov/whisper.cpp |
| File | ggml-small.en-q8_0.bin |
| Size | ~253 MB |
| Language | English only |

---

### TTS Model — `models/tts/`

**Optional.** Enables voice output (text-to-speech). Degrades gracefully if absent.

Requires **two files** with fixed names:
- `en_US-amy-medium.onnx` (~63 MB)
- `en_US-amy-medium.onnx.json` (tiny config file)

| Spec | Value |
|---|---|
| Downloaded by | setup\Setup-Full.ps1 |
| Source | https://huggingface.co/rhasspy/piper-voices |
| Voice | Amy (female, US English, medium quality) |
| Sample rate | 22050 Hz |

---

## Summary Table

| Directory | Required | Auto-downloaded | Fixed filename |
|---|---|---|---|
| `models/primary/` | Yes | Yes | No |
| `models/tool/` | No | Yes | No |
| `models/thinking/` | No | Yes | No |
| `models/creativity/` | No | **No — bring your own** | No |
| `models/vision/` | Vision mode only | Yes | No (but mmproj must start with `mmproj`) |
| `models/stt/` | No | Yes | **Yes** |
| `models/tts/` | No | Yes | **Yes** |
