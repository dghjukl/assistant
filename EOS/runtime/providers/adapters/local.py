"""
EOS — Local Provider Adapter
==============================
Wraps the local llama-server primary backend (OpenAI-compatible REST API).

No API key required.  is_external=False on all results.
The primary server runs at cfg["servers"]["primary"]["host:port"] — the
endpoint URL is passed at construction time.

This adapter lets the router treat local inference as just another backend,
enabling LOCAL_ONLY and CHEAPEST routing modes that prefer free compute.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import httpx

from runtime.providers.base import BaseProvider, ProviderCapabilities, ProviderResult

logger = logging.getLogger("eos.providers.local")

_DEFAULT_ENDPOINT = "http://127.0.0.1:8080"


class LocalAdapter(BaseProvider):
    """
    Adapter for the local llama-server (OpenAI-compatible) primary backend.

    Parameters
    ----------
    endpoint    — full base URL, e.g. "http://127.0.0.1:8080"
    timeout_sec — per-request timeout
    """

    def __init__(
        self,
        endpoint:    str   = _DEFAULT_ENDPOINT,
        timeout_sec: float = 30.0,
    ) -> None:
        self._endpoint   = endpoint.rstrip("/")
        self._timeout    = timeout_sec

    # ── BaseProvider interface ────────────────────────────────────────────────

    @property
    def provider_id(self) -> str:
        return "local"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id              = "local",
            supports_tool_calling    = False,
            supports_structured_json = True,
            supports_streaming       = False,
            supports_vision          = False,
            context_size_class       = "large",
            is_local                 = True,
            quality_tier             = 2,   # standard — varies by loaded model
            cost_tier                = 4,   # free
            default_model            = "local",
        )

    def with_model(self, model_id: str) -> "LocalAdapter":
        """Local adapter ignores model_id — returns self unchanged."""
        return self

    def complete(
        self,
        messages: List[Dict],
        *,
        api_key:     str   = "",
        max_tokens:  int   = 512,
        temperature: float = 0.7,
    ) -> ProviderResult:
        url  = f"{self._endpoint}/v1/chat/completions"
        body = {
            "messages":    messages,
            "max_tokens":  max_tokens,
            "temperature": temperature,
            "stream":      False,
        }
        t0 = time.monotonic()
        try:
            resp       = httpx.post(url, json=body, timeout=self._timeout)
            latency_ms = int((time.monotonic() - t0) * 1000)

            if not resp.is_success:
                return ProviderResult(
                    ok=False, provider="local",
                    error=f"HTTP {resp.status_code}", error_code="http_error",
                    latency_ms=latency_ms, is_external=False,
                )

            data = resp.json()
            content, t_in, t_out = _parse_oai_response(data)
            if not content:
                return ProviderResult(
                    ok=False, provider="local",
                    error="Empty response from local backend",
                    error_code="empty_response",
                    latency_ms=latency_ms, is_external=False,
                )
            return ProviderResult(
                ok=True, content=content, provider="local", model_id="local",
                tokens_input=t_in, tokens_output=t_out,
                latency_ms=latency_ms, is_external=False,
            )

        except httpx.ConnectError as exc:
            return ProviderResult(
                ok=False, provider="local",
                error=f"Connection error: {exc}", error_code="connect_error",
                is_external=False,
            )
        except httpx.TimeoutException:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return ProviderResult(
                ok=False, provider="local",
                error=f"Request timed out after {self._timeout}s",
                error_code="timeout", latency_ms=latency_ms, is_external=False,
            )
        except Exception as exc:
            logger.exception("[local] Unexpected error in complete()")
            return ProviderResult(
                ok=False, provider="local",
                error=str(exc), error_code="unexpected_error", is_external=False,
            )

    def test_connection(self, api_key: str = "") -> ProviderResult:
        """Probe the /health endpoint to verify the local backend is running."""
        url = f"{self._endpoint}/health"
        t0  = time.monotonic()
        try:
            resp       = httpx.get(url, timeout=5.0)
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.is_success:
                return ProviderResult(
                    ok=True, content="ok", provider="local",
                    latency_ms=latency_ms, is_external=False,
                )
            return ProviderResult(
                ok=False, provider="local",
                error=f"HTTP {resp.status_code}", error_code="http_error",
                latency_ms=latency_ms, is_external=False,
            )
        except Exception as exc:
            return ProviderResult(
                ok=False, provider="local",
                error=str(exc), error_code="connect_error", is_external=False,
            )


# ── Shared helper ─────────────────────────────────────────────────────────────


def _parse_oai_response(data: dict):
    """Extract (content, tokens_in, tokens_out) from an OpenAI-compat response."""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = ""
    usage   = data.get("usage", {})
    return content, usage.get("prompt_tokens"), usage.get("completion_tokens")
