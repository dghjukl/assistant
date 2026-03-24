"""
EOS — Provider Registry
========================
Central registry for all available provider adapters.

One ProviderRegistry is created at startup and held in the policy engine.
Adapters are registered by provider_id.  The router looks them up here.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from runtime.providers.base import BaseProvider, ProviderCapabilities

logger = logging.getLogger("eos.providers.registry")


class ProviderRegistry:
    """
    Holds all registered provider adapters indexed by provider_id.

    Thread safety: register() is called once at startup; concurrent reads
    via get() / all() are safe without locks after that.
    """

    def __init__(self) -> None:
        self._providers: Dict[str, BaseProvider] = {}

    def register(self, provider: BaseProvider) -> None:
        """Register a provider adapter.  Replaces any existing registration."""
        pid = provider.provider_id
        self._providers[pid] = provider
        logger.debug("[registry] Registered provider: %s (local=%s)",
                     pid, provider.capabilities.is_local)

    def get(self, provider_id: str) -> Optional[BaseProvider]:
        """Return the adapter for *provider_id*, or None if not registered."""
        return self._providers.get(provider_id)

    def all(self) -> List[BaseProvider]:
        """Return all registered adapters in insertion order."""
        return list(self._providers.values())

    def ids(self) -> List[str]:
        """Return all registered provider_ids."""
        return list(self._providers.keys())

    def capabilities_map(self) -> Dict[str, ProviderCapabilities]:
        """Return {provider_id: ProviderCapabilities} for all registered providers."""
        return {pid: p.capabilities for pid, p in self._providers.items()}

    def summary(self) -> List[dict]:
        """Return a serialisable summary list for admin/status endpoints."""
        result = []
        for pid, p in self._providers.items():
            caps = p.capabilities
            result.append({
                "provider_id":             pid,
                "is_local":                caps.is_local,
                "quality_tier":            caps.quality_tier,
                "cost_tier":               caps.cost_tier,
                "supports_tool_calling":   caps.supports_tool_calling,
                "supports_structured_json": caps.supports_structured_json,
                "supports_streaming":      caps.supports_streaming,
                "supports_vision":         caps.supports_vision,
                "context_size_class":      caps.context_size_class,
                "default_model":           caps.default_model,
            })
        return result
