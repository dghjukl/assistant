"""Tool executor — executes tool calls with mandatory governance controls.

Governance controls enforced on every execution:
  - Trust level check: caller must meet the tool's required trust level
  - Parameter schema validation: params validated against tool's JSON schema
  - Confirmation gating: HARD_CONFIRM tools block until explicitly approved
    or denied via confirm_pending() / deny_pending()
  - Timeout enforcement: execution killed after spec.timeout_seconds
  - Durable audit logging: every call written to AuditStore if available
"""
from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ── Trust level ordering ────────────────────────────────────────────────────

_TRUST_ORDER = ["PUBLIC", "VERIFIED_USER", "OPERATOR_ONLY"]


def _trust_rank(level: str) -> int:
    try:
        return _TRUST_ORDER.index(level.upper())
    except ValueError:
        return 0


# ── Exceptions ──────────────────────────────────────────────────────────────

class ToolPendingConfirmation(Exception):
    """Raised when a HARD_CONFIRM tool is called without prior approval.

    The caller should present confirmation_id to the operator and then call
    executor.confirm_pending(confirmation_id) or executor.deny_pending(confirmation_id).
    """

    def __init__(self, confirmation_id: str, tool_name: str, params_summary: str):
        self.confirmation_id = confirmation_id
        self.tool_name = tool_name
        self.params_summary = params_summary
        super().__init__(
            f"Tool '{tool_name}' requires operator confirmation — "
            f"confirmation_id={confirmation_id}"
        )


class ToolExecutionTimeout(Exception):
    """Raised when a tool handler exceeds its configured timeout."""


# ── Result ───────────────────────────────────────────────────────────────────

class ToolResult:
    """Result of a tool execution.

    Attributes
    ----------
    success
        True if the tool completed without error.
    output
        Parsed JSON output or raw string from the handler.
    error
        Error message if execution failed.
    audit_id
        ID of the durable audit entry (if AuditStore available).
    pending_confirmation_id
        Set when the tool returned a ToolPendingConfirmation — the operator
        must call confirm_pending() with this ID to proceed.
    """

    def __init__(
        self,
        success: bool,
        output: Any = None,
        error: Optional[str] = None,
        audit_id: Optional[str] = None,
        pending_confirmation_id: Optional[str] = None,
    ):
        self.success = success
        self.output = output
        self.error = error
        self.audit_id = audit_id
        self.pending_confirmation_id = pending_confirmation_id


# ── Executor ─────────────────────────────────────────────────────────────────

class ToolExecutor:
    """Executes tool calls from the ToolRegistry with mandatory governance.

    Governance controls applied (in order):
      1. Registry lookup — unknown / disabled tools are rejected
      2. Trust level — caller_trust must meet tool's required trust_level
      3. Schema validation — params validated against spec.parameters (JSON Schema)
      4. Confirmation gating — HARD_CONFIRM tools are queued pending approval
      5. Handler execution — run in thread with spec.timeout_seconds deadline
      6. Audit logging — outcome written to AuditStore (if configured)

    Parameters
    ----------
    registry
        ToolRegistry instance.
    audit_store
        Optional AuditStore for durable logging.  Falls back to the
        module-level singleton from core.audit if not provided.
    """

    def __init__(self, registry: Any = None, audit_store: Any = None):
        self.registry = registry
        self._audit_store = audit_store
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        # Single-thread executor per ToolExecutor instance for isolated timeouts
        self._thread_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="tool_exec"
        )

    def _get_audit_store(self):
        if self._audit_store is not None:
            return self._audit_store
        try:
            from core.audit import get_audit_store
            return get_audit_store()
        except Exception:
            return None

    # ── Public execute API ────────────────────────────────────────────────

    def execute(
        self,
        tool_name: str,
        params: Optional[Dict[str, Any]] = None,
        caller_trust: str = "PUBLIC",
    ) -> ToolResult:
        """Execute a tool call with full governance enforcement.

        Parameters
        ----------
        tool_name
            Name of the registered tool.
        params
            Parameters dict.  Validated against spec.parameters if present.
        caller_trust
            Trust level of the caller: PUBLIC | VERIFIED_USER | OPERATOR_ONLY.
            Calls are rejected if this is below the tool's required trust_level.

        Returns
        -------
        ToolResult
            On HARD_CONFIRM tools: success=False, pending_confirmation_id set.
            On timeout: success=False, error="timed out after Ns".
            On schema error: success=False, error describes the violation.
        """
        if self.registry is None:
            return ToolResult(success=False, error="No tool registry configured")

        params = params or {}

        # 1. Registry lookup
        spec = self.registry.get(tool_name)
        if spec is None:
            return ToolResult(success=False, error=f"Unknown or disabled tool: {tool_name}")

        # 2. Trust level check
        required_rank = _trust_rank(getattr(spec, "trust_level", "PUBLIC"))
        caller_rank = _trust_rank(caller_trust)
        if caller_rank < required_rank:
            msg = (
                f"Insufficient trust: caller={caller_trust}, "
                f"required={spec.trust_level}"
            )
            logger.warning("[executor] Trust check failed for %s: %s", tool_name, msg)
            _audit = self._get_audit_store()
            if _audit:
                _audit.record_tool_execution(
                    tool_name,
                    success=False,
                    pack=getattr(spec, "pack", None),
                    risk_level=getattr(spec, "risk_level", None),
                    params_summary=_summarize_params(params),
                    error=msg,
                )
            return ToolResult(success=False, error=msg)

        # 3. Parameter schema validation
        schema = getattr(spec, "parameters", None)
        if schema and isinstance(schema, dict):
            validation_error = _validate_params(params, schema)
            if validation_error:
                msg = f"Parameter validation failed: {validation_error}"
                logger.warning("[executor] Schema validation failed for %s: %s", tool_name, msg)
                _audit = self._get_audit_store()
                if _audit:
                    _audit.record_tool_execution(
                        tool_name,
                        success=False,
                        pack=getattr(spec, "pack", None),
                        risk_level=getattr(spec, "risk_level", None),
                        params_summary=_summarize_params(params),
                        error=msg,
                    )
                return ToolResult(success=False, error=msg)

        # 4. Confirmation gating
        confirmation_policy = getattr(spec, "confirmation_policy", "NONE").upper()
        if confirmation_policy == "HARD_CONFIRM":
            conf_id = str(uuid.uuid4())
            with self._pending_lock:
                self._pending[conf_id] = {
                    "tool_name": tool_name,
                    "params": params,
                    "params_summary": _summarize_params(params),
                    "spec": spec,
                    "caller_trust": caller_trust,
                    "requested_at": time.time(),
                }
            logger.info(
                "[executor] HARD_CONFIRM required for %s — confirmation_id=%s",
                tool_name, conf_id,
            )
            return ToolResult(
                success=False,
                error=f"Tool '{tool_name}' requires operator confirmation",
                pending_confirmation_id=conf_id,
            )

        # 5. Execute with timeout
        return self._run_handler(spec, params)

    def confirm_pending(self, confirmation_id: str) -> ToolResult:
        """Approve and execute a HARD_CONFIRM-gated tool call.

        Returns ToolResult(success=False, error=...) if the ID is unknown.
        """
        with self._pending_lock:
            pending = self._pending.pop(confirmation_id, None)
        if pending is None:
            return ToolResult(
                success=False,
                error=f"Confirmation ID not found or already resolved: {confirmation_id}",
            )
        logger.info(
            "[executor] Confirmed %s (tool=%s)",
            confirmation_id, pending["tool_name"],
        )
        return self._run_handler(pending["spec"], pending["params"])

    def deny_pending(self, confirmation_id: str) -> bool:
        """Deny and discard a HARD_CONFIRM-gated tool call.

        Returns True if the confirmation ID existed, False if not found.
        """
        with self._pending_lock:
            entry = self._pending.pop(confirmation_id, None)
        if entry:
            logger.info(
                "[executor] Denied %s (tool=%s)",
                confirmation_id, entry["tool_name"],
            )
        return entry is not None

    def list_pending(self) -> list[dict]:
        """Return summary of all pending confirmations (no param values)."""
        with self._pending_lock:
            return [
                {
                    "confirmation_id": cid,
                    "tool_name": e["tool_name"],
                    "params_summary": e["params_summary"],
                    "requested_at": e["requested_at"],
                }
                for cid, e in self._pending.items()
            ]

    # ── Internal execution ────────────────────────────────────────────────

    def _run_handler(self, spec: Any, params: Dict[str, Any]) -> ToolResult:
        """Run spec.handler(params) with timeout and audit logging."""
        tool_name = spec.name
        timeout = getattr(spec, "timeout_seconds", 30) or 30
        start_ms = int(time.monotonic() * 1000)

        try:
            future = self._thread_pool.submit(spec.handler, params)
            try:
                raw_output = future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                future.cancel()
                duration_ms = int(time.monotonic() * 1000) - start_ms
                msg = f"Tool timed out after {timeout}s"
                logger.error("[executor] %s: %s", tool_name, msg)
                audit_id = self._write_audit(
                    tool_name, spec, params, success=False,
                    error=msg, duration_ms=duration_ms,
                )
                return ToolResult(success=False, error=msg, audit_id=audit_id)

            # Parse JSON output
            output = raw_output
            try:
                import json as _json
                output = _json.loads(raw_output)
            except Exception:
                pass

            duration_ms = int(time.monotonic() * 1000) - start_ms
            audit_id = self._write_audit(
                tool_name, spec, params, success=True, duration_ms=duration_ms
            )

            # Also record to in-registry audit log for backward compatibility
            params_summary = _summarize_params(params)
            if self.registry and hasattr(self.registry, "record_execution"):
                self.registry.record_execution(tool_name, success=True, params_summary=params_summary)

            return ToolResult(success=True, output=output, audit_id=audit_id)

        except Exception as exc:
            duration_ms = int(time.monotonic() * 1000) - start_ms
            logger.error("[executor] Tool execution failed: %s: %s", tool_name, exc)
            audit_id = self._write_audit(
                tool_name, spec, params, success=False,
                error=str(exc), duration_ms=duration_ms,
            )
            if self.registry and hasattr(self.registry, "record_execution"):
                self.registry.record_execution(
                    tool_name, success=False,
                    params_summary=_summarize_params(params),
                    note=str(exc),
                )
            return ToolResult(success=False, error=str(exc), audit_id=audit_id)

    def _write_audit(
        self,
        tool_name: str,
        spec: Any,
        params: Dict[str, Any],
        success: bool,
        error: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> Optional[str]:
        store = self._get_audit_store()
        if store is None:
            return None
        return store.record_tool_execution(
            tool_name=tool_name,
            success=success,
            pack=getattr(spec, "pack", None),
            risk_level=getattr(spec, "risk_level", None),
            params_summary=_summarize_params(params),
            error=error,
            duration_ms=duration_ms,
        )


# ── Param helpers ─────────────────────────────────────────────────────────────

_SENSITIVE_KEYS = {"token", "secret", "password", "key", "credential", "auth"}


def _summarize_params(params: Dict[str, Any], max_chars: int = 200) -> str:
    """Produce a redacted, length-limited summary of params for logging."""
    parts = []
    for k, v in params.items():
        if any(s in k.lower() for s in _SENSITIVE_KEYS):
            parts.append(f"{k}=<redacted>")
        else:
            v_repr = repr(v)
            if len(v_repr) > 60:
                v_repr = v_repr[:57] + "..."
            parts.append(f"{k}={v_repr}")
    summary = "{" + ", ".join(parts) + "}"
    if len(summary) > max_chars:
        summary = summary[: max_chars - 3] + "..."
    return summary


def _validate_params(params: Dict[str, Any], schema: dict) -> Optional[str]:
    """Validate params against a JSON Schema dict.

    Returns None on success, or an error string on failure.
    Silently skips validation if jsonschema is unavailable or broken at import-time.
    """
    try:
        import jsonschema
    except Exception:
        logger.debug("[executor] jsonschema unavailable — skipping param validation")
        return None

    try:
        jsonschema.validate(instance=params, schema=schema)
        return None
    except Exception as exc:
        return str(getattr(exc, "message", exc))
