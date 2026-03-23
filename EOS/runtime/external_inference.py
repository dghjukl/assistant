"""
EOS — Hugging Face External Inference Client
=============================================
First-class HTTP client for the Hugging Face Inference API.

Design principles
-----------------
* This module is purely a provider client.  It has no knowledge of budgets,
  origin checks, or policy.  Those concerns belong to external_inference_policy.
* All calls are made via the official REST API — no browser automation, no
  scraping, no unofficial endpoints.
* The API key is retrieved from the EOS SecretsManager at call time; it is
  never cached in a module global or logged.
* Responses are wrapped in HFInferenceResult so callers can clearly distinguish
  external results from local results (trust boundary labelling).
* Every failure mode returns a structured HFInferenceResult with ok=False rather
  than raising, so the orchestrator degrades cleanly to local behaviour.

Hugging Face Inference API endpoints used
------------------------------------------
  Chat completions (OpenAI-compatible):
    POST https://api-inference.huggingface.co/v1/chat/completions
    Authorization: Bearer <token>
    Body: { model, messages, max_tokens, temperature, stream:false }

  Health / connection test:
    GET  https://api-inference.huggingface.co/v1/models/<model_id>
    Authorization: Bearer <token>

Cost estimation
---------------
HuggingFace Inference API pricing varies by model.  Because reliable per-model
pricing data is not machine-readable from the API, we use a conservative flat
rate per 1K tokens as the cost estimate.  This estimate is intentionally high
to avoid under-counting spend.  The estimate module is isolated so it can be
replaced with real pricing data when available.

The DEFAULT_COST_PER_1K_TOKENS constant covers both input and output tokens
at the same rate (conservative; input is cheaper in reality).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("eos.ext_inference.hf")

# ── Cost estimation constants ─────────────────────────────────────────────────

# Conservative per-1K-token rate in USD.
# HF Serverless Inference for most models is ~$0.0004–$0.002/1K tokens.
# We use $0.005 to stay safely above worst-case rates.
DEFAULT_COST_PER_1K_TOKENS: float = 0.005

# Fallback token estimate when we cannot count tokens before sending
DEFAULT_ASSUMED_INPUT_TOKENS: int = 512
DEFAULT_ASSUMED_OUTPUT_TOKENS: int = 256

# ── Result dataclass ──────────────────────────────────────────────────────────


@dataclass
class HFInferenceResult:
    """
    Structured result of a Hugging Face inference call or test.

    All callers should check .ok before using .content.
    The .is_external flag must be propagated through any response pipeline so
    that external results are never silently treated as local guaranteed outputs.
    """
    ok:              bool
    content:         str   = ""
    model_id:        str   = ""
    tokens_input:    Optional[int] = None
    tokens_output:   Optional[int] = None
    latency_ms:      Optional[int] = None
    error:           str   = ""
    error_code:      str   = ""   # machine key e.g. "invalid_key", "rate_limit"
    raw_response:    Optional[dict] = None
    # TRUST BOUNDARY — always True for results from this module.
    # Callers must not strip this flag when passing results downstream.
    is_external:     bool  = True
    provider:        str   = "huggingface"


# ── Provider client ───────────────────────────────────────────────────────────

_HF_API_BASE = "https://api-inference.huggingface.co"


class HuggingFaceProvider:
    """
    Stateless client for the Hugging Face Inference API.

    Instantiate once and reuse.  Does not cache the API key — always reads it
    from the secrets manager at call time so key rotation takes effect
    immediately.
    """

    def __init__(
        self,
        model_id: str,
        timeout_sec: float = 30.0,
        max_retries: int = 1,
    ) -> None:
        self.model_id    = model_id
        self.timeout_sec = timeout_sec
        self.max_retries = max_retries

    # ── Public API ────────────────────────────────────────────────────────────

    def complete(
        self,
        messages: List[Dict[str, str]],
        *,
        api_key: str,
        max_tokens: int = 512,
        temperature: float = 0.7,
    ) -> HFInferenceResult:
        """
        Call the HF chat completions endpoint synchronously.

        Parameters
        ----------
        messages   — OpenAI-format message list [{"role": ..., "content": ...}]
        api_key    — HuggingFace API token (Bearer)
        max_tokens — max completion tokens (passed to the API)
        temperature — generation temperature

        Returns
        -------
        HFInferenceResult — always; never raises.
        """
        if not api_key:
            return HFInferenceResult(
                ok=False,
                error="No API key provided",
                error_code="no_api_key",
            )

        url = f"{_HF_API_BASE}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body: dict = {
            "model": self.model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }

        t0 = time.monotonic()
        last_error = ""
        last_code  = "unknown"

        for attempt in range(max(1, self.max_retries)):
            try:
                resp = httpx.post(
                    url,
                    headers=headers,
                    json=body,
                    timeout=self.timeout_sec,
                )
                latency_ms = int((time.monotonic() - t0) * 1000)

                if resp.status_code == 401:
                    return HFInferenceResult(
                        ok=False,
                        error="Invalid or expired API key",
                        error_code="invalid_key",
                        latency_ms=latency_ms,
                    )

                if resp.status_code == 429:
                    logger.warning("[hf_provider] Rate limited (attempt %d)", attempt + 1)
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
                    return HFInferenceResult(
                        ok=False,
                        error=f"HTTP {resp.status_code}: {detail}",
                        error_code="http_error",
                        latency_ms=latency_ms,
                    )

                data = resp.json()
                content, tokens_in, tokens_out = self._parse_chat_response(data)
                return HFInferenceResult(
                    ok=True,
                    content=content,
                    model_id=self.model_id,
                    tokens_input=tokens_in,
                    tokens_output=tokens_out,
                    latency_ms=latency_ms,
                    raw_response=data,
                )

            except httpx.TimeoutException:
                latency_ms = int((time.monotonic() - t0) * 1000)
                logger.warning("[hf_provider] Timeout after %dms (attempt %d)",
                               latency_ms, attempt + 1)
                last_error = f"Request timed out after {self.timeout_sec}s"
                last_code  = "timeout"

            except httpx.ConnectError as exc:
                last_error = f"Connection error: {exc}"
                last_code  = "connect_error"

            except Exception as exc:
                logger.exception("[hf_provider] Unexpected error on attempt %d", attempt + 1)
                last_error = str(exc)
                last_code  = "unexpected_error"

        return HFInferenceResult(
            ok=False,
            error=last_error,
            error_code=last_code,
        )

    def test_connection(self, api_key: str) -> HFInferenceResult:
        """
        Probe the model's info endpoint to validate the API key and model.

        Returns HFInferenceResult with ok=True and content="ok" on success.
        """
        if not api_key:
            return HFInferenceResult(
                ok=False,
                error="No API key provided",
                error_code="no_api_key",
            )

        url = f"{_HF_API_BASE}/api/models/{self.model_id}"
        headers = {"Authorization": f"Bearer {api_key}"}

        t0 = time.monotonic()
        try:
            resp = httpx.get(url, headers=headers, timeout=10.0)
            latency_ms = int((time.monotonic() - t0) * 1000)

            if resp.status_code == 401:
                return HFInferenceResult(
                    ok=False,
                    error="Invalid or expired API key",
                    error_code="invalid_key",
                    latency_ms=latency_ms,
                )
            if resp.status_code == 404:
                return HFInferenceResult(
                    ok=False,
                    error=f"Model not found: {self.model_id}",
                    error_code="model_not_found",
                    latency_ms=latency_ms,
                )
            if resp.is_success:
                data = resp.json() if resp.text else {}
                model_name = data.get("modelId", self.model_id)
                return HFInferenceResult(
                    ok=True,
                    content="ok",
                    model_id=model_name,
                    latency_ms=latency_ms,
                    raw_response=data,
                )
            return HFInferenceResult(
                ok=False,
                error=f"HTTP {resp.status_code}",
                error_code="http_error",
                latency_ms=latency_ms,
            )
        except httpx.TimeoutException:
            return HFInferenceResult(
                ok=False,
                error="Connection test timed out",
                error_code="timeout",
            )
        except Exception as exc:
            return HFInferenceResult(
                ok=False,
                error=str(exc),
                error_code="connect_error",
            )

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_chat_response(data: dict) -> tuple[str, Optional[int], Optional[int]]:
        """Extract (content, tokens_in, tokens_out) from an OpenAI-compat response."""
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            content = ""

        usage = data.get("usage", {})
        tokens_in  = usage.get("prompt_tokens")
        tokens_out = usage.get("completion_tokens")
        return content, tokens_in, tokens_out


# ── Cost estimation ───────────────────────────────────────────────────────────


def estimate_cost(
    *,
    tokens_input: Optional[int] = None,
    tokens_output: Optional[int] = None,
    cost_per_1k: float = DEFAULT_COST_PER_1K_TOKENS,
) -> float:
    """
    Conservative pre-call cost estimate in USD.

    If exact token counts are not yet known (pre-call), falls back to
    DEFAULT_ASSUMED_INPUT_TOKENS / DEFAULT_ASSUMED_OUTPUT_TOKENS.

    The estimate is intentionally conservative (high) to prevent under-counting.
    """
    inp = tokens_input  if tokens_input  is not None else DEFAULT_ASSUMED_INPUT_TOKENS
    out = tokens_output if tokens_output is not None else DEFAULT_ASSUMED_OUTPUT_TOKENS
    total_tokens = inp + out
    return round((total_tokens / 1000.0) * cost_per_1k, 6)


# ── Module-level singleton ─────────────────────────────────────────────────────

_provider: Optional[HuggingFaceProvider] = None


def init_provider(model_id: str, timeout_sec: float = 30.0, max_retries: int = 1) -> HuggingFaceProvider:
    """Create or replace the module-level provider singleton."""
    global _provider
    _provider = HuggingFaceProvider(
        model_id=model_id,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
    )
    logger.info("[hf_provider] Provider initialised for model %r", model_id)
    return _provider


def get_provider() -> Optional[HuggingFaceProvider]:
    """Return the active provider, or None if not yet initialised."""
    return _provider
