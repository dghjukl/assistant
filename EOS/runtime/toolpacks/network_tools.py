"""Network Tools — DNS, TCP, TLS diagnostics

Provides network-level diagnostics without external dependencies.
All operations are read-only.
"""

from __future__ import annotations

import json
import socket
import ssl
import time
from typing import Any, Dict
from urllib.parse import urlparse


def _jdump(x: Any) -> str:
    try:
        return json.dumps(x, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


def _safe_float(x: Any, default: float) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _parse_host_port(url_or_host: str, default_port: int) -> tuple[str, int]:
    """Extract host and port from URL or host:port string."""
    s = (url_or_host or "").strip()
    if not s:
        return "", default_port
    if "://" in s:
        u = urlparse(s)
        host = u.hostname or ""
        port = int(u.port or default_port)
        return host, port
    if ":" in s and s.count(":") == 1:
        h, p = s.split(":", 1)
        try:
            return h.strip(), int(p.strip())
        except Exception:
            return s, default_port
    return s, default_port


def register(registry: Any, config: Dict[str, Any]) -> None:
    """Register network diagnostic tools."""
    from runtime.tool_registry import (
        ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy
    )

    net_cfg = config.get("network_tools", {}) if isinstance(config, dict) else {}
    enabled = bool(net_cfg.get("enabled", True))

    def dns_lookup_handler(params: Dict[str, Any]) -> str:
        host = str(params.get("host") or "").strip()
        if not host:
            return _jdump({"error": "host is required"})

        record_type = str(params.get("record_type") or "A").upper()
        if record_type not in ("A", "AAAA", "CNAME"):
            record_type = "A"

        out: Dict[str, Any] = {"host": host, "record_type": record_type, "addresses": []}
        family = socket.AF_UNSPEC
        if record_type == "A":
            family = socket.AF_INET
        elif record_type == "AAAA":
            family = socket.AF_INET6

        addrs = set()
        try:
            for res in socket.getaddrinfo(host, None, family, socket.SOCK_STREAM):
                addr = res[4][0]
                addrs.add(addr)
        except Exception as e:
            out["error"] = str(e)
            return _jdump(out)

        out["addresses"] = sorted(addrs)
        return _jdump(out)

    registry.register(ToolSpec(
        name="dns_lookup",
        description="Resolve hostname to IP address.",
        pack="network_tools",
        tags=["network", "web"],
        parameters={
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "record_type": {"type": "string", "enum": ["A", "AAAA", "CNAME"]},
            },
            "required": ["host"],
        },
        handler=dns_lookup_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    def tcp_connect_handler(params: Dict[str, Any]) -> str:
        host_or_url = str(params.get("host") or "").strip()
        if not host_or_url:
            return _jdump({"error": "host is required"})

        default_port = 80
        if "https://" in host_or_url:
            default_port = 443
        host, port = _parse_host_port(host_or_url, default_port)

        timeout_s = _safe_float(params.get("timeout_seconds"), 5.0)
        timeout_s = max(1.0, min(timeout_s, 30.0))

        start = time.perf_counter()
        out: Dict[str, Any] = {"host": host, "port": port, "timeout_seconds": timeout_s}

        try:
            with socket.create_connection((host, port), timeout=timeout_s):
                out["ok"] = True
        except Exception as e:
            out["ok"] = False
            out["error"] = str(e)

        out["duration_ms"] = int((time.perf_counter() - start) * 1000)
        return _jdump(out)

    registry.register(ToolSpec(
        name="tcp_connect",
        description="Test TCP connectivity to a host:port.",
        pack="network_tools",
        tags=["network", "web"],
        parameters={
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "timeout_seconds": {"type": "number"},
            },
            "required": ["host"],
        },
        handler=tcp_connect_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    def tls_probe_handler(params: Dict[str, Any]) -> str:
        host_or_url = str(params.get("host") or "").strip()
        if not host_or_url:
            return _jdump({"error": "host is required"})

        host, port = _parse_host_port(host_or_url, 443)

        timeout_s = _safe_float(params.get("timeout_seconds"), 10.0)
        timeout_s = max(1.0, min(timeout_s, 30.0))

        start = time.perf_counter()
        out: Dict[str, Any] = {"host": host, "port": port, "timeout_seconds": timeout_s}

        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=timeout_s) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                    cert = ssock.getpeercert()
                    out["ok"] = True
                    out["tls_version"] = ssock.version()
                    try:
                        out["cipher"] = ssock.cipher()[0] if ssock.cipher() else None
                    except Exception:
                        out["cipher"] = None
                    if cert:
                        out["cert_subject"] = dict(cert.get("subject", []))
                        out["cert_issuer"] = dict(cert.get("issuer", []))
                        out["cert_notAfter"] = cert.get("notAfter")
        except Exception as e:
            out["ok"] = False
            out["error"] = str(e)

        out["duration_ms"] = int((time.perf_counter() - start) * 1000)
        return _jdump(out)

    registry.register(ToolSpec(
        name="tls_probe",
        description="Probe TLS/SSL certificate and version on host:port.",
        pack="network_tools",
        tags=["network", "web"],
        parameters={
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "timeout_seconds": {"type": "number"},
            },
            "required": ["host"],
        },
        handler=tls_probe_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))
