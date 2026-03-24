"""
EOS — OpenAI Provider Adapter
================================
Adapter for the OpenAI Chat Completions API via direct HTTP (no SDK).

Endpoint: https://api.openai.com/v1/chat/completions
Secret key name in SecretsManager: "openai_api_key"

Cost tier: 2 (moderate) — GPT-4o class is expensive; use gpt-4o-mini for budget
Quality tier: 1 (premium) — best-in-class reasoning and instruction following

Supported models (non-exhaustive examples):
  gpt-4o, gpt-4o-mini, gpt-4-turbo, gpt-3.5-turbo

The default model is configurable via config.json:
  external_inference.openai.model_id
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import httpx

from runtime.providers.base import BaseProvider, ProviderCapabilities, ProviderResult

logger = logging.getLogger("eos.providers.openai")

_OPENAI_API_BASE = "https://api.openai.com"
_DEFAULT_MODEL   = "gpt-4o-mini"

# Secret key name used in EOS SecretsManager
SECRET_KEY = "openai_api_key"


class OpenAIAdapter(BaseProvider):
    """
    Adapter for the OpenAI Chat Completions API.

    Parameters
    ----------
    model_id    — OpenAI model name (e.g. "gpt-4o-mini", "gpt-4o")
    timeout_sec — per-request timeout in seconds
    max_retries — retry count on rate-limit (429) and service errors (5xx)
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
        return "openai"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id              = "openai",
            supports_tool_calling    = True,
            supports_structured_json = True,
            supports_streaming       = True,   # capability exists; not used here
            supports_vision          = True,   # GPT-4o class supports images
            context_size_class       = "xl",
            is_local                 = False,
            quality_tier             = 1,   # premium
            cost_tier                = 2,   # moderate
            default_model            = self.model_id,
        )

    def with_model(self, model_id: str) -> "OpenAIAdapter":
        return OpenAIAdapter(
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
                ok=False, provider="openai",
                error="No API key provided", error_code="no_api_key",
            )

        url     = f"{_OPENAI_API_BASE}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
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
                        ok=False, provider="openai",
                        error="Invalid or expired API key",
                        error_code="invalid_key", latency_ms=latency_ms,
                    )

                if resp.status_code == 429:
                    logger.warning("[openai_adapter] Rate limited (attempt %d/%d)",
                                   attempt + 1, self.max_retries)
                    last_error = "Rate limited by OpenAI"
                    last_code  = "rate_limit"
                    time.sleep(2.0)
                    continue

                if resp.status_code in (500, 502, 503):
                    last_error = f"OpenAI service error: HTTP {resp.status_code}"
                    last_code  = "service_unavailable"
                    continue

                if not resp.is_success:
                    try:
                        detail = resp.json().get("error", {}).get("message", resp.text[:200])
                    except Exception:
                        detail = resp.text[:200]
                    return ProviderResult(
                        ok=False, provider="openai",
                        error=f"HTTP {resp.status_code}: {detail}",
                        error_code="http_error", latency_ms=latency_ms,
                    )

                data = resp.json()
                content, t_in, t_out = _parse_oai_response(data)
                return ProviderResult(
                    ok=True, content=content,
                    model_id=data.get("model", self.model_id),
                    provider="openai",
                    tokens_input=t_in, tokens_output=t_out,
                    latency_ms=latency_ms, raw_response=data,
                )

            except httpx.TimeoutException:
                latency_ms = int((time.monotonic() - t0) * 1000)
                logger.warning("[openai_adapter] Timeout (attempt %d)", attempt + 1)
                last_error = f"Request timed out after {self.timeout_sec}s"
                last_code  = "timeout"

            except httpx.ConnectError as exc:
                last_error = f"Connection error: {exc}"
                last_code  = "connect_error"

            except Exception as exc:
                logger.exception("[openai_adapter] Unexpected error on attempt %d", attempt + 1)
                last_error = str(exc)
                last_code  = "unexpected_error"

        return ProviderResult(
            ok=False, provider="openai",
            error=last_error, error_code=last_code,
        )

    def test_connection(self, api_key: str) -> ProviderResult:
        """List available models to validate the API key."""
        if not api_key:
            return ProviderResult(
                ok=False, provider="openai",
                error="No API key provided", error_code="no_api_key",
            )
        url     = f"{_OPENAI_API_BASE}/v1/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        t0      = time.monotonic()
        try:
            resp       = httpx.get(url, headers=headers, timeout=10.0)
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.status_code == 401:
                return ProviderResult(
                    ok=False, provider="openai",
                    error="Invalid or expired API key",
                    error_code="invalid_key", latency_ms=latency_ms,
                )
            if resp.is_success:
                return ProviderResult(
                    ok=True, content="ok", provider="openai",
                    model_id=self.model_id, latency_ms=latency_ms,
                )
            return ProviderResult(
                ok=False, provider="openai",
                error=f"HTTP {resp.status_code}", error_code="http_error",
                latency_ms=latency_ms,
            )
        except Exception as exc:
            return ProviderResult(
                ok=False, provider="openai",
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
