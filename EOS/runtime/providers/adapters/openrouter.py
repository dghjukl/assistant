"""
EOS — OpenRouter Provider Adapter
=====================================
Adapter for OpenRouter — an OpenAI-compatible API gateway that provides
access to open-weight and commercial models through a single endpoint.

Endpoint: https://openrouter.ai/api/v1/chat/completions
Secret key name in SecretsManager: "openrouter_api_key"

OpenRouter is the recommended gateway for open-weight hosted models:
  - Meta Llama family (llama-3.1-8b, llama-3.1-70b, llama-3.3-70b, ...)
  - Mistral family (mistral-7b, mixtral-8x7b, ...)
  - DeepSeek family (deepseek-chat, deepseek-r1, ...)
  - Qwen family (qwen2.5-72b-instruct, ...)
  - And many others, including several free-tier options

Why OpenRouter for open-weight instead of individual provider APIs:
  - Single API key covers all open-weight providers
  - OpenAI-compatible format requires no custom parsing
  - Automatic routing to available providers when one is down
  - Free tier models available at zero cost

Cost tier: 3 (cheap) — many free-tier and sub-cent models available
Quality tier: 2 (standard) — varies by model; top-tier models available

Default model: "meta-llama/llama-3.1-8b-instruct:free" (free tier)
The default model is configurable via config.json:
  external_inference.openrouter.model_id

Additional headers sent per OpenRouter requirements:
  HTTP-Referer: EOS (identifies the application)
  X-Title:      EOS AI System
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import httpx

from runtime.providers.base import BaseProvider, ProviderCapabilities, ProviderResult

logger = logging.getLogger("eos.providers.openrouter")

_OR_API_BASE   = "https://openrouter.ai/api"
_DEFAULT_MODEL = "meta-llama/llama-3.1-8b-instruct:free"
_APP_REFERER   = "https://github.com/dghjukl/assistant"
_APP_TITLE     = "EOS AI System"

# Secret key name used in EOS SecretsManager
SECRET_KEY = "openrouter_api_key"


class OpenRouterAdapter(BaseProvider):
    """
    Adapter for the OpenRouter API gateway.

    The OpenRouter API is OpenAI-compatible, so parsing is identical.
    Additional required headers (HTTP-Referer, X-Title) are sent per
    OpenRouter's API usage policy.
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
        return "openrouter"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id              = "openrouter",
            supports_tool_calling    = True,    # most OR models support it
            supports_structured_json = True,
            supports_streaming       = True,
            supports_vision          = False,   # model-dependent; assume no by default
            context_size_class       = "large",
            is_local                 = False,
            quality_tier             = 2,   # standard (varies widely by model)
            cost_tier                = 3,   # cheap (many free-tier options)
            default_model            = self.model_id,
        )

    def with_model(self, model_id: str) -> "OpenRouterAdapter":
        return OpenRouterAdapter(
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
                ok=False, provider="openrouter",
                error="No API key provided", error_code="no_api_key",
            )

        url     = f"{_OR_API_BASE}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
            "HTTP-Referer":  _APP_REFERER,
            "X-Title":       _APP_TITLE,
        }
        body: dict = {
            "model":       self.model_id,
            "messages":    messages,
            "max_tokens":  max_tokens,
            "temperature": temperature,
        }

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
                        ok=False, provider="openrouter",
                        error="Invalid or expired API key",
                        error_code="invalid_key", latency_ms=latency_ms,
                    )

                if resp.status_code == 429:
                    logger.warning("[openrouter_adapter] Rate limited (attempt %d/%d)",
                                   attempt + 1, self.max_retries)
                    last_error = "Rate limited by OpenRouter"
                    last_code  = "rate_limit"
                    time.sleep(2.0)
                    continue

                if resp.status_code in (500, 502, 503):
                    last_error = f"OpenRouter service error: HTTP {resp.status_code}"
                    last_code  = "service_unavailable"
                    continue

                if not resp.is_success:
                    try:
                        detail = resp.json().get("error", {}).get("message", resp.text[:200])
                    except Exception:
                        detail = resp.text[:200]
                    return ProviderResult(
                        ok=False, provider="openrouter",
                        error=f"HTTP {resp.status_code}: {detail}",
                        error_code="http_error", latency_ms=latency_ms,
                    )

                data = resp.json()
                content, t_in, t_out = _parse_oai_response(data)
                return ProviderResult(
                    ok=True, content=content,
                    model_id=data.get("model", self.model_id),
                    provider="openrouter",
                    tokens_input=t_in, tokens_output=t_out,
                    latency_ms=latency_ms, raw_response=data,
                )

            except httpx.TimeoutException:
                latency_ms = int((time.monotonic() - t0) * 1000)
                logger.warning("[openrouter_adapter] Timeout (attempt %d)", attempt + 1)
                last_error = f"Request timed out after {self.timeout_sec}s"
                last_code  = "timeout"

            except httpx.ConnectError as exc:
                last_error = f"Connection error: {exc}"
                last_code  = "connect_error"

            except Exception as exc:
                logger.exception("[openrouter_adapter] Unexpected error on attempt %d", attempt + 1)
                last_error = str(exc)
                last_code  = "unexpected_error"

        return ProviderResult(
            ok=False, provider="openrouter",
            error=last_error, error_code=last_code,
        )

    def test_connection(self, api_key: str) -> ProviderResult:
        """List available models to validate the API key."""
        if not api_key:
            return ProviderResult(
                ok=False, provider="openrouter",
                error="No API key provided", error_code="no_api_key",
            )
        url     = f"{_OR_API_BASE}/v1/models"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer":  _APP_REFERER,
            "X-Title":       _APP_TITLE,
        }
        t0 = time.monotonic()
        try:
            resp       = httpx.get(url, headers=headers, timeout=10.0)
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.status_code == 401:
                return ProviderResult(
                    ok=False, provider="openrouter",
                    error="Invalid or expired API key",
                    error_code="invalid_key", latency_ms=latency_ms,
                )
            if resp.is_success:
                return ProviderResult(
                    ok=True, content="ok", provider="openrouter",
                    model_id=self.model_id, latency_ms=latency_ms,
                )
            return ProviderResult(
                ok=False, provider="openrouter",
                error=f"HTTP {resp.status_code}", error_code="http_error",
                latency_ms=latency_ms,
            )
        except Exception as exc:
            return ProviderResult(
                ok=False, provider="openrouter",
                error=str(exc), error_code="connect_error",
            )


# ── Shared helper ─────────────────────────────────────────────────────────────


def _parse_oai_response(data: dict):
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = ""
    usage = data.get("usage", {})
    return content, usage.get("prompt_tokens"), usage.get("completion_tokens")
