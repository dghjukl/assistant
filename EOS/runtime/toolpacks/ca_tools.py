"""CA Certificate Tools — Certificate diagnostics"""

from __future__ import annotations

import json
import ssl
import socket
from pathlib import Path
from typing import Any, Dict, List


def _jdump(x: Any) -> str:
    try:
        return json.dumps(x, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


def _safe_int(x: Any, default: int) -> int:
    try:
        return int(x)
    except Exception:
        return default


def register(registry: Any, config: Dict[str, Any]) -> None:
    from runtime.tool_registry import ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy

    cfg_ca = config.get("ca_fix", {}) if isinstance(config, dict) else {}
    enabled = bool(cfg_ca.get("enabled", True))

    def check_ca_bundle_handler(params: Dict[str, Any]) -> str:
        try:
            import certifi
            ca_bundle = certifi.where()
            ca_path = Path(ca_bundle)
            return _jdump({
                "ok": True,
                "ca_bundle_path": ca_bundle,
                "exists": ca_path.exists(),
                "size_bytes": ca_path.stat().st_size if ca_path.exists() else None,
            })
        except Exception as e:
            return _jdump({"ok": False, "error": str(e)})

    registry.register(ToolSpec(
        name="check_ca_bundle",
        description="Check CA certificate bundle location and status.",
        pack="ca_tools",
        tags=["security", "certificates"],
        parameters={"type": "object", "properties": {}, "required": []},
        handler=check_ca_bundle_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    def fetch_peer_cert_handler(params: Dict[str, Any]) -> str:
        host = str(params.get("host") or "").strip()
        port = _safe_int(params.get("port"), 443)
        timeout = float(params.get("timeout_seconds") or 5.0)
        if not host:
            return _jdump({"error": "Missing host"})
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with socket.create_connection((host, port), timeout=timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                    cert = ssock.getpeercert()
                    return _jdump({"ok": True, "host": host, "port": port, "cert": cert, "tls_version": ssock.version()})
        except Exception as e:
            return _jdump({"ok": False, "error": str(e)})

    registry.register(ToolSpec(
        name="fetch_peer_cert",
        description="Fetch TLS certificate from a remote host.",
        pack="ca_tools",
        tags=["security", "certificates"],
        parameters={"type": "object", "properties": {"host": {"type": "string"}, "port": {"type": "integer"}, "timeout_seconds": {"type": "number"}}, "required": ["host"]},
        handler=fetch_peer_cert_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))
