"""
Google OAuth 2.0 web-flow credential manager for EOS.

Replaces the CLI-only InstalledAppFlow.run_local_server() pattern used in
tools/calendar.py with a proper server-side flow that works inside a FastAPI
process.

Flow
----
1. Call  build_authorize_url(scopes, redirect_uri)  → returns (auth_url, state)
2. Redirect the user to auth_url (the admin panel opens it in a new tab)
3. Google redirects to  /api/google_workspace/callback?code=...&state=...
4. Call  exchange_code(code, state, redirect_uri)  → stores token, returns account info
5. Future API calls use  get_credentials(scopes)  which auto-refreshes when expired

Token storage
-------------
Credentials are serialised as JSON and stored in  data/google_token.json
(path overridden by config key  google.token_path).  The file is created with
0600 permissions on POSIX.  On Windows the file is stored normally — use
disk-level encryption or Credential Manager for additional protection.

The client-secrets file path is read from  google.client_secret_path  in the
loaded config.  The path may use globs; the first match is used.

Account information
-------------------
After authorisation, the user's email and display name are fetched from the
Google OAuth UserInfo endpoint and cached in memory for the process lifetime.
"""
from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── In-memory state ───────────────────────────────────────────────────────────

# Pending OAuth states: state_token → {"redirect_uri": str, "scopes": list, "created_at": float}
_pending_states: dict[str, dict] = {}

# Cached account info (populated after first successful exchange)
_account_info: dict = {}

# Config reference (set at startup via configure())
_cfg: dict = {}

# ── Default scopes ────────────────────────────────────────────────────────────

DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "openid",
    "email",
    "profile",
]


# ── Configuration ─────────────────────────────────────────────────────────────

def configure(cfg: dict) -> None:
    """Inject the loaded config dict.  Call once at startup."""
    global _cfg
    _cfg = cfg


def _token_path() -> Path:
    gcfg = _cfg.get("google", {})
    return Path(gcfg.get("token_path", "data/google_token.json"))


def _client_secret_path() -> Optional[Path]:
    gcfg = _cfg.get("google", {})
    pattern = gcfg.get(
        "client_secret_path",
        gcfg.get("client_secret_glob", "config/google/client_secret*.json"),
    )
    matches = glob.glob(str(pattern))
    if matches:
        return Path(matches[0])
    # Also check the old location used by tools/calendar.py
    legacy = glob.glob("AI personal files/client_secret_*.json")
    if legacy:
        return Path(legacy[0])
    return None


# ── Credential helpers ────────────────────────────────────────────────────────

def _import_google() -> tuple:
    """Import Google auth libraries; raise ImportError with install hint on failure."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import Flow
        from googleapiclient.discovery import build
        return Credentials, Request, Flow, build
    except ImportError as exc:
        raise ImportError(
            "Google auth libraries not installed.  "
            "Run: pip install google-auth google-auth-oauthlib google-api-python-client"
        ) from exc


def get_credentials(scopes: list[str] | None = None) -> Optional[object]:
    """Return valid Google Credentials, refreshing if necessary.

    Returns None if no token has been stored yet (user must authorise first).
    """
    Credentials, Request, _, _ = _import_google()
    scopes = scopes or DEFAULT_SCOPES
    tp = _token_path()

    if not tp.exists():
        return None

    try:
        creds = Credentials.from_authorized_user_file(str(tp), scopes)
    except Exception as exc:
        logger.warning("[google_oauth] Failed to load token file: %s", exc)
        return None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_credentials(creds)
            logger.info("[google_oauth] Credentials refreshed")
            return creds
        except Exception as exc:
            logger.warning("[google_oauth] Token refresh failed: %s", exc)
            return None

    return None


def _save_credentials(creds: object) -> None:
    """Persist credentials to disk with restrictive permissions."""
    tp = _token_path()
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(str(tp), 0o600)
    except (AttributeError, NotImplementedError):
        pass  # Windows — filesystem-level protection required separately


def is_authorized() -> bool:
    """Return True if valid (or refreshable) credentials exist."""
    return get_credentials() is not None


# ── Authorization URL ─────────────────────────────────────────────────────────

def build_authorize_url(
    redirect_uri: str,
    scopes: list[str] | None = None,
) -> tuple[str, str]:
    """Build the Google OAuth authorization URL.

    Parameters
    ----------
    redirect_uri
        The URL Google will redirect to after authorization.
        Should be  http://localhost:<port>/api/google_workspace/callback
    scopes
        OAuth scopes to request.  Defaults to DEFAULT_SCOPES.

    Returns
    -------
    (auth_url, state)
        *auth_url*  — open this in the browser to start the OAuth flow.
        *state*     — opaque token; pass to exchange_code() for validation.

    Raises
    ------
    FileNotFoundError
        If no client_secret JSON file is found.
    ImportError
        If Google auth libraries are not installed.
    """
    Credentials, _, Flow, _ = _import_google()
    scopes = scopes or DEFAULT_SCOPES

    secret_path = _client_secret_path()
    if secret_path is None:
        raise FileNotFoundError(
            "Google OAuth client secret file not found.  "
            "Place client_secret_*.json in config/google/ and set "
            "google.client_secret_path in config.json."
        )

    state = secrets.token_urlsafe(32)

    flow = Flow.from_client_secrets_file(
        str(secret_path),
        scopes=scopes,
        redirect_uri=redirect_uri,
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",          # ensure refresh_token is always returned
        state=state,
    )

    _pending_states[state] = {
        "redirect_uri": redirect_uri,
        "scopes": scopes,
        "created_at": time.time(),
    }
    # Expire pending states older than 10 minutes
    _prune_pending_states()

    logger.info("[google_oauth] Authorization URL generated (state=%s…)", state[:8])
    return auth_url, state


def _prune_pending_states(max_age_seconds: float = 600) -> None:
    cutoff = time.time() - max_age_seconds
    expired = [s for s, v in _pending_states.items() if v["created_at"] < cutoff]
    for s in expired:
        del _pending_states[s]


# ── Code exchange (OAuth callback) ────────────────────────────────────────────

def exchange_code(
    code: str,
    state: str,
    redirect_uri: str,
) -> dict:
    """Exchange an authorization code for tokens and persist them.

    Parameters
    ----------
    code
        Authorization code returned by Google in the callback.
    state
        State token from the callback; must match a pending state.
    redirect_uri
        Must exactly match the redirect_uri used to generate the auth URL.

    Returns
    -------
    dict
        {"ok": True, "account": {...}}  on success.
        {"ok": False, "error": "..."}  on failure.
    """
    Credentials, _, Flow, _ = _import_google()

    pending = _pending_states.pop(state, None)
    if pending is None:
        return {"ok": False, "error": "Invalid or expired OAuth state token"}

    # Guard against redirect_uri mismatch attacks
    if pending["redirect_uri"] != redirect_uri:
        return {"ok": False, "error": "redirect_uri mismatch — possible CSRF attack"}

    secret_path = _client_secret_path()
    if secret_path is None:
        return {"ok": False, "error": "Client secret file not found"}

    try:
        flow = Flow.from_client_secrets_file(
            str(secret_path),
            scopes=pending["scopes"],
            redirect_uri=redirect_uri,
            state=state,
        )
        flow.fetch_token(code=code)
        creds = flow.credentials
    except Exception as exc:
        logger.error("[google_oauth] Token exchange failed: %s", exc)
        return {"ok": False, "error": f"Token exchange failed: {exc}"}

    _save_credentials(creds)
    logger.info("[google_oauth] Credentials saved successfully")

    # Fetch and cache account info
    account = _fetch_account_info(creds)
    return {"ok": True, "account": account}


# ── Account info ──────────────────────────────────────────────────────────────

def _fetch_account_info(creds: object) -> dict:
    """Fetch the authenticated user's profile from the OAuth UserInfo endpoint."""
    global _account_info
    try:
        import httpx as _httpx
        resp = _httpx.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {creds.token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            _account_info = {
                "email":        data.get("email", ""),
                "name":         data.get("name", ""),
                "picture":      data.get("picture", ""),
                "hd":           data.get("hd", ""),    # hosted domain (Workspace)
            }
            logger.info("[google_oauth] Account: %s", _account_info.get("email"))
        else:
            _account_info = {"error": f"UserInfo HTTP {resp.status_code}"}
    except Exception as exc:
        logger.warning("[google_oauth] Could not fetch account info: %s", exc)
        _account_info = {"error": str(exc)}
    return _account_info


def get_account_info() -> dict:
    """Return cached account info, refreshing if credentials are valid and cache is empty."""
    global _account_info
    if _account_info:
        return _account_info
    creds = get_credentials()
    if creds is None:
        return {}
    return _fetch_account_info(creds)


# ── Revocation ────────────────────────────────────────────────────────────────

def revoke() -> dict:
    """Revoke the current Google token and delete the local token file.

    Returns {"ok": True} on success, {"ok": False, "error": "..."} on failure.
    """
    global _account_info
    tp = _token_path()
    revoked_remotely = False

    # Try remote revocation
    creds = get_credentials()
    if creds is not None:
        try:
            import httpx as _httpx
            token_to_revoke = getattr(creds, "refresh_token", None) or getattr(creds, "token", None)
            if token_to_revoke:
                resp = _httpx.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={"token": token_to_revoke},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=10,
                )
                revoked_remotely = resp.status_code == 200
                if not revoked_remotely:
                    logger.warning(
                        "[google_oauth] Remote revocation returned HTTP %s", resp.status_code
                    )
        except Exception as exc:
            logger.warning("[google_oauth] Remote revocation failed: %s", exc)

    # Always delete local token file
    if tp.exists():
        try:
            tp.unlink()
            logger.info("[google_oauth] Token file deleted: %s", tp)
        except Exception as exc:
            logger.error("[google_oauth] Could not delete token file: %s", exc)
            return {"ok": False, "error": str(exc)}

    _account_info = {}
    logger.info("[google_oauth] Access revoked (remote_revocation=%s)", revoked_remotely)
    return {"ok": True, "revoked_remotely": revoked_remotely}


# ── Build API service clients ─────────────────────────────────────────────────

def build_service(service_name: str, version: str, scopes: list[str] | None = None):
    """Build and return a Google API service client.

    Parameters
    ----------
    service_name
        E.g. "calendar", "gmail", "drive"
    version
        E.g. "v3", "v1"
    scopes
        Subset of DEFAULT_SCOPES required for this service.

    Returns the resource object from googleapiclient.discovery.build,
    or raises if not authorized or libraries not installed.
    """
    _, _, _, build_fn = _import_google()
    creds = get_credentials(scopes)
    if creds is None:
        raise PermissionError(
            "Google credentials not available — "
            "authorize via /api/google_workspace/authorize first"
        )
    return build_fn(service_name, version, credentials=creds)
