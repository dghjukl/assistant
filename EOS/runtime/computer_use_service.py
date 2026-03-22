"""
EOS — Computer Use Service
==========================
First-class subsystem for bounded, supervised computer-use capability.

This is NOT unrestricted computer access.  It is a layered permission and policy
subsystem that controls exactly which applications the entity may interact with,
and exactly which actions are permitted within each application.

Architecture
------------
Three orthogonal layers of control:

  Layer 1 — Session Mode  (is computer use enabled at all?)
  ──────────────────────────────────────────────────────────
    off                  → no computer use is permitted under any circumstance
    command_only         → entity may use approved apps only for direct,
                           user-requested tasks; each task is discrete
    supervised_session   → while explicitly enabled, the entity may use approved
                           apps for direct assistance AND for bounded continuation
                           of already-authorised work, assuming the user is
                           present and watching; user may halt at any time

  Layer 2 — Shortcut Allowlist  (which apps are currently approved?)
  ───────────────────────────────────────────────────────────────────
    Managed via the folder:  data/computer_use/approved_shortcuts/
    Each .json file in that folder is one approved entry point.
    Only shortcuts present there are considered launchable/usable.
    Removing a file from the folder immediately revokes that app.

  Layer 3 — Per-application Action Policy  (what may the entity do inside each app?)
  ────────────────────────────────────────────────────────────────────────────────────
    Defined in:  data/computer_use/app_policy.json
    Maps each app_id → { allowed_actions, confirmation_required_actions,
                         blocked_actions, window_title_match, domain_restrictions, … }

Decision flow
─────────────
  1. Is mode == "off"?                                    → DENY
  2. Is app_id in the live shortcut allowlist?            → DENY if absent
  3. Is action in policy.blocked_actions?                 → DENY
  4. Is action in policy.allowed_actions?                 → ALLOW
  5. Is action in policy.confirmation_required_actions?   → PENDING (ask user)
  6. Otherwise (unlisted action)                          → DENY (deny-by-default)

Observability
─────────────
  - All decisions (allow / deny / pending) are appended to an in-memory ring
  - Current state is always queryable via get_state()
  - Halt can be called at any time: mode → off, active app cleared, log appended
  - SignalBus integration: publishes on mode change and on halt

Terminology note
────────────────
  shortcut_id  — the identifier of the entry-point file in approved_shortcuts/
  app_id       — the logical application identifier (referenced by policy)
  action       — a named capability the entity wants to exercise (open, type, save…)

Usage
─────
    from runtime.computer_use_service import ComputerUseService

    svc = ComputerUseService(cfg)
    svc.set_mode("command_only")

    result = svc.check_action(app_id="notepad", action="type")
    if result.permitted:
        ...  # proceed
    elif result.requires_confirmation:
        ...  # surface to user; call svc.confirm_action() or svc.deny_action()
    else:
        ...  # blocked — result.reason explains why
"""

from __future__ import annotations

import collections
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("eos.computer_use")

UTC = timezone.utc


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# ── Mode constants ────────────────────────────────────────────────────────────

class ComputerUseMode:
    OFF                = "off"
    COMMAND_ONLY       = "command_only"
    SUPERVISED_SESSION = "supervised_session"

    ALL_MODES = (OFF, COMMAND_ONLY, SUPERVISED_SESSION)

    _DESCRIPTIONS = {
        OFF:                "No computer use permitted.",
        COMMAND_ONLY:       "Approved apps only, direct user-requested tasks.",
        SUPERVISED_SESSION: "Approved apps with bounded continuation while user supervises.",
    }

    @classmethod
    def describe(cls, mode: str) -> str:
        return cls._DESCRIPTIONS.get(mode, "Unknown mode.")


# ── Decision outcomes ─────────────────────────────────────────────────────────

class DecisionOutcome:
    ALLOW   = "allow"
    DENY    = "deny"
    PENDING = "pending"   # requires explicit user confirmation before proceeding


# ── Action categories ─────────────────────────────────────────────────────────

class ActionCategory:
    """Canonical action names the policy layer understands.

    Not every action must be implemented immediately; this list defines the
    full semantic vocabulary so policies written today remain valid as the
    runtime gains more capabilities.
    """
    OPEN             = "open"
    READ             = "read"
    TYPE             = "type"
    SAVE             = "save"
    SAVE_AS          = "save_as"
    CREATE_NEW       = "create_new"
    MOVE_WITHIN      = "move_within_workspace"
    DELETE           = "delete"
    SUBMIT           = "submit"
    SEND             = "send"
    LOGIN            = "login"
    PURCHASE         = "purchase"
    DOWNLOAD         = "download"
    CLOSE            = "close"
    NAVIGATE         = "navigate"
    COPY             = "copy"
    PASTE            = "paste"
    SCREENSHOT       = "screenshot"
    EXECUTE_COMMAND  = "execute_command"


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class ShortcutEntry:
    """A single entry from the approved_shortcuts/ folder."""
    shortcut_id:       str
    display_name:      str
    app_id:            str
    executable:        Optional[str]    = None
    description:       str             = ""
    added_by:          str             = "operator"
    added_at:          str             = field(default_factory=_now_iso)
    window_title_match: Optional[str]  = None
    path_restriction:  Optional[str]   = None
    notes:             str             = ""

    def to_dict(self) -> dict:
        return {
            "shortcut_id":       self.shortcut_id,
            "display_name":      self.display_name,
            "app_id":            self.app_id,
            "executable":        self.executable,
            "description":       self.description,
            "added_by":          self.added_by,
            "added_at":          self.added_at,
            "window_title_match": self.window_title_match,
            "path_restriction":  self.path_restriction,
            "notes":             self.notes,
        }


@dataclass
class AppPolicy:
    """Action policy for a single application."""
    app_id:                      str
    display_name:                str             = ""
    allowed_actions:             list[str]       = field(default_factory=list)
    confirmation_required_actions: list[str]    = field(default_factory=list)
    blocked_actions:             list[str]       = field(default_factory=list)
    window_title_match:          Optional[str]   = None
    domain_restrictions:         list[str]       = field(default_factory=list)
    path_restrictions:           list[str]       = field(default_factory=list)
    notes:                       str             = ""

    def to_dict(self) -> dict:
        return {
            "app_id":                       self.app_id,
            "display_name":                 self.display_name,
            "allowed_actions":              self.allowed_actions,
            "confirmation_required_actions": self.confirmation_required_actions,
            "blocked_actions":              self.blocked_actions,
            "window_title_match":           self.window_title_match,
            "domain_restrictions":          self.domain_restrictions,
            "path_restrictions":            self.path_restrictions,
            "notes":                        self.notes,
        }


@dataclass
class PolicyDecision:
    """Result of a check_action() call."""
    outcome:              str                   # DecisionOutcome.*
    app_id:               str
    action:               str
    reason:               str
    requires_confirmation: bool                 = False
    confirmation_id:      Optional[str]         = None
    timestamp:            str                   = field(default_factory=_now_iso)

    @property
    def permitted(self) -> bool:
        return self.outcome == DecisionOutcome.ALLOW

    @property
    def denied(self) -> bool:
        return self.outcome == DecisionOutcome.DENY

    def to_dict(self) -> dict:
        return {
            "outcome":               self.outcome,
            "app_id":                self.app_id,
            "action":                self.action,
            "reason":                self.reason,
            "requires_confirmation": self.requires_confirmation,
            "confirmation_id":       self.confirmation_id,
            "timestamp":             self.timestamp,
        }


@dataclass
class ComputerUseSnapshot:
    """Point-in-time observable state — safe to serialise for admin API."""
    mode:                str
    mode_description:    str
    active_app_id:       Optional[str]
    active_shortcut_id:  Optional[str]
    active_window_title: Optional[str]
    pending_confirmation: Optional[dict]
    approved_shortcuts:  list[dict]
    recent_decisions:    list[dict]
    total_decisions:     int
    session_started_at:  Optional[str]
    last_action_at:      Optional[str]

    def to_dict(self) -> dict:
        return {
            "mode":                 self.mode,
            "mode_description":     self.mode_description,
            "active_app_id":        self.active_app_id,
            "active_shortcut_id":   self.active_shortcut_id,
            "active_window_title":  self.active_window_title,
            "pending_confirmation": self.pending_confirmation,
            "approved_shortcuts":   self.approved_shortcuts,
            "recent_decisions":     self.recent_decisions,
            "total_decisions":      self.total_decisions,
            "session_started_at":   self.session_started_at,
            "last_action_at":       self.last_action_at,
        }


# ── Main service ──────────────────────────────────────────────────────────────

class ComputerUseService:
    """
    First-class computer-use subsystem for Entity OS.

    Thread-safe.  All public methods acquire the internal lock before
    reading or mutating state.

    Lifecycle
    ---------
    1. Instantiate at startup:  svc = ComputerUseService(cfg)
    2. Optionally reload policy / shortcuts on demand: svc.reload()
    3. Call set_mode() to enable/disable computer use
    4. Before each computer-use action:  decision = svc.check_action(...)
    5. If decision.requires_confirmation: surface to user, then call
       svc.confirm_pending(decision.confirmation_id) or
       svc.deny_pending(decision.confirmation_id)
    6. Call svc.set_active_app() when focus moves to a new application
    7. Call svc.halt() at any time to immediately cut off computer use
    """

    # How many decisions to keep in the in-memory ring
    DECISION_RING_SIZE = 200

    def __init__(self, cfg: dict, *, bus=None) -> None:
        self._cfg  = cfg
        self._bus  = bus
        self._lock = threading.Lock()

        cu_cfg = cfg.get("computer_use", {})
        root   = Path(cfg.get("_root", "."))

        # Data directories
        shortcuts_rel = cu_cfg.get(
            "approved_shortcuts_path",
            "data/computer_use/approved_shortcuts",
        )
        policy_rel = cu_cfg.get(
            "app_policy_path",
            "data/computer_use/app_policy.json",
        )
        self._shortcuts_dir = root / shortcuts_rel
        self._policy_path   = root / policy_rel

        # Runtime state
        self._mode:                str             = ComputerUseMode.OFF
        self._active_app_id:       Optional[str]   = None
        self._active_shortcut_id:  Optional[str]   = None
        self._active_window_title: Optional[str]   = None
        self._session_started_at:  Optional[str]   = None
        self._last_action_at:      Optional[str]   = None
        self._total_decisions:     int             = 0

        # Pending confirmation slot (only one at a time)
        self._pending_confirmation: Optional[dict] = None

        # In-memory decision ring
        self._decisions: collections.deque = collections.deque(
            maxlen=self.DECISION_RING_SIZE
        )

        # Loaded data
        self._shortcuts: dict[str, ShortcutEntry] = {}   # keyed by app_id
        self._policies:  dict[str, AppPolicy]     = {}   # keyed by app_id

        # Initial load
        self._load_shortcuts()
        self._load_policies()

        logger.info(
            "[computer_use] Initialised. mode=%s shortcuts=%d policies=%d",
            self._mode, len(self._shortcuts), len(self._policies),
        )

    # ── Configuration loading ─────────────────────────────────────────────────

    def reload(self) -> dict:
        """Reload shortcuts and policy from disk without changing mode."""
        with self._lock:
            prev_shortcuts = len(self._shortcuts)
            prev_policies  = len(self._policies)
            self._load_shortcuts()
            self._load_policies()
            result = {
                "shortcuts_loaded": len(self._shortcuts),
                "policies_loaded":  len(self._policies),
                "shortcuts_delta":  len(self._shortcuts) - prev_shortcuts,
                "policies_delta":   len(self._policies)  - prev_policies,
            }
        logger.info("[computer_use] Reloaded: %s", result)
        return result

    def _load_shortcuts(self) -> None:
        """Scan approved_shortcuts/ folder and load all .json shortcut files."""
        self._shortcuts.clear()

        if not self._shortcuts_dir.is_dir():
            logger.warning(
                "[computer_use] approved_shortcuts dir not found: %s",
                self._shortcuts_dir,
            )
            return

        for p in sorted(self._shortcuts_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                entry = ShortcutEntry(
                    shortcut_id        = data.get("shortcut_id",       p.stem),
                    display_name       = data.get("display_name",      p.stem),
                    app_id             = data.get("app_id",            p.stem),
                    executable         = data.get("executable"),
                    description        = data.get("description",       ""),
                    added_by           = data.get("added_by",          "operator"),
                    added_at           = data.get("added_at",          ""),
                    window_title_match = data.get("window_title_match"),
                    path_restriction   = data.get("path_restriction"),
                    notes              = data.get("notes",             ""),
                )
                self._shortcuts[entry.app_id] = entry
                logger.debug("[computer_use] Shortcut loaded: %s → %s", p.name, entry.app_id)
            except Exception as exc:
                logger.warning("[computer_use] Failed to load shortcut %s: %s", p, exc)

        logger.info("[computer_use] %d shortcut(s) loaded.", len(self._shortcuts))

    def _load_policies(self) -> None:
        """Load the master per-application policy file."""
        self._policies.clear()

        if not self._policy_path.is_file():
            logger.warning(
                "[computer_use] app_policy.json not found: %s",
                self._policy_path,
            )
            return

        try:
            data = json.loads(self._policy_path.read_text(encoding="utf-8"))
            apps = data.get("apps", {})
            for app_id, pcfg in apps.items():
                policy = AppPolicy(
                    app_id                       = app_id,
                    display_name                 = pcfg.get("display_name", app_id),
                    allowed_actions              = pcfg.get("allowed_actions", []),
                    confirmation_required_actions= pcfg.get("confirmation_required_actions", []),
                    blocked_actions              = pcfg.get("blocked_actions", []),
                    window_title_match           = pcfg.get("window_title_match"),
                    domain_restrictions          = pcfg.get("domain_restrictions", []),
                    path_restrictions            = pcfg.get("path_restrictions", []),
                    notes                        = pcfg.get("notes", ""),
                )
                self._policies[app_id] = policy
            logger.info("[computer_use] %d app polic(ies) loaded.", len(self._policies))
        except Exception as exc:
            logger.error("[computer_use] Failed to load app_policy.json: %s", exc)

    # ── Mode management ───────────────────────────────────────────────────────

    def get_mode(self) -> str:
        with self._lock:
            return self._mode

    def set_mode(self, mode: str, *, reason: str = "operator") -> bool:
        """
        Set the computer-use mode.  Returns True if the mode changed.

        Only modes defined in ComputerUseMode.ALL_MODES are accepted.
        Setting mode to 'off' implicitly clears the active app context
        and any pending confirmations.
        """
        if mode not in ComputerUseMode.ALL_MODES:
            raise ValueError(
                f"Invalid mode {mode!r}. "
                f"Valid modes: {ComputerUseMode.ALL_MODES}"
            )

        with self._lock:
            prev = self._mode
            if prev == mode:
                return False

            self._mode = mode

            # Entering a session mode: record start time
            if mode != ComputerUseMode.OFF and prev == ComputerUseMode.OFF:
                self._session_started_at = _now_iso()

            # Leaving all active modes: clear context
            if mode == ComputerUseMode.OFF:
                self._active_app_id       = None
                self._active_shortcut_id  = None
                self._active_window_title = None
                self._pending_confirmation = None
                self._session_started_at  = None

            entry = {
                "event":      "mode_changed",
                "prev_mode":  prev,
                "new_mode":   mode,
                "reason":     reason,
                "timestamp":  _now_iso(),
            }
            self._decisions.append(entry)
            self._total_decisions += 1

        logger.info(
            "[computer_use] Mode changed: %s → %s (reason=%s)", prev, mode, reason
        )
        self._publish_signal("mode_changed", {"prev_mode": prev, "new_mode": mode, "reason": reason})
        return True

    # ── Active application tracking ───────────────────────────────────────────

    def set_active_app(
        self,
        app_id: str,
        *,
        shortcut_id: Optional[str]   = None,
        window_title: Optional[str]  = None,
    ) -> None:
        """
        Record that the entity has switched focus to a new application.
        Does NOT perform a permission check — call check_action("open") first.
        """
        with self._lock:
            self._active_app_id       = app_id
            self._active_shortcut_id  = shortcut_id
            self._active_window_title = window_title

    def clear_active_app(self) -> None:
        """Clear active application context (e.g. after the entity closes an app)."""
        with self._lock:
            self._active_app_id       = None
            self._active_shortcut_id  = None
            self._active_window_title = None

    # ── Core decision engine ──────────────────────────────────────────────────

    def check_action(
        self,
        app_id: str,
        action: str,
        *,
        window_title: Optional[str] = None,
        context: Optional[dict]     = None,
    ) -> PolicyDecision:
        """
        The central gate.  Returns a PolicyDecision for every proposed action.

        Decision flow:
          1. mode == off                → DENY
          2. app_id not in allowlist    → DENY
          3. action in blocked_actions  → DENY
          4. action in allowed_actions  → ALLOW
          5. action in confirmation_req → PENDING
          6. action not listed anywhere → DENY (deny-by-default)

        All decisions are appended to the internal decision ring for observability.
        """
        with self._lock:
            decision = self._evaluate(app_id, action, window_title, context)
            self._decisions.append(decision.to_dict())
            self._total_decisions += 1
            if decision.permitted:
                self._last_action_at = decision.timestamp

        if decision.denied:
            logger.debug(
                "[computer_use] DENY  app=%s action=%s reason=%s",
                app_id, action, decision.reason,
            )
        elif decision.requires_confirmation:
            logger.info(
                "[computer_use] PENDING app=%s action=%s (confirmation required)",
                app_id, action,
            )
        else:
            logger.debug(
                "[computer_use] ALLOW app=%s action=%s",
                app_id, action,
            )

        return decision

    def _evaluate(
        self,
        app_id: str,
        action: str,
        window_title: Optional[str],
        context: Optional[dict],
    ) -> PolicyDecision:
        """Internal (lock already held). Returns the PolicyDecision."""
        import uuid as _uuid

        # ── Layer 1: mode gate ─────────────────────────────────────────────
        if self._mode == ComputerUseMode.OFF:
            return PolicyDecision(
                outcome = DecisionOutcome.DENY,
                app_id  = app_id,
                action  = action,
                reason  = "Computer use is disabled (mode=off).",
            )

        # ── Layer 2: shortcut allowlist ────────────────────────────────────
        if app_id not in self._shortcuts:
            return PolicyDecision(
                outcome = DecisionOutcome.DENY,
                app_id  = app_id,
                action  = action,
                reason  = (
                    f"Application '{app_id}' is not in the approved shortcut allowlist. "
                    "Add a shortcut file to data/computer_use/approved_shortcuts/ to grant access."
                ),
            )

        # ── Layer 3: policy lookup ─────────────────────────────────────────
        policy = self._policies.get(app_id)
        if policy is None:
            # App is allowlisted but has no policy entry → deny-by-default
            return PolicyDecision(
                outcome = DecisionOutcome.DENY,
                app_id  = app_id,
                action  = action,
                reason  = (
                    f"Application '{app_id}' has no policy entry in app_policy.json. "
                    "All actions are denied until a policy is defined."
                ),
            )

        # ── Layer 3a: blocked actions ──────────────────────────────────────
        if action in policy.blocked_actions:
            return PolicyDecision(
                outcome = DecisionOutcome.DENY,
                app_id  = app_id,
                action  = action,
                reason  = f"Action '{action}' is explicitly blocked by policy for '{app_id}'.",
            )

        # ── Layer 3b: allowed actions ──────────────────────────────────────
        if action in policy.allowed_actions:
            return PolicyDecision(
                outcome = DecisionOutcome.ALLOW,
                app_id  = app_id,
                action  = action,
                reason  = f"Action '{action}' is permitted by policy for '{app_id}'.",
            )

        # ── Layer 3c: confirmation-required actions ────────────────────────
        if action in policy.confirmation_required_actions:
            confirmation_id = "CONF-" + _uuid.uuid4().hex[:8].upper()
            pending = {
                "confirmation_id": confirmation_id,
                "app_id":          app_id,
                "action":          action,
                "window_title":    window_title,
                "context":         context,
                "requested_at":    _now_iso(),
            }
            self._pending_confirmation = pending
            return PolicyDecision(
                outcome               = DecisionOutcome.PENDING,
                app_id                = app_id,
                action                = action,
                reason                = (
                    f"Action '{action}' requires explicit user confirmation "
                    f"before execution (policy: confirmation_required)."
                ),
                requires_confirmation = True,
                confirmation_id       = confirmation_id,
            )

        # ── Layer 3d: unlisted action → deny-by-default ────────────────────
        return PolicyDecision(
            outcome = DecisionOutcome.DENY,
            app_id  = app_id,
            action  = action,
            reason  = (
                f"Action '{action}' is not listed in the policy for '{app_id}'. "
                "Unlisted actions are denied by default."
            ),
        )

    # ── Confirmation management ───────────────────────────────────────────────

    def confirm_pending(self, confirmation_id: str) -> bool:
        """
        Operator confirms a pending action.  Returns True if the confirmation_id
        matches the pending slot (and clears it).
        """
        with self._lock:
            if (
                self._pending_confirmation is not None
                and self._pending_confirmation.get("confirmation_id") == confirmation_id
            ):
                app_id = self._pending_confirmation.get("app_id", "?")
                action = self._pending_confirmation.get("action",  "?")
                self._pending_confirmation = None
                self._last_action_at = _now_iso()
                entry = {
                    "event":           "confirmation_granted",
                    "confirmation_id": confirmation_id,
                    "app_id":          app_id,
                    "action":          action,
                    "timestamp":       _now_iso(),
                }
                self._decisions.append(entry)
                self._total_decisions += 1
                logger.info(
                    "[computer_use] Confirmation GRANTED: %s for %s.%s",
                    confirmation_id, app_id, action,
                )
                return True
        logger.warning(
            "[computer_use] confirm_pending: id %s not found or already resolved.",
            confirmation_id,
        )
        return False

    def deny_pending(self, confirmation_id: str) -> bool:
        """
        Operator denies a pending action.  Returns True if matched and cleared.
        """
        with self._lock:
            if (
                self._pending_confirmation is not None
                and self._pending_confirmation.get("confirmation_id") == confirmation_id
            ):
                app_id = self._pending_confirmation.get("app_id", "?")
                action = self._pending_confirmation.get("action",  "?")
                self._pending_confirmation = None
                entry = {
                    "event":           "confirmation_denied",
                    "confirmation_id": confirmation_id,
                    "app_id":          app_id,
                    "action":          action,
                    "timestamp":       _now_iso(),
                }
                self._decisions.append(entry)
                self._total_decisions += 1
                logger.info(
                    "[computer_use] Confirmation DENIED: %s for %s.%s",
                    confirmation_id, app_id, action,
                )
                return True
        return False

    # ── Emergency halt ────────────────────────────────────────────────────────

    def halt(self, *, reason: str = "operator halt") -> dict:
        """
        Immediately disable all computer use.

        - Sets mode to off
        - Clears active app context
        - Clears any pending confirmation
        - Appends a HALT event to the decision ring
        - Publishes a HALT signal on the bus

        This is always available regardless of current state.
        """
        with self._lock:
            prev_mode   = self._mode
            prev_app    = self._active_app_id
            self._mode  = ComputerUseMode.OFF
            self._active_app_id       = None
            self._active_shortcut_id  = None
            self._active_window_title = None
            self._pending_confirmation = None
            self._session_started_at  = None
            halt_ts = _now_iso()
            entry = {
                "event":      "halt",
                "prev_mode":  prev_mode,
                "prev_app":   prev_app,
                "reason":     reason,
                "timestamp":  halt_ts,
            }
            self._decisions.append(entry)
            self._total_decisions += 1

        result = {
            "halted":    True,
            "prev_mode": prev_mode,
            "prev_app":  prev_app,
            "reason":    reason,
            "timestamp": halt_ts,
        }
        logger.warning(
            "[computer_use] HALT issued. prev_mode=%s prev_app=%s reason=%s",
            prev_mode, prev_app, reason,
        )
        self._publish_signal("halt", result)
        return result

    # ── Observability ─────────────────────────────────────────────────────────

    def get_state(self, decision_limit: int = 20) -> ComputerUseSnapshot:
        """Return a thread-safe snapshot of the current computer-use state."""
        with self._lock:
            mode    = self._mode
            pending = dict(self._pending_confirmation) if self._pending_confirmation else None
            recent  = list(self._decisions)[-decision_limit:]
            total   = self._total_decisions
            started = self._session_started_at
            last    = self._last_action_at
            shortcuts = [s.to_dict() for s in self._shortcuts.values()]
            active_app   = self._active_app_id
            active_sc    = self._active_shortcut_id
            active_wt    = self._active_window_title

        return ComputerUseSnapshot(
            mode                 = mode,
            mode_description     = ComputerUseMode.describe(mode),
            active_app_id        = active_app,
            active_shortcut_id   = active_sc,
            active_window_title  = active_wt,
            pending_confirmation = pending,
            approved_shortcuts   = shortcuts,
            recent_decisions     = recent,
            total_decisions      = total,
            session_started_at   = started,
            last_action_at       = last,
        )

    def get_policies(self) -> list[dict]:
        """Return all loaded app policies as plain dicts."""
        with self._lock:
            return [p.to_dict() for p in self._policies.values()]

    def get_shortcuts(self) -> list[dict]:
        """Return all currently-approved shortcuts as plain dicts."""
        with self._lock:
            return [s.to_dict() for s in self._shortcuts.values()]

    def is_app_allowed(self, app_id: str) -> bool:
        """Check whether an app_id is in the current shortcut allowlist."""
        with self._lock:
            return app_id in self._shortcuts

    def is_action_allowed(self, app_id: str, action: str) -> bool:
        """
        Quick boolean check (no confirmation support).
        Useful for pre-filtering before constructing full check_action context.
        """
        decision = self.check_action(app_id, action)
        return decision.permitted

    # ── SignalBus integration (optional) ──────────────────────────────────────

    def set_bus(self, bus: Any) -> None:
        """Attach a SignalBus instance after construction."""
        self._bus = bus

    def _publish_signal(self, event_type: str, payload: dict) -> None:
        """Publish a signal to the bus if one is attached."""
        if self._bus is None:
            return
        try:
            from runtime.signal_bus import SignalEnvelope, SEVERITY_INFO, SEVERITY_HIGH
            severity = SEVERITY_HIGH if event_type == "halt" else SEVERITY_INFO
            env = SignalEnvelope(
                source      = "computer_use",
                signal_type = f"computer_use:{event_type}",
                severity    = severity,
                confidence  = 1.0,
                payload     = payload,
            )
            self._bus.publish(env)
        except Exception as exc:
            logger.debug("[computer_use] Bus publish failed: %s", exc)

    # ── Capability registry integration ───────────────────────────────────────

    def capability_entry(self) -> dict:
        """
        Return a dict suitable for registering with the CapabilityRegistry.
        The server.py startup should call this and register the entry.
        """
        with self._lock:
            mode = self._mode
        from runtime.capability_registry import CapabilityKind, CapabilityStatus
        return {
            "name":    "computer_use",
            "kind":    "computer_use",        # new kind; registry accepts any string
            "status":  CapabilityStatus.ENABLED if mode != ComputerUseMode.OFF
                       else CapabilityStatus.DISABLED,
            "healthy": True,
            "policy":  "optional",
            "version": "1.0",
            "metadata": {
                "mode":              mode,
                "shortcuts_loaded":  len(self._shortcuts),
                "policies_loaded":   len(self._policies),
            },
        }
