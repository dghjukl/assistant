"""
EOS — Anthropic Provider Adapter
===================================
Adapter for the Anthropic Messages API via direct HTTP (no SDK).

Endpoint: https://api.anthropic.com/v1/messages
Secret key name in SecretsManager: "anthropic_api_key"
API version header: "2023-06-01" (stable)

Cost tier: 2 (moderate) — Claude 3 Haiku is cheap; Opus is expensive
Quality tier: 1 (premium) — Claude family excels at reasoning and safety

Supported models (non-exhaustive):
  claude-haiku-4-5-20251001 (fast/cheap), claude-sonnet-4-6 (balanced),
  claude-opus-4-6 (most capable)

The default model is configurable via config.json:
  external_inference.anthropic.model_id

Anthropic Messages API differs from OpenAI:
  - system prompt is a top-level field, not a message
  - response is in content[0].text
  - usage is in usage.input_tokens / output_tokens
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import httpx

from runtime.providers.base import BaseProvider, ProviderCapabilities, ProviderResult

logger = logging.getLogger("eos.providers.anthropic")

_ANTHROPIC_API_BASE = "https://api.anthropic.com"
_API_VERSION        = "2023-06-01"
_DEFAULT_MODEL      = "claude-haiku-4-5-20251001"

# Secret key name used in EOS SecretsManager
SECRET_KEY = "anthropic_api_key"


class AnthropicAdapter(BaseProvider):
    """
    Adapter for the Anthropic Messages API.

    The adapter normalises the Anthropic request/response format to match
    the OpenAI-style messages convention used throughout EOS.
    """

    def __init__(
        self,
        model_id:    str   = _DEFAULT_MODEL,
        timeout_sec: float = 60.0,
        max_retries: int   = 1,
    ) -> None:
        self.model_id    = model_id
        self.timeout_sec = timeout_sec
        self.max_retries = max(1, max_retries)

    # ── BaseProvider interface ────────────────────────────────────────────────

    @property
    def provider_id(self) -> str:
        return "anthropic"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id              = "anthropic",
            supports_tool_calling    = True,
            supports_structured_json = True,
            supports_streaming       = True,
            supports_vision          = True,
            context_size_class       = "xl",
            is_local                 = False,
            quality_tier             = 1,   # premium
            cost_tier                = 2,   # moderate (Haiku) to expensive (Opus)
            default_model            = self.model_id,
        )

    def with_model(self, model_id: str) -> "AnthropicAdapter":
        return AnthropicAdapter(
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
                ok=False, provider="anthropic",
                error="No API key provided", error_code="no_api_key",
            )

        # Anthropic expects system prompt as top-level field, not in messages
        system_msg = ""
        user_messages = []
        for m in messages:
            if m.get("role") == "system":
                system_msg = m.get("content", "")
            else:
                user_messages.append(m)

        # Ensure conversation starts with "user" role (Anthropic requirement)
        if not user_messages or user_messages[0].get("role") != "user":
            user_messages.insert(0, {"role": "user", "content": ""})

        url     = f"{_ANTHROPIC_API_BASE}/v1/messages"
        headers = {
            "x-api-key":         api_key,
            "anthropic-version": _API_VERSION,
            "Content-Type":      "application/json",
        }
        body: dict = {
            "model":       self.model_id,
            "messages":    user_messages,
            "max_tokens":  max_tokens,
            "temperature": temperature,
        }
        if system_msg:
            body["system"] = system_msg

        t0         = time.monotonic()
        last_error = ""
        last_code  = "unknown"

        for attempt in range(self.max_retries):
            try:
                resp       = httpx.post(url, headers=headers, json=body,
                                        timeout=self.timeout_sec)
                latency_ms = int((time.monotonic() - t0) * 1000)

                if resp.status_code == 401:
                    return ProviderResult(
                        ok=False, provider="anthropic",
                        error="Invalid or expired API key",
                        error_code="invalid_key", latency_ms=latency_ms,
                    )

                if resp.status_code == 429:
                    logger.warning("[anthropic_adapter] Rate limited (attempt %d/%d)",
                                   attempt + 1, self.max_retries)
                    last_error = "Rate limited by Anthropic"
                    last_code  = "rate_limit"
                    time.sleep(2.0)
                    continue

                if resp.status_code in (500, 502, 503, 529):
                    last_error = f"Anthropic service error: HTTP {resp.status_code}"
                    last_code  = "service_unavailable"
                    continue

                if not resp.is_success:
                    try:
                        detail = resp.json().get("error", {}).get("message", resp.text[:200])
                    except Exception:
                        detail = resp.text[:200]
                    return ProviderResult(
                        ok=False, provider="anthropic",
                        error=f"HTTP {resp.status_code}: {detail}",
                        error_code="http_error", latency_ms=latency_ms,
                    )

                data    = resp.json()
                content = _extract_content(data)
                t_in    = data.get("usage", {}).get("input_tokens")
                t_out   = data.get("usage", {}).get("output_tokens")
                return ProviderResult(
                    ok=True, content=content,
                    model_id=data.get("model", self.model_id),
                    provider="anthropic",
                    tokens_input=t_in, tokens_output=t_out,
                    latency_ms=latency_ms, raw_response=data,
                )

            except httpx.TimeoutException:
                latency_ms = int((time.monotonic() - t0) * 1000)
                logger.warning("[anthropic_adapter] Timeout (attempt %d)", attempt + 1)
                last_error = f"Request timed out after {self.timeout_sec}s"
                last_code  = "timeout"

            except httpx.ConnectError as exc:
                last_error = f"Connection error: {exc}"
                last_code  = "connect_error"

            except Exception as exc:
                logger.exception("[anthropic_adapter] Unexpected error on attempt %d", attempt + 1)
                last_error = str(exc)
                last_code  = "unexpected_error"

        return ProviderResult(
            ok=False, provider="anthropic",
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


# ── Helpers ───────────────────────────────────────────────────────────────────


def _extract_content(data: dict) -> str:
    """Extract text content from an Anthropic Messages API response."""
    try:
        blocks = data.get("content", [])
        texts  = [b["text"] for b in blocks if b.get("type") == "text"]
        return " ".join(texts).strip()
    except Exception:
        return ""
