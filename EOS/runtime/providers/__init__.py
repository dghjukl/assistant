"""
EOS — Provider Abstraction Package
====================================
Unified multi-provider inference subsystem.

Public surface
--------------
  from runtime.providers import (
      ProviderResult, ProviderCapabilities, BaseProvider,
      ProviderRegistry, InferenceRouter, RoutingMode, RoutingRequest,
      RouteRecord, build_registry,
  )

Design rules
------------
* Providers are compute backends — interchangeable behind one interface.
* No provider-specific logic may live outside this package or its adapters.
* Every adapter returns ProviderResult; never raises.
* The router is deterministic: candidate ordering is derived from config,
  not random selection.
* Secrets are never stored in provider instances; passed at call time only.
"""
from runtime.providers.base import (
    ProviderResult,
    ProviderCapabilities,
    BaseProvider,
)
from runtime.providers.registry import ProviderRegistry
from runtime.providers.router import (
    InferenceRouter,
    RoutingMode,
    RoutingRequest,
    RouteRecord,
)
from runtime.providers._bootstrap import build_registry

__all__ = [
    "ProviderResult",
    "ProviderCapabilities",
    "BaseProvider",
    "ProviderRegistry",
    "InferenceRouter",
    "RoutingMode",
    "RoutingRequest",
    "RouteRecord",
    "build_registry",
]
