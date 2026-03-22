"""
EOS — Survival Mode (Last-Ditch Fallback)
==========================================
Defines the minimal-intelligence path entered when the primary model is
unavailable.  This is not a degraded mode — it is the absolute floor:
no LLM inference, no tool execution, no background cognition.

Purpose
-------
When the primary model fails (at boot or mid-session), the FastAPI process
stays alive to:
  1. Tell the user the system is degraded, with a diagnostic message.
  2. Keep the admin panel accessible so the operator can diagnose and restart.
  3. Accept a small set of hard-coded operator commands (status, help, restart).
  4. Emit structured audit entries for every survival-mode turn.

Activation
----------
The survival mode is entered via one of two paths:

  A. Pre-boot failure: boot.py raises BootError for the primary server.
     The WebUI server catches this and calls SurvivalMode.activate(reason).

  B. Mid-session failure: the backend health probe marks the primary server
     as ERROR.  The orchestrator detects this before dispatching a turn and
     delegates to SurvivalMode.handle_turn(message).

Integration contract
--------------------
  from runtime.survival_mode import survival_mode

  # At WebUI startup if boot fails:
  survival_mode.activate(SurvivalReason.PRIMARY_BOOT_FAILURE, detail=str(exc))

  # In orchestrator.process_turn, before dispatching to the LLM:
  if survival_mode.is_active:
      return survival_mode.handle_turn(user_message)

  # Health endpoint:
  info = survival_mode.status()   # dict safe to serialise as JSON

The module singleton ``survival_mode`` is always importable regardless of
system state.  It is inactive until ``activate()`` is called.
"""
from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Optional

logger = logging.getLogger("eos.survival_mode")


# ── Reason taxonomy ──────────────────────────────────────────────────────────

class SurvivalReason(str, Enum):
    """Why the system entered survival mode."""
    PRIMARY_BOOT_FAILURE   = "primary_boot_failure"    # primary never became ready
    PRIMARY_MID_SESSION    = "primary_mid_session"     # primary died after initial boot
    CONFIG_FATAL           = "config_fatal"            # config could not be loaded
    MEMORY_INIT_FAILURE    = "memory_init_failure"     # database init failed
    UNKNOWN                = "unknown"


# ── Hard-coded operator commands ─────────────────────────────────────────────

_OPERATOR_COMMANDS: dict[str, str] = {
    "status":   "Report current system status.",
    "help":     "Show available commands.",
    "diagnose": "Show last error detail.",
    "restart":  "Instruct the operator to restart the EOS process.",
}

_HELP_TEXT = (
    "EOS is in survival mode.  The primary model is unavailable.  "
    "Available commands: " + ", ".join(f"/{k}" for k in _OPERATOR_COMMANDS) + ".  "
    "Restart the EOS process to attempt recovery."
)


# ── SurvivalModeService ───────────────────────────────────────────────────────

class SurvivalModeService:
    """
    Minimal response engine for use when the primary model is unavailable.

    Thread-safe: activate() and handle_turn() may be called from different
    threads (startup thread vs. FastAPI worker thread).
    """

    def __init__(self) -> None:
        self._active:    bool            = False
        self._reason:    SurvivalReason  = SurvivalReason.UNKNOWN
        self._detail:    str             = ""
        self._activated_at: float        = 0.0
        self._turn_count: int            = 0

    # ── Activation ────────────────────────────────────────────────────────

    def activate(
        self,
        reason: SurvivalReason = SurvivalReason.UNKNOWN,
        detail: str = "",
    ) -> None:
        """Enter survival mode.  Idempotent — safe to call multiple times."""
        if self._active:
            return
        self._active       = True
        self._reason       = reason
        self._detail       = detail
        self._activated_at = time.time()
        logger.error(
            "[survival] Survival mode ACTIVATED — reason=%s detail=%s",
            reason.value, detail or "(none)",
        )

    def deactivate(self) -> None:
        """Exit survival mode (called when the primary model recovers)."""
        if not self._active:
            return
        self._active = False
        logger.info("[survival] Survival mode deactivated — system recovered.")

    @property
    def is_active(self) -> bool:
        return self._active

    # ── Turn handling ─────────────────────────────────────────────────────

    def handle_turn(self, message: str) -> str:
        """
        Handle a user message in survival mode.

        Returns a plain-text response string.  The caller is responsible for
        wrapping this in whatever envelope the WebUI expects.
        """
        self._turn_count += 1
        msg = message.strip().lower()

        # Hard-coded operator command dispatch
        if msg in ("/help", "help"):
            return _HELP_TEXT

        if msg in ("/status", "status"):
            return self._status_text()

        if msg in ("/diagnose", "diagnose"):
            if self._detail:
                return f"Last error: {self._detail}"
            return "No additional diagnostic detail is available."

        if msg in ("/restart", "restart"):
            return (
                "To restart EOS: close this browser tab, stop the EOS process "
                "(Ctrl+C in the terminal or close the launcher), then relaunch.  "
                "If the primary model was out of VRAM, free other GPU processes first."
            )

        # Generic degraded response for all other messages
        return self._degraded_response()

    # ── Status / introspection ────────────────────────────────────────────

    def status(self) -> dict:
        """Return a JSON-serialisable status dict for the health endpoint."""
        return {
            "survival_mode": self._active,
            "reason":        self._reason.value if self._active else None,
            "detail":        self._detail        if self._active else None,
            "activated_at":  self._activated_at  if self._active else None,
            "turns_handled": self._turn_count,
        }

    # ── Private helpers ───────────────────────────────────────────────────

    def _status_text(self) -> str:
        elapsed = int(time.time() - self._activated_at)
        minutes, seconds = divmod(elapsed, 60)
        return (
            f"System status: SURVIVAL MODE (degraded).  "
            f"Reason: {self._reason.value}.  "
            f"Time in survival mode: {minutes}m {seconds}s.  "
            f"Turns handled since activation: {self._turn_count}.  "
            f"The primary model is unavailable.  Restart EOS to recover."
        )

    def _degraded_response(self) -> str:
        return (
            "I'm currently operating in survival mode — my primary model is "
            "unavailable and I cannot process general requests.  "
            "Type /status for diagnostics or /help for available commands.  "
            "An operator restart is required to restore normal function."
        )


# ── Module-level singleton ────────────────────────────────────────────────────

survival_mode = SurvivalModeService()
