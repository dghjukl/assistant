"""Google Workspace Tools — Calendar, Gmail, Drive integration

Configuration:
  google_workspace:
    enabled: false
    client_secret_path: config/google/client_secret.json
"""

from __future__ import annotations

import json
from typing import Any, Dict


def _jdump(x: Any) -> str:
    try:
        return json.dumps(x, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


def register(registry: Any, config: Dict[str, Any]) -> None:
    from runtime.tool_registry import ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy

    gw_cfg = config.get("google_workspace", {}) if isinstance(config, dict) else {}
    enabled = bool(gw_cfg.get("enabled", False))

    def google_placeholder_handler(params: Dict[str, Any]) -> str:
        return _jdump({
            "error": "Google Workspace tools not configured",
            "hint": "Install: pip install google-api-python-client google-auth-oauthlib google-auth-httplib2",
            "docs": "See config/google/README.md for setup"
        })

    # Try to import Google libraries
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.service_account import Credentials
        google_available = True
    except ImportError:
        google_available = False

    # Register placeholder tools when Google libs unavailable
    if not google_available or not enabled:
        registry.register(ToolSpec(
            name="list_calendar_events",
            description="List Google Calendar events.",
            pack="google_tools",
            tags=["google", "calendar"],
            parameters={"type": "object", "properties": {}, "required": []},
            handler=google_placeholder_handler,
            risk_level=ToolRiskLevel.READ_ONLY,
            trust_level=ToolTrustLevel.VERIFIED_USER,
            confirmation_policy=ConfirmationPolicy.NONE,
            enabled=False,
        ))

        registry.register(ToolSpec(
            name="search_gmail",
            description="Search Gmail messages.",
            pack="google_tools",
            tags=["google", "gmail"],
            parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            handler=google_placeholder_handler,
            risk_level=ToolRiskLevel.READ_ONLY,
            trust_level=ToolTrustLevel.VERIFIED_USER,
            confirmation_policy=ConfirmationPolicy.NONE,
            enabled=False,
        ))

        registry.register(ToolSpec(
            name="list_drive_files",
            description="List Google Drive files.",
            pack="google_tools",
            tags=["google", "drive"],
            parameters={"type": "object", "properties": {}, "required": []},
            handler=google_placeholder_handler,
            risk_level=ToolRiskLevel.READ_ONLY,
            trust_level=ToolTrustLevel.VERIFIED_USER,
            confirmation_policy=ConfirmationPolicy.NONE,
            enabled=False,
        ))
