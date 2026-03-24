"""
EOS — Provider Abstraction: Base Types
=======================================
ProviderResult, ProviderCapabilities, and BaseProvider ABC.

All provider adapters must extend BaseProvider and return ProviderResult.
No adapter may raise — every error path returns ProviderResult(ok=False).

Trust boundary
--------------
ProviderResult.is_external mirrors the contract from HFInferenceResult:
  True  — result came from a remote provider (OpenAI, HF, etc.)
  False — result came from a local backend (llama-server)

Callers must not strip this flag when passing results downstream.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional


# ── Unified result type ────────────────────────────────────────────────────────


@dataclass
class ProviderResult:
    """
    Normalised result of any inference call, regardless of provider.

    Field names are intentionally identical to the legacy HFInferenceResult
    so that code using HFInferenceResult continues to work when ProviderResult
    is substituted.

    Additional fields not on HFInferenceResult:
      routed_by    — routing mode that selected this provider (set by router)
      fallback_from — provider that failed immediately before this one
    """
    ok:            bool
    content:       str            = ""
    model_id:      str            = ""
    provider:      str            = ""      # provider_id, e.g. "openai"
    tokens_input:  Optional[int]  = None
    tokens_output: Optional[int]  = None
    latency_ms:    Optional[int]  = None
    error:         str            = ""
    error_code:    str            = ""      # machine key, e.g. "rate_limit"
    raw_response:  Optional[dict] = None
    # TRUST BOUNDARY — False only for local (llama-server) backends
    is_external:   bool           = True
    # Routing metadata — set by InferenceRouter, not by adapters
    routed_by:     str            = ""
    fallback_from: Optional[str]  = None


# ── Provider capability declaration ───────────────────────────────────────────


@dataclass
class ProviderCapabilities:
    """
    Declared capabilities of a provider adapter.

    Used by InferenceRouter to filter providers that meet the request's
    requirements.  Values are defaults; operators may override via config.

    Tier conventions
    ----------------
    quality_tier : 1=premium, 2=standard, 3=budget   (lower = better)
    cost_tier    : 1=expensive, 2=moderate, 3=cheap, 4=free  (higher = cheaper)
    """
    provider_id:              str
    supports_tool_calling:    bool  = False
    supports_structured_json: bool  = False
    supports_streaming:       bool  = False
    supports_vision:          bool  = False
    # "small" | "medium" | "large" | "xl"
    context_size_class:       str   = "medium"
    is_local:                 bool  = False
    quality_tier:             int   = 2
    cost_tier:                int   = 2
    default_model:            str   = ""
    available_models:         List[str] = field(default_factory=list)


# ── Abstract base ─────────────────────────────────────────────────────────────


class BaseProvider(ABC):
    """
    Abstract base for all inference provider adapters.

    Adapters are stateless with respect to secrets — the API key is passed
    at call time, not stored in the instance.  Key rotation takes effect
    immediately without restarting the adapter.

    Thread safety: adapters must be safe to call concurrently from multiple
    threads.  httpx clients used inside adapters should be created per-call
    or managed with appropriate locking.
    """

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Stable lowercase identifier: 'huggingface', 'openai', etc."""
        ...

    @property
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """Declared capabilities of this provider."""
        ...

    @abstractmethod
    def complete(
        self,
        messages: list,
        *,
        api_key:     str,
        max_tokens:  int   = 512,
        temperature: float = 0.7,
    ) -> ProviderResult:
        """
        Send a chat completion request.

        Parameters
        ----------
        messages    — OpenAI-format list: [{"role": ..., "content": ...}]
        api_key     — provider API token (empty string for local)
        max_tokens  — max completion tokens
        temperature — sampling temperature

        Returns
        -------
        ProviderResult — always; never raises.
        """
        ...

    @abstractmethod
    def test_connection(self, api_key: str) -> ProviderResult:
        """
        Validate the API key and basic connectivity.

        Returns ProviderResult(ok=True, content="ok") on success.
        Never raises.
        """
        ...
