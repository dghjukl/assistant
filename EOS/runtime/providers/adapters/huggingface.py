"""
EOS — HuggingFace Provider Adapter
=====================================
Adapter that wraps the HuggingFace Inference API (OpenAI-compatible endpoint).

This adapter is the successor to the legacy HuggingFaceProvider in
runtime/external_inference.py.  The legacy class is preserved for backward
compatibility; this adapter is used by the InferenceRouter.

Secret key name in SecretsManager: "huggingface_api_key"
API base: https://api-inference.huggingface.co

Cost tier: 3 (cheap)  — HF Serverless free tier + pay-per-token above quota
Quality tier: 3 (budget) — model quality varies widely; default is Mistral 7B class
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import httpx

from runtime.providers.base import BaseProvider, ProviderCapabilities, ProviderResult

logger = logging.getLogger("eos.providers.huggingface")

_HF_API_BASE = "https://api-inference.huggingface.co"
_DEFAULT_MODEL = "mistralai/Mistral-7B-Instruct-v0.2"

# Secret key name used in EOS SecretsManager
SECRET_KEY = "huggingface_api_key"


class HuggingFaceAdapter(BaseProvider):
    """
    Adapter for the HuggingFace Inference API.

    Parameters
    ----------
    model_id    — HF model repo ID (e.g. "mistralai/Mistral-7B-Instruct-v0.2")
    timeout_sec — per-request timeout in seconds
    max_retries — retry count on rate-limit (429) and service unavailable (503)
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
        return "huggingface"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id              = "huggingface",
            supports_tool_calling    = False,
            supports_structured_json = False,
            supports_streaming       = False,
            supports_vision          = False,
            context_size_class       = "medium",
            is_local                 = False,
            quality_tier             = 3,   # budget
            cost_tier                = 3,   # cheap
            default_model            = self.model_id,
        )

    def with_model(self, model_id: str) -> "HuggingFaceAdapter":
        """Return a new adapter instance configured for *model_id*."""
        return HuggingFaceAdapter(
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
                ok=False, provider="huggingface",
                error="No API key provided", error_code="no_api_key",
            )

        url     = f"{_HF_API_BASE}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        }
        body: dict = {
            "model":       self.model_id,
            "messages":    messages,
            "max_tokens":  max_tokens,
            "temperature": temperature,
            "stream":      False,
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
                        ok=False, provider="huggingface",
                        error="Invalid or expired API key",
                        error_code="invalid_key", latency_ms=latency_ms,
                    )

                if resp.status_code == 429:
                    logger.warning("[hf_adapter] Rate limited (attempt %d/%d)",
                                   attempt + 1, self.max_retries)
                    last_error = "Rate limited by HuggingFace"
                    last_code  = "rate_limit"
                    time.sleep(1.5)
                    continue

                if resp.status_code == 503:
                    last_error = "HuggingFace service unavailable"
                    last_code  = "service_unavailable"
                    continue

                if not resp.is_success:
                    try:
                        detail = resp.json().get("error", resp.text[:200])
                    except Exception:
                        detail = resp.text[:200]
                    return ProviderResult(
                        ok=False, provider="huggingface",
                        error=f"HTTP {resp.status_code}: {detail}",
                        error_code="http_error", latency_ms=latency_ms,
                    )

                data = resp.json()
                content, t_in, t_out = _parse_oai_response(data)
                return ProviderResult(
                    ok=True, content=content,
                    model_id=self.model_id, provider="huggingface",
                    tokens_input=t_in, tokens_output=t_out,
                    latency_ms=latency_ms, raw_response=data,
                )

            except httpx.TimeoutException:
                latency_ms = int((time.monotonic() - t0) * 1000)
                logger.warning("[hf_adapter] Timeout after %dms (attempt %d)",
                               latency_ms, attempt + 1)
                last_error = f"Request timed out after {self.timeout_sec}s"
                last_code  = "timeout"

            except httpx.ConnectError as exc:
                last_error = f"Connection error: {exc}"
                last_code  = "connect_error"

            except Exception as exc:
                logger.exception("[hf_adapter] Unexpected error on attempt %d", attempt + 1)
                last_error = str(exc)
                last_code  = "unexpected_error"

        return ProviderResult(
            ok=False, provider="huggingface",
            error=last_error, error_code=last_code,
        )

    def test_connection(self, api_key: str) -> ProviderResult:
        """Probe the model info endpoint to validate the API key and model ID."""
        if not api_key:
            return ProviderResult(
                ok=False, provider="huggingface",
                error="No API key provided", error_code="no_api_key",
            )

        url     = f"{_HF_API_BASE}/api/models/{self.model_id}"
        headers = {"Authorization": f"Bearer {api_key}"}
        t0      = time.monotonic()
        try:
            resp       = httpx.get(url, headers=headers, timeout=10.0)
            latency_ms = int((time.monotonic() - t0) * 1000)

            if resp.status_code == 401:
                return ProviderResult(
                    ok=False, provider="huggingface",
                    error="Invalid or expired API key",
                    error_code="invalid_key", latency_ms=latency_ms,
                )
            if resp.status_code == 404:
                return ProviderResult(
                    ok=False, provider="huggingface",
                    error=f"Model not found: {self.model_id}",
                    error_code="model_not_found", latency_ms=latency_ms,
                )
            if resp.is_success:
                data       = resp.json() if resp.text else {}
                model_name = data.get("modelId", self.model_id)
                return ProviderResult(
                    ok=True, content="ok", provider="huggingface",
                    model_id=model_name, latency_ms=latency_ms,
                    raw_response=data,
                )
            return ProviderResult(
                ok=False, provider="huggingface",
                error=f"HTTP {resp.status_code}", error_code="http_error",
                latency_ms=latency_ms,
            )
        except httpx.TimeoutException:
            return ProviderResult(
                ok=False, provider="huggingface",
                error="Connection test timed out", error_code="timeout",
            )
        except Exception as exc:
            return ProviderResult(
                ok=False, provider="huggingface",
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
