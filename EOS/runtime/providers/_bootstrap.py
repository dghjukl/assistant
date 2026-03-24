"""
EOS — Provider Bootstrap
=========================
Builds a ProviderRegistry and InferenceRouter from the EOS config dict.

Called once at startup by the ExternalInferencePolicy.  The registry and
router instances are held by the policy for the lifetime of the process.
"""
from __future__ import annotations

import logging
from typing import Optional

from runtime.providers.base import ProviderResult
from runtime.providers.registry import ProviderRegistry
from runtime.providers.router import InferenceRouter, RoutingMode, VALID_ROUTING_MODES
from runtime.providers.cost import build_cost_overrides
from runtime.providers.adapters.local       import LocalAdapter
from runtime.providers.adapters.huggingface import HuggingFaceAdapter
from runtime.providers.adapters.openai      import OpenAIAdapter
from runtime.providers.adapters.anthropic   import AnthropicAdapter
from runtime.providers.adapters.gemini      import GeminiAdapter
from runtime.providers.adapters.openrouter  import OpenRouterAdapter

logger = logging.getLogger("eos.providers.bootstrap")

# ── Default config values for each provider ───────────────────────────────────

_PROVIDER_DEFAULTS: dict = {
    "huggingface": {
        "model_id":    "mistralai/Mistral-7B-Instruct-v0.2",
        "timeout_sec": 30.0,
        "max_retries": 1,
    },
    "openai": {
        "model_id":    "gpt-4o-mini",
        "timeout_sec": 30.0,
        "max_retries": 1,
    },
    "anthropic": {
        "model_id":    "claude-haiku-4-5-20251001",
        "timeout_sec": 60.0,
        "max_retries": 1,
    },
    "gemini": {
        "model_id":    "gemini-2.0-flash",
        "timeout_sec": 30.0,
        "max_retries": 1,
    },
    "openrouter": {
        "model_id":    "meta-llama/llama-3.1-8b-instruct:free",
        "timeout_sec": 30.0,
        "max_retries": 1,
    },
}

# Default ordered fallback chain (most conservative → most expensive)
_DEFAULT_FALLBACK_ORDER = [
    "huggingface",
    "openrouter",
    "openai",
    "anthropic",
    "gemini",
]

# Default enabled providers (conservative: only HF by default)
_DEFAULT_ENABLED_PROVIDERS = ["huggingface"]


def build_registry(ei_cfg: dict, primary_endpoint: Optional[str] = None) -> ProviderRegistry:
    """
    Instantiate all provider adapters and register them.

    Parameters
    ----------
    ei_cfg            — external_inference block from config.json
    primary_endpoint  — local llama-server URL (e.g. "http://127.0.0.1:8080")

    Always registers all adapters, regardless of enabled_providers config.
    The enabled_providers list is enforced by the router, not the registry.
    """
    registry = ProviderRegistry()

    # Local adapter (always registered; key-free)
    if primary_endpoint:
        registry.register(LocalAdapter(
            endpoint    = primary_endpoint,
            timeout_sec = 30.0,
        ))
        logger.debug("[bootstrap] Registered local adapter: %s", primary_endpoint)

    # HuggingFace
    hf_cfg = {**_PROVIDER_DEFAULTS["huggingface"], **ei_cfg.get("huggingface", {})}
    registry.register(HuggingFaceAdapter(
        model_id    = str(hf_cfg["model_id"]),
        timeout_sec = float(hf_cfg["timeout_sec"]),
        max_retries = int(hf_cfg["max_retries"]),
    ))

    # OpenAI
    oai_cfg = {**_PROVIDER_DEFAULTS["openai"], **ei_cfg.get("openai", {})}
    registry.register(OpenAIAdapter(
        model_id    = str(oai_cfg["model_id"]),
        timeout_sec = float(oai_cfg["timeout_sec"]),
        max_retries = int(oai_cfg["max_retries"]),
    ))

    # Anthropic
    ant_cfg = {**_PROVIDER_DEFAULTS["anthropic"], **ei_cfg.get("anthropic", {})}
    registry.register(AnthropicAdapter(
        model_id    = str(ant_cfg["model_id"]),
        timeout_sec = float(ant_cfg["timeout_sec"]),
        max_retries = int(ant_cfg["max_retries"]),
    ))

    # Gemini
    gem_cfg = {**_PROVIDER_DEFAULTS["gemini"], **ei_cfg.get("gemini", {})}
    registry.register(GeminiAdapter(
        model_id    = str(gem_cfg["model_id"]),
        timeout_sec = float(gem_cfg["timeout_sec"]),
        max_retries = int(gem_cfg["max_retries"]),
    ))

    # OpenRouter
    or_cfg = {**_PROVIDER_DEFAULTS["openrouter"], **ei_cfg.get("openrouter", {})}
    registry.register(OpenRouterAdapter(
        model_id    = str(or_cfg["model_id"]),
        timeout_sec = float(or_cfg["timeout_sec"]),
        max_retries = int(or_cfg["max_retries"]),
    ))

    logger.info("[bootstrap] Registry built: %s", registry.ids())
    return registry


def build_router(ei_cfg: dict, registry: ProviderRegistry) -> InferenceRouter:
    """
    Construct an InferenceRouter from the external_inference config.

    Parameters
    ----------
    ei_cfg   — external_inference block from config.json
    registry — pre-built ProviderRegistry

    Routing config keys (all optional, with defaults):
      routing_mode      — default routing mode (default: "default")
      default_provider  — provider used for "default" mode (default: ei_cfg["provider"])
      fallback_order    — ordered list for "fallback" mode
      enabled_providers — which providers the router considers
    """
    raw_mode  = ei_cfg.get("routing_mode", "default")
    if raw_mode not in VALID_ROUTING_MODES:
        logger.warning("[bootstrap] Unknown routing_mode %r; falling back to 'default'", raw_mode)
        raw_mode = "default"

    default_provider = ei_cfg.get("default_provider") or ei_cfg.get("provider", "huggingface")
    fallback_order   = ei_cfg.get("fallback_order", _DEFAULT_FALLBACK_ORDER)
    enabled_providers = ei_cfg.get("enabled_providers", _DEFAULT_ENABLED_PROVIDERS)

    # Ensure the default/active provider is in the enabled list
    if default_provider not in enabled_providers:
        enabled_providers = [default_provider] + list(enabled_providers)

    cost_overrides = build_cost_overrides(ei_cfg)

    router = InferenceRouter(
        registry          = registry,
        enabled_providers = enabled_providers,
        default_provider  = default_provider,
        fallback_order    = fallback_order,
        cost_overrides    = cost_overrides or None,
    )
    logger.info(
        "[bootstrap] Router built: mode=%s default=%s fallback=%s enabled=%s",
        raw_mode, default_provider, fallback_order, enabled_providers,
    )
    return router
