"""Toolpack Loader — Discovers and loads toolpack modules

Owns the responsibilities of:
- Discovering which toolpacks to load from configuration
- Calling register() for each pack in the list
- Building and exposing the capability manifest
- Failing loudly for any required packs that fail

Usage in runtime initialization::

    from .toolpack_loader import ToolpackLoader
    from .tool_registry import ToolRegistry

    registry = ToolRegistry()
    loader = ToolpackLoader(registry=registry, config={})
    manifest = loader.load_all()
    print(manifest["summary"])
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from .toolpacks import try_register_pack

logger = logging.getLogger(__name__)


class ToolpackLoader:
    """Loads toolpacks from configuration and builds the capability manifest.

    Parameters
    ----------
    registry : ToolRegistry
        The ToolRegistry instance that packs register tools into.
    config : dict
        Configuration dict with optional "toolpacks" section containing:
        - enabled: bool (default True) — whether to load packs at all
        - packs: list[str] — which packs to load
        - required_packs: list[str] — packs that must load or startup fails
    """

    def __init__(
        self,
        registry: Any,
        config: Dict[str, Any] | None = None,
    ) -> None:
        self._registry = registry
        self._config = config or {}
        self._capability_manifest: Dict[str, Any] = {
            "enabled": False,
            "no_packs_requested": False,
            "packs": [],
            "summary": {"total": 0, "loaded": 0, "failed": 0, "skipped": 0},
        }
        self._loaded = False

    @property
    def capability_manifest(self) -> Dict[str, Any]:
        """The capability manifest built during load_all()."""
        return self._capability_manifest

    def load_all(self) -> Dict[str, Any]:
        """Load all configured toolpacks and build the capability manifest.

        Returns
        -------
        dict
            The capability manifest with enabled status, pack list, and summary.

        Raises
        ------
        RuntimeError
            If any pack listed in required_packs fails to load.
        """
        toolpack_cfg = self._config.get("toolpacks", {})
        if not isinstance(toolpack_cfg, dict):
            toolpack_cfg = {}

        packs_enabled = bool(toolpack_cfg.get("enabled", True))
        packs_value = toolpack_cfg.get("packs", None)
        # Null/missing packs means "load nothing"; operators must opt in with names.
        pack_list: List[str] = [] if packs_value is None else list(packs_value)
        no_packs_requested = packs_enabled and not pack_list
        required_packs: List[str] = list(toolpack_cfg.get("required_packs") or [])

        cap_entries: List[Dict[str, Any]] = []

        if packs_enabled:
            for pack_name in pack_list:
                result = try_register_pack(self._registry, pack_name, self._config)
                loaded = result.get("status") == "ok"
                skipped = result.get("status") == "skipped"
                failed = result.get("status") == "failed"
                entry: Dict[str, Any] = {
                    "pack": pack_name,
                    "requested": True,
                    "loaded": loaded,
                    "skipped": skipped,
                    "failed": failed,
                    "reason": result.get("status", "unknown"),
                    "error": result.get("error"),
                }
                cap_entries.append(entry)
                if failed:
                    logger.warning(
                        "[toolpack_loader] Pack '%s' failed to load: %s",
                        pack_name,
                        result.get("error"),
                    )
        else:
            cap_entries.append(
                {
                    "pack": "__all__",
                    "requested": False,
                    "loaded": False,
                    "skipped": True,
                    "failed": False,
                    "reason": "toolpacks_disabled",
                }
            )

        if no_packs_requested:
            logger.warning(
                "[startup][toolpacks] toolpacks.enabled=true but toolpacks.packs is null/empty; "
                "no packs were requested and none will be loaded"
            )

        # Fail loudly for required packs that didn't load.
        for req_pack in required_packs:
            entry = next((e for e in cap_entries if e.get("pack") == req_pack), None)
            if entry is None or not entry.get("loaded"):
                reason = (entry or {}).get("error", "not in pack list")
                raise RuntimeError(
                    f"[startup] Required toolpack '{req_pack}' failed to load: {reason}. "
                    "Fix the pack or remove it from 'required_packs' in config."
                )

        self._capability_manifest = {
            "enabled": packs_enabled,
            "no_packs_requested": no_packs_requested,
            "packs": cap_entries,
            "summary": {
                "total": len(cap_entries),
                "loaded": sum(1 for e in cap_entries if e.get("loaded")),
                "failed": sum(1 for e in cap_entries if e.get("failed")),
                "skipped": sum(1 for e in cap_entries if e.get("skipped")),
            },
        }
        self._loaded = True
        logger.info(
            "[toolpack_loader] Loaded %d/%d packs; %d failed; %d skipped",
            self._capability_manifest["summary"]["loaded"],
            self._capability_manifest["summary"]["total"],
            self._capability_manifest["summary"]["failed"],
            self._capability_manifest["summary"]["skipped"],
        )
        return self._capability_manifest
