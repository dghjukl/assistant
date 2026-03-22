"""
EOS access control — origin classification, per-tier policy enforcement,
LAN session management, and per-tier rate limiting.

Three access tiers (highest → lowest trust):
  localhost — loopback (127.x.x.x, ::1)
  lan       — RFC 1918 private + link-local addresses
  external  — all other routable addresses

Policy per tier:
  enabled          — whether this tier may connect at all
  chat_enabled     — whether /api/chat (and public API) is accessible
  admin_enabled    — whether /admin/* paths are reachable (admin token still required)
  require_auth     — whether a LAN session token is required for non-admin routes
  rate_limit_rpm   — requests per minute (0 = unlimited)
  rate_limit_burst — burst tolerance above steady-state rate
  session_ttl_sec  — LAN session token lifetime in seconds

Auth flow for LAN clients (when require_auth=true):
  1. Operator generates a one-time pairing code from the admin panel.
  2. LAN device POSTs the code to  POST /api/auth/lan/pair  → receives session token.
  3. Device includes token as  X-Lan-Token: <token>  (or Authorization: Bearer <token>)
     on all subsequent requests to /api/* routes.
  4. Token expires after session_ttl_sec seconds.  Operator can revoke at any time.

Localhost is always unrestricted (no auth required, no rate limit by default).
Admin routes (/admin/*) are gated solely by the admin token regardless of tier.
"""
from __future__ import annotations

import collections
import ipaddress
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Deque, Dict, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

TIER_LOCALHOST = "localhost"
TIER_LAN       = "lan"
TIER_EXTERNAL  = "external"

_LAN_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),   # IPv4 link-local
    ipaddress.ip_network("fc00::/7"),           # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),          # IPv6 link-local
]

# Paths exempt from LAN session auth checks
_AUTH_EXEMPT_PREFIXES = (
    "/admin",           # handled by AdminAuthMiddleware
    "/api/auth/lan",    # the auth endpoints themselves
    "/api/auth/verify", # admin token verification
)
_AUTH_EXEMPT_EXACT = {"/", "/index.html", "/admin"}

# State file for runtime tier overrides
_TIERS_FILE_NAME = "access_tiers.json"

# ── IP classification ─────────────────────────────────────────────────────────


def classify_origin(ip_str: str) -> str:
    """Return the access tier for *ip_str*.

    Returns one of  TIER_LOCALHOST / TIER_LAN / TIER_EXTERNAL.
    Unknown / unparseable addresses are treated as TIER_EXTERNAL.
    """
    try:
        addr = ipaddress.ip_address(ip_str.split("%")[0])  # strip zone-id
        if addr.is_loopback:
            return TIER_LOCALHOST
        for net in _LAN_NETWORKS:
            if addr in net:
                return TIER_LAN
        return TIER_EXTERNAL
    except (ValueError, AttributeError):
        logger.debug("[access_ctrl] Could not parse IP %r — treating as external", ip_str)
        return TIER_EXTERNAL


def extract_client_ip(request: Request) -> str:
    """Extract the real client IP from the request.

    Trusts X-Real-IP / X-Forwarded-For only when EOS_TRUST_PROXY=1 is set,
    which is appropriate when running behind a trusted reverse proxy on the
    same machine.  In all other cases the direct connection address is used.
    """
    trust_proxy = os.environ.get("EOS_TRUST_PROXY", "").strip() == "1"
    if trust_proxy:
        real_ip = request.headers.get("X-Real-IP", "").strip()
        if real_ip:
            return real_ip
        forwarded = request.headers.get("X-Forwarded-For", "").strip()
        if forwarded:
            return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host or "127.0.0.1"
    return "127.0.0.1"


# ── Policy dataclass ──────────────────────────────────────────────────────────


@dataclass
class TierPolicy:
    """Access policy for a single origin tier."""

    enabled: bool          = True
    chat_enabled: bool     = True
    admin_enabled: bool    = True
    require_auth: bool     = False
    rate_limit_rpm: int    = 0       # 0 = unlimited
    rate_limit_burst: int  = 10
    session_ttl_sec: int   = 86_400  # 24 hours

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TierPolicy":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


_DEFAULTS: Dict[str, TierPolicy] = {
    TIER_LOCALHOST: TierPolicy(
        enabled=True,
        chat_enabled=True,
        admin_enabled=True,
        require_auth=False,
        rate_limit_rpm=0,
        rate_limit_burst=0,
        session_ttl_sec=86_400,
    ),
    TIER_LAN: TierPolicy(
        enabled=True,
        chat_enabled=True,
        admin_enabled=True,
        require_auth=True,
        rate_limit_rpm=60,
        rate_limit_burst=15,
        session_ttl_sec=86_400,
    ),
    TIER_EXTERNAL: TierPolicy(
        enabled=False,
        chat_enabled=False,
        admin_enabled=False,
        require_auth=True,
        rate_limit_rpm=20,
        rate_limit_burst=5,
        session_ttl_sec=3_600,
    ),
}


# ── Policy store ─────────────────────────────────────────────────────────────


class PolicyStore:
    """Load, persist, and provide tier policies.

    Policies are merged from three sources (last wins):
      1. Built-in defaults
      2. config.json  access_tiers  block
      3. data/access_tiers.json  (runtime overrides written by admin API)
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._file = data_dir / _TIERS_FILE_NAME
        self._policies: Dict[str, TierPolicy] = {
            k: TierPolicy(**asdict(v)) for k, v in _DEFAULTS.items()
        }

    def load_from_config(self, cfg: dict) -> None:
        """Overlay defaults with values from the config dict."""
        tiers_cfg = cfg.get("access_tiers", {})
        for tier, raw in tiers_cfg.items():
            if tier not in _DEFAULTS:
                continue
            if not isinstance(raw, dict):
                continue
            base = asdict(self._policies.get(tier, _DEFAULTS[tier]))
            base.update({k: v for k, v in raw.items() if k in base})
            self._policies[tier] = TierPolicy.from_dict(base)
        logger.info("[access_ctrl] Tier policies loaded from config.")

    def load_runtime_overrides(self) -> None:
        """Apply any saved runtime overrides from data/access_tiers.json."""
        if not self._file.is_file():
            return
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
            for tier, overrides in raw.items():
                if tier not in self._policies or not isinstance(overrides, dict):
                    continue
                current = asdict(self._policies[tier])
                current.update({k: v for k, v in overrides.items() if k in current})
                self._policies[tier] = TierPolicy.from_dict(current)
            logger.info("[access_ctrl] Runtime tier overrides loaded from %s", self._file)
        except Exception as exc:
            logger.warning("[access_ctrl] Could not load runtime overrides: %s", exc)

    def save_runtime_overrides(self) -> None:
        """Persist current policies to data/access_tiers.json."""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            payload = {tier: asdict(policy) for tier, policy in self._policies.items()}
            self._file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.error("[access_ctrl] Could not save runtime overrides: %s", exc)

    def get(self, tier: str) -> TierPolicy:
        return self._policies.get(tier, _DEFAULTS.get(tier, _DEFAULTS[TIER_EXTERNAL]))

    def update(self, tier: str, updates: dict) -> TierPolicy:
        """Apply partial updates to a tier policy and persist."""
        if tier not in self._policies:
            raise KeyError(f"Unknown tier: {tier!r}")
        current = asdict(self._policies[tier])
        current.update({k: v for k, v in updates.items() if k in current})
        self._policies[tier] = TierPolicy.from_dict(current)
        self.save_runtime_overrides()
        return self._policies[tier]

    def all_policies(self) -> Dict[str, dict]:
        return {tier: asdict(policy) for tier, policy in self._policies.items()}


# ── Rate limiter ─────────────────────────────────────────────────────────────


class _ClientWindow:
    """Per-client sliding 60-second request window."""
    __slots__ = ("_ts",)

    def __init__(self) -> None:
        self._ts: Deque[float] = collections.deque()

    def is_allowed(self, rpm: int, burst: int) -> bool:
        if rpm == 0:
            return True  # unlimited
        now = time.monotonic()
        cutoff = now - 60.0
        while self._ts and self._ts[0] < cutoff:
            self._ts.popleft()
        if len(self._ts) >= rpm + burst:
            return False
        self._ts.append(now)
        return True


class RateLimiter:
    """Per-IP rate limiter with per-tier settings."""

    def __init__(self) -> None:
        # (tier, ip) → _ClientWindow
        self._windows: Dict[tuple, _ClientWindow] = {}
        self._last_prune: float = 0.0

    def _prune(self) -> None:
        now = time.monotonic()
        if now - self._last_prune < 120:
            return
        cutoff = now - 120.0
        stale = [k for k, w in self._windows.items()
                 if not w._ts or w._ts[-1] < cutoff]
        for k in stale:
            del self._windows[k]
        self._last_prune = now

    def is_allowed(self, tier: str, ip: str, policy: TierPolicy) -> bool:
        if policy.rate_limit_rpm == 0:
            return True
        self._prune()
        key = (tier, ip)
        if key not in self._windows:
            self._windows[key] = _ClientWindow()
        return self._windows[key].is_allowed(policy.rate_limit_rpm, policy.rate_limit_burst)

    def client_count(self) -> int:
        return len(self._windows)


# ── Session store ─────────────────────────────────────────────────────────────


@dataclass
class LanSession:
    token: str
    client_ip: str
    created_at: float
    expires_at: float
    label: str = ""

    @property
    def is_valid(self) -> bool:
        return time.time() < self.expires_at

    def to_dict(self) -> dict:
        return {
            "token_prefix": self.token[:8] + "…",
            "client_ip":    self.client_ip,
            "created_at":   self.created_at,
            "expires_at":   self.expires_at,
            "label":        self.label,
            "valid":        self.is_valid,
        }


class SessionStore:
    """LAN session token management.  Tokens are stored in data/lan_sessions.json."""

    _FILE = "lan_sessions.json"

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._sessions: Dict[str, LanSession] = {}
        self._load()

    def _load(self) -> None:
        path = self._data_dir / self._FILE
        if not path.is_file():
            return
        try:
            raw: list = json.loads(path.read_text(encoding="utf-8"))
            now = time.time()
            for item in raw:
                if not isinstance(item, dict):
                    continue
                s = LanSession(**item)
                if s.is_valid:
                    self._sessions[s.token] = s
            logger.info("[access_ctrl] Loaded %d active LAN sessions", len(self._sessions))
        except Exception as exc:
            logger.warning("[access_ctrl] Could not load LAN sessions: %s", exc)

    def _save(self) -> None:
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            active = [asdict(s) for s in self._sessions.values() if s.is_valid]
            (self._data_dir / self._FILE).write_text(
                json.dumps(active, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.error("[access_ctrl] Could not save LAN sessions: %s", exc)

    def create(self, client_ip: str, ttl_sec: int, label: str = "") -> LanSession:
        """Create and persist a new session token."""
        self._prune()
        token = secrets.token_urlsafe(32)
        now = time.time()
        session = LanSession(
            token=token,
            client_ip=client_ip,
            created_at=now,
            expires_at=now + ttl_sec,
            label=label,
        )
        self._sessions[token] = session
        self._save()
        return session

    def validate(self, token: str) -> Optional[LanSession]:
        """Return a valid session for *token*, or None."""
        s = self._sessions.get(token)
        if s is None or not s.is_valid:
            return None
        return s

    def revoke(self, token: str) -> bool:
        """Revoke a session. Returns True if it existed."""
        existed = token in self._sessions
        self._sessions.pop(token, None)
        if existed:
            self._save()
        return existed

    def list_sessions(self) -> list:
        self._prune()
        return [s.to_dict() for s in self._sessions.values()]

    def _prune(self) -> None:
        stale = [t for t, s in self._sessions.items() if not s.is_valid]
        for t in stale:
            del self._sessions[t]

    def active_count(self) -> int:
        self._prune()
        return len(self._sessions)


# ── Pairing store ─────────────────────────────────────────────────────────────


class PairingStore:
    """One-time pairing codes for LAN device registration.

    Codes are generated by the admin panel, consumed once by a LAN device,
    and expire after *ttl_sec* seconds (default 5 minutes).
    """

    _TTL = 300  # 5 minutes

    def __init__(self) -> None:
        self._codes: Dict[str, float] = {}  # code → expires_at

    def generate(self) -> str:
        self._prune()
        code = secrets.token_urlsafe(16)
        self._codes[code] = time.time() + self._TTL
        return code

    def consume(self, code: str) -> bool:
        """Validate and consume a pairing code. Returns True on success."""
        self._prune()
        if code not in self._codes:
            return False
        del self._codes[code]
        return True

    def _prune(self) -> None:
        now = time.time()
        stale = [c for c, exp in self._codes.items() if now >= exp]
        for c in stale:
            del self._codes[c]

    def pending_count(self) -> int:
        self._prune()
        return len(self._codes)


# ── Main controller ──────────────────────────────────────────────────────────


class AccessController:
    """Singleton that wires together the policy store, rate limiter,
    session store, and pairing store."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.policies = PolicyStore(data_dir)
        self.rate_limiter = RateLimiter()
        self.sessions = SessionStore(data_dir)
        self.pairing = PairingStore()

    def load(self, cfg: dict) -> None:
        """Apply config and runtime overrides.  Call once at startup."""
        self.policies.load_from_config(cfg)
        self.policies.load_runtime_overrides()

    def check_access(
        self,
        tier: str,
        client_ip: str,
        path: str,
        lan_token: Optional[str],
    ) -> Optional[JSONResponse]:
        """Return a JSONResponse error if the request should be blocked,
        or None if it should be allowed through.

        Checks (in order):
          1. Tier enabled
          2. Per-path capability (chat, admin)
          3. Rate limit
          4. LAN session auth (for non-localhost tiers with require_auth=True)
        """
        policy = self.policies.get(tier)

        # 1. Tier enabled
        if not policy.enabled:
            return JSONResponse(
                {"ok": False, "error": f"Access from {tier} is disabled"},
                status_code=403,
            )

        # 2. Path-level capability checks (skip for localhost)
        if tier != TIER_LOCALHOST:
            is_admin = path.startswith("/admin")
            is_chat_api = (
                path.startswith("/api/") and not path.startswith("/api/auth/lan")
            )
            if is_admin and not policy.admin_enabled:
                return JSONResponse(
                    {"ok": False, "error": f"Admin access from {tier} is disabled"},
                    status_code=403,
                )
            # Note: non-admin, non-API paths (static HTML) are always allowed

        # 3. Rate limit
        if not self.rate_limiter.is_allowed(tier, client_ip, policy):
            return JSONResponse(
                {"ok": False, "error": "Rate limit exceeded", "tier": tier},
                status_code=429,
                headers={"Retry-After": "60"},
            )

        # 4. LAN session auth — only for non-localhost tiers that require it,
        #    and only for routes that are not /admin/* (those are handled by
        #    AdminAuthMiddleware) and not the auth endpoints themselves.
        if tier != TIER_LOCALHOST and policy.require_auth:
            is_exempt = (
                path in _AUTH_EXEMPT_EXACT
                or any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES)
                or path.endswith(".html")
                or path.endswith(".js")
                or path.endswith(".css")
            )
            if not is_exempt:
                if lan_token is None or self.sessions.validate(lan_token) is None:
                    return JSONResponse(
                        {"ok": False, "error": "LAN session token required",
                         "hint": "POST /api/auth/lan/pair with a pairing code"},
                        status_code=401,
                        headers={"WWW-Authenticate": 'Bearer realm="lan"'},
                    )

        return None  # allow

    def status(self) -> dict:
        return {
            "policies":        self.policies.all_policies(),
            "active_sessions": self.sessions.active_count(),
            "pending_codes":   self.pairing.pending_count(),
            "rate_limiter_clients": self.rate_limiter.client_count(),
        }


# ── Module-level singleton ────────────────────────────────────────────────────

_controller: Optional[AccessController] = None


def init_access_controller(data_dir: Path, cfg: dict) -> AccessController:
    """Initialise the module-level AccessController. Call once at startup."""
    global _controller
    _controller = AccessController(data_dir)
    _controller.load(cfg)
    logger.info("[access_ctrl] AccessController ready.")
    return _controller


def get_access_controller() -> Optional[AccessController]:
    """Return the active AccessController, or None if not yet initialised."""
    return _controller


# ── Starlette middleware ──────────────────────────────────────────────────────


class AccessControlMiddleware(BaseHTTPMiddleware):
    """Classify request origin, enforce tier policy, apply rate limiting,
    and validate LAN session tokens.

    Attaches to request.state:
      origin_tier  — "localhost" | "lan" | "external"
      client_ip    — resolved client IP string

    Must be added AFTER AdminAuthMiddleware so admin routes are already
    gated before this middleware adds its own checks.

    Exempt from all checks (always pass through):
      - WebSocket upgrades to /admin/logs (already gated by AdminAuthMiddleware)
      - Static files (.html, .js, .css) served from the root
    """

    async def dispatch(self, request: Request, call_next):
        ctrl = _controller
        if ctrl is None:
            # Controller not initialised yet — pass through (startup race)
            return await call_next(request)

        path = request.url.path
        client_ip = extract_client_ip(request)
        tier = classify_origin(client_ip)

        # Attach to request state for endpoint / audit use
        request.state.origin_tier = tier
        request.state.client_ip   = client_ip

        # Extract LAN token from X-Lan-Token header or Bearer token
        lan_token: Optional[str] = request.headers.get("X-Lan-Token", "").strip() or None
        if lan_token is None:
            auth_hdr = request.headers.get("Authorization", "")
            if auth_hdr.lower().startswith("bearer "):
                # Could be admin token or LAN token — try as LAN token; admin
                # auth is handled separately by AdminAuthMiddleware.
                candidate = auth_hdr[7:].strip()
                if ctrl.sessions.validate(candidate) is not None:
                    lan_token = candidate

        error_response = ctrl.check_access(tier, client_ip, path, lan_token)
        if error_response is not None:
            return error_response

        return await call_next(request)
