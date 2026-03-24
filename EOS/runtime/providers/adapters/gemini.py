"""
EOS — Gemini Provider Adapter
================================
Adapter for the Google Gemini GenerateContent API via direct HTTP (no SDK).

Endpoint: https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
Secret key name in SecretsManager: "gemini_api_key"
Auth: API key passed as ?key= query parameter

Cost tier: 3 (cheap) — Gemini Flash models are aggressively priced
Quality tier: 1 (premium) — Gemini 2.0 Flash matches GPT-4o class on most tasks

Supported models (non-exhaustive):
  gemini-2.0-flash, gemini-2.0-flash-lite, gemini-1.5-pro, gemini-1.5-flash

The default model is configurable via config.json:
  external_inference.gemini.model_id

Gemini API format differences from OpenAI:
  - Messages are in "contents": [{"role": ..., "parts": [{"text": ...}]}]
  - "assistant" role is "model" in Gemini API
  - System instruction is a separate top-level field
  - Response text is in candidates[0].content.parts[0].text
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import httpx

from runtime.providers.base import BaseProvider, ProviderCapabilities, ProviderResult

logger = logging.getLogger("eos.providers.gemini")

_GEMINI_API_BASE = "https://generativelanguage.googleapis.com"
_DEFAULT_MODEL   = "gemini-2.0-flash"

# Secret key name used in EOS SecretsManager
SECRET_KEY = "gemini_api_key"


class GeminiAdapter(BaseProvider):
    """
    Adapter for the Google Gemini GenerateContent API.

    Normalises OpenAI-format messages to Gemini's contents format internally.
    """

    def __init__(
        self,
        model_id:    str   = _DEFAULT_MODEL,
        timeout_sec: float = 30.0,
        max_retries: int   = 1,
    ) -> None:
        self.model_id    = model_id
        self.timeout_sec = timeout_sec
        self.max_retries = max(1, max_retries)

    # ── BaseProvider interface ────────────────────────────────────────────────

    @property
    def provider_id(self) -> str:
        return "gemini"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id              = "gemini",
            supports_tool_calling    = True,
            supports_structured_json = True,
            supports_streaming       = True,
            supports_vision          = True,
            context_size_class       = "xl",
            is_local                 = False,
            quality_tier             = 1,   # premium (Flash class)
            cost_tier                = 3,   # cheap
            default_model            = self.model_id,
        )

    def with_model(self, model_id: str) -> "GeminiAdapter":
        return GeminiAdapter(
            model_id=model_id,
            timeout_sec=self.timeout_sec,
            max_retries=self.max_retries,
        )

    def complete(
        self,
        messages: List[Dict],
        *,
        api_key:     str,
        max_tokens:  int   = 512,
        temperature: float = 0.7,
    ) -> ProviderResult:
        if not api_key:
            return ProviderResult(
                ok=False, provider="gemini",
                error="No API key provided", error_code="no_api_key",
            )

        system_instruction, contents = _convert_messages(messages)

        url  = (
            f"{_GEMINI_API_BASE}/v1beta/models/{self.model_id}"
            f":generateContent?key={api_key}"
        )
        body: dict = {
            "contents":           contents,
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature":     temperature,
            },
        }
        if system_instruction:
            body["system_instruction"] = {
                "parts": [{"text": system_instruction}]
            }

        t0         = time.monotonic()
        last_error = ""
        last_code  = "unknown"

        for attempt in range(self.max_retries):
            try:
                resp       = httpx.post(url, json=body, timeout=self.timeout_sec)
                latency_ms = int((time.monotonic() - t0) * 1000)

                if resp.status_code == 400:
                    try:
                        detail = resp.json().get("error", {}).get("message", "Bad request")
                    except Exception:
                        detail = resp.text[:200]
                    return ProviderResult(
                        ok=False, provider="gemini",
                        error=f"Bad request: {detail}", error_code="bad_request",
                        latency_ms=latency_ms,
                    )

                if resp.status_code in (401, 403):
                    return ProviderResult(
                        ok=False, provider="gemini",
                        error="Invalid or unauthorized API key",
                        error_code="invalid_key", latency_ms=latency_ms,
                    )

                if resp.status_code == 429:
                    logger.warning("[gemini_adapter] Rate limited (attempt %d/%d)",
                                   attempt + 1, self.max_retries)
                    last_error = "Rate limited by Gemini"
                    last_code  = "rate_limit"
                    time.sleep(2.0)
                    continue

                if resp.status_code in (500, 503):
                    last_error = f"Gemini service error: HTTP {resp.status_code}"
                    last_code  = "service_unavailable"
                    continue

                if not resp.is_success:
                    try:
                        detail = resp.json().get("error", {}).get("message", resp.text[:200])
                    except Exception:
                        detail = resp.text[:200]
                    return ProviderResult(
                        ok=False, provider="gemini",
                        error=f"HTTP {resp.status_code}: {detail}",
                        error_code="http_error", latency_ms=latency_ms,
                    )

                data    = resp.json()
                content = _extract_content(data)
                t_in    = data.get("usageMetadata", {}).get("promptTokenCount")
                t_out   = data.get("usageMetadata", {}).get("candidatesTokenCount")
                return ProviderResult(
                    ok=True, content=content,
                    model_id=self.model_id, provider="gemini",
                    tokens_input=t_in, tokens_output=t_out,
                    latency_ms=latency_ms, raw_response=data,
                )

            except httpx.TimeoutException:
                latency_ms = int((time.monotonic() - t0) * 1000)
                logger.warning("[gemini_adapter] Timeout (attempt %d)", attempt + 1)
                last_error = f"Request timed out after {self.timeout_sec}s"
                last_code  = "timeout"

            except httpx.ConnectError as exc:
                last_error = f"Connection error: {exc}"
                last_code  = "connect_error"

            except Exception as exc:
                logger.exception("[gemini_adapter] Unexpected error on attempt %d", attempt + 1)
                last_error = str(exc)
                last_code  = "unexpected_error"

        return ProviderResult(
            ok=False, provider="gemini",
            error=last_error, error_code=last_code,
        )

    def test_connection(self, api_key: str) -> ProviderResult:
        """Send a minimal completion to validate the key and model."""
        return self.complete(
            [{"role": "user", "content": "Reply with exactly the word: ok"}],
            api_key=api_key,
            max_tokens=10,
            temperature=0.0,
        )


# ── Format helpers ────────────────────────────────────────────────────────────


def _convert_messages(messages: List[Dict]):
    """
    Convert OpenAI-format messages to Gemini contents format.

    Returns (system_instruction: str, contents: list).
    """
    system_instruction = ""
    contents = []

    for m in messages:
        role    = m.get("role", "user")
        content = m.get("content", "")

        if role == "system":
            system_instruction = content
            continue

        # Gemini uses "model" instead of "assistant"
        gemini_role = "model" if role == "assistant" else "user"
        contents.append({
            "role":  gemini_role,
            "parts": [{"text": content}],
        })

    # Gemini requires the conversation to start with a user turn
    if not contents or contents[0]["role"] != "user":
        contents.insert(0, {"role": "user", "parts": [{"text": ""}]})

    return system_instruction, contents


def _extract_content(data: dict) -> str:
    """Extract text from a Gemini GenerateContent response."""
    try:
        parts = data["candidates"][0]["content"]["parts"]
        texts = [p["text"] for p in parts if "text" in p]
        return "".join(texts).strip()
    except (KeyError, IndexError, TypeError):
        return ""
