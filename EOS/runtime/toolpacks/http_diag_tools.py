"""HTTP Diagnostics — TLS environment and CA bundle inspection"""

from __future__ import annotations

import json
import os
import ssl
import sys
from typing import Any, Dict


def _jdump(x: Any) -> str:
    try:
        return json.dumps(x, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


def register(registry: Any, config: Dict[str, Any]) -> None:
    """Register HTTP diagnostic tools."""
    from runtime.tool_registry import (
        ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy
    )

    http_cfg = config.get("http_diag_tools", {}) if isinstance(config, dict) else {}
    enabled = bool(http_cfg.get("enabled", True))

    def tls_env_info_handler(params: Dict[str, Any]) -> str:
        info: Dict[str, Any] = {
            "python": sys.version,
            "executable": sys.executable,
            "ssl_openssl_version": getattr(ssl, "OPENSSL_VERSION", None),
        }
        try:
            vp = ssl.get_default_verify_paths()
            info["ssl_default_verify_paths"] = {
                "cafile": vp.cafile,
                "capath": vp.capath,
                "openssl_cafile_env": vp.openssl_cafile_env,
                "openssl_capath_env": vp.openssl_capath_env,
            }
        except Exception as e:
            info["ssl_error"] = str(e)

        try:
            import certifi
            info["certifi_installed"] = True
            info["certifi_where"] = certifi.where()
        except Exception:
            info["certifi_installed"] = False

        try:
            import requests
            info["requests_installed"] = True
            info["requests_version"] = getattr(requests, "__version__", None)
        except Exception:
            info["requests_installed"] = False

        return _jdump(info)

    registry.register(ToolSpec(
        name="tls_env_info",
        description="Show TLS environment and CA bundle configuration.",
        pack="http_diag_tools",
        tags=["web", "network"],
        parameters={"type": "object", "properties": {}},
        handler=tls_env_info_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    def ca_bundle_info_handler(params: Dict[str, Any]) -> str:
        info: Dict[str, Any] = {"exists": {}}

        try:
            import requests
            bundle = getattr(getattr(requests, "utils", None), "DEFAULT_CA_BUNDLE_PATH", None)
            if bundle:
                info["requests_ca_bundle"] = bundle
                info["exists"][bundle] = os.path.exists(bundle)
        except Exception:
            pass

        try:
            import certifi
            cw = certifi.where()
            info["certifi_where"] = cw
            info["exists"][cw] = os.path.exists(cw)
        except Exception:
            pass

        try:
            vp = ssl.get_default_verify_paths()
            for p in [vp.cafile, vp.capath, vp.openssl_cafile, vp.openssl_capath]:
                if p:
                    info["exists"][p] = os.path.exists(p)
        except Exception:
            pass

        return _jdump(info)

    registry.register(ToolSpec(
        name="ca_bundle_info",
        description="Check CA bundle paths and verify they exist.",
        pack="http_diag_tools",
        tags=["web", "network"],
        parameters={"type": "object", "properties": {}},
        handler=ca_bundle_info_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))
