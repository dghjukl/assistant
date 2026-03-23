"""
Admin authentication for EOS WebUI.

A single admin token is generated at first startup and stored in
data/admin_token.txt. All /admin/* API routes require this token via
X-Admin-Token header or Authorization: Bearer <token>.

The token file is created with a warning comment on first run. To
rotate the token, delete the file and restart.

Environment override: set EOS_ADMIN_TOKEN to bypass the file.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import Header, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Token storage — written alongside the databases
_TOKEN_FILE_NAME = "admin_token.txt"
_ENV_VAR = "EOS_ADMIN_TOKEN"

_admin_token: str = ""
_token_file: Path = Path("data") / _TOKEN_FILE_NAME  # overridden by load_or_create_token


def load_or_create_token(data_dir: Path | str | None = None) -> str:
    """Load an existing admin token or generate and persist a new one.

    Call once at server startup.  Returns the active token.

    Priority:
      1. EOS_ADMIN_TOKEN environment variable
      2. data/admin_token.txt
      3. Generate a new cryptographically random token and write it
    """
    global _admin_token, _token_file

    if data_dir is not None:
        _token_file = Path(data_dir) / _TOKEN_FILE_NAME
    else:
        # Resolve relative to the EOS package root (two levels up from this file)
        _token_file = Path(__file__).parent.parent / "data" / _TOKEN_FILE_NAME

    # 1. Environment override
    env_token = os.environ.get(_ENV_VAR, "").strip()
    if env_token:
        _admin_token = env_token
        logger.info("[auth] Admin token loaded from environment variable %s", _ENV_VAR)
        return _admin_token

    # 2. Existing token file
    if _token_file.is_file():
        raw = _token_file.read_text(encoding="utf-8").strip()
        # Strip any comment lines (lines starting with #)
        lines = [ln for ln in raw.splitlines() if ln and not ln.startswith("#")]
        if lines:
            _admin_token = lines[0].strip()
            logger.info("[auth] Admin token loaded from %s", _token_file)
            return _admin_token

    # 3. Generate new token
    _admin_token = secrets.token_urlsafe(32)
    _token_file.parent.mkdir(parents=True, exist_ok=True)
    _token_file.write_text(
        "# EOS admin token — keep this file private\n"
        "# To rotate: delete this file and restart EOS\n"
        f"{_admin_token}\n",
        encoding="utf-8",
    )
    logger.warning(
        "[auth] NEW admin token generated and saved to: %s — "
        "copy this token to access the admin panel",
        _token_file,
    )
    return _admin_token


def get_admin_token() -> str:
    """Return the currently active admin token."""
    return _admin_token


def get_token_file_path() -> Path:
    """Return the path where the token is stored."""
    return _token_file


def _admin_origin_allowed(request: Request) -> bool:
    """Return True when the request origin tier is allowed to use /admin routes."""
    try:
        from core.access_control import (
            TIER_LOCALHOST,
            classify_origin,
            extract_client_ip,
            get_access_controller,
        )

        tier = getattr(request.state, "origin_tier", None) or classify_origin(extract_client_ip(request))
        if tier == TIER_LOCALHOST:
            return True

        ctrl = get_access_controller()
        if ctrl is None:
            return True
        return bool(ctrl.policies.get(tier).admin_enabled)
    except Exception as exc:
        logger.debug("[auth] Admin origin-tier check fallback due to error: %s", exc)
        return True


# ── FastAPI dependency ──────────────────────────────────────────────────────

def require_admin(
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
    authorization: Optional[str] = Header(None),
) -> str:
    """FastAPI dependency — raise 401 if valid admin token not present."""
    token = x_admin_token
    if not token and authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()

    if not token or not _admin_token or token != _admin_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


# ── Starlette middleware ────────────────────────────────────────────────────

class AdminAuthMiddleware(BaseHTTPMiddleware):
    """Protect all /admin/* routes with the admin token.

    Exempt paths:
    - GET /admin          — HTML page (JS handles auth client-side)
    - GET /api/auth/verify — token-check endpoint used by the UI

    WebSocket connections to /admin/ws must pass the token as a
    ?token=<value> query parameter since the WS API doesn't support
    custom headers.
    """

    _EXEMPT = {"/admin", "/api/auth/verify"}

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Only gate /admin/* paths
        if not path.startswith("/admin"):
            return await call_next(request)

        # Exempt: the HTML page itself
        if path in self._EXEMPT and request.method == "GET":
            return await call_next(request)

        # WebSocket upgrade: check ?token= query param
        if request.headers.get("upgrade", "").lower() == "websocket":
            token = request.query_params.get("token", "")
            from starlette.responses import Response

            if not token or token != _admin_token:
                return Response("Admin token required", status_code=403)
            if not _admin_origin_allowed(request):
                return Response("Admin access from this origin is disabled", status_code=403)
            return await call_next(request)

        # HTTP: check X-Admin-Token or Authorization: Bearer
        token = request.headers.get("X-Admin-Token", "")
        if not token:
            auth = request.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()

        if not token or not _admin_token or token != _admin_token:
            return JSONResponse(
                {"ok": False, "error": "Admin authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        if not _admin_origin_allowed(request):
            return JSONResponse(
                {"ok": False, "error": "Admin access from this origin is disabled"},
                status_code=403,
            )

        return await call_next(request)
