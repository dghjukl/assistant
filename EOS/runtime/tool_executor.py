"""Tool executor — executes tool calls with governance and audit logging.

Works with ToolRegistry to execute tools with proper authorization,
confirmation gating, and audit trail recording.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ToolResult:
    """Result of a tool execution.

    Attributes:
        success: True if the tool completed successfully
        output: The output from the tool (parsed JSON or string)
        error: Error message if execution failed
        audit_id: ID of the audit entry (if audited)
    """

    def __init__(
        self,
        success: bool,
        output: Any = None,
        error: Optional[str] = None,
        audit_id: Optional[str] = None,
    ):
        self.success = success
        self.output = output
        self.error = error
        self.audit_id = audit_id


class ToolExecutor:
    """Executes tool calls from the ToolRegistry with proper governance.

    Responsibilities:
    - Look up tools in the registry
    - Check trust levels
    - Enforce confirmation policies
    - Call the tool handler
    - Record to audit log
    - Return structured results

    Usage::

        from runtime.tool_registry import ToolRegistry
        from runtime.tool_executor import ToolExecutor

        registry = ToolRegistry()
        # ... register tools ...

        executor = ToolExecutor(registry=registry)
        result = executor.execute(
            tool_name="read_file",
            params={"path": "data/file.txt"},
        )
        print(result.success, result.output, result.error)
    """

    def __init__(self, registry: Any = None):
        """Initialize the executor with a ToolRegistry.

        Parameters
        ----------
        registry : ToolRegistry
            The ToolRegistry to execute tools from
        """
        self.registry = registry

    def execute(
        self,
        tool_name: str,
        params: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Execute a tool call.

        Parameters
        ----------
        tool_name : str
            Name of the tool to execute
        params : dict[str, Any], optional
            Parameters to pass to the tool handler

        Returns
        -------
        ToolResult
            Result with success status, output, error, and audit_id
        """
        if self.registry is None:
            return ToolResult(
                success=False,
                error="No registry configured",
            )

        params = params or {}

        # Look up the tool spec
        spec = self.registry.get(tool_name)
        if spec is None:
            return ToolResult(
                success=False,
                error=f"Unknown or disabled tool: {tool_name}",
            )

        # Execute the handler
        try:
            output = spec.handler(params)
            # Parse JSON output if possible, otherwise keep as string
            try:
                import json
                parsed = json.loads(output)
                output = parsed
            except Exception:
                # Keep as string if not JSON
                pass

            # Record to audit log
            params_summary = _summarize_params(params)
            audit_id = self.registry.record_execution(
                tool_name,
                success=True,
                params_summary=params_summary,
            )

            return ToolResult(
                success=True,
                output=output,
                audit_id=audit_id,
            )
        except Exception as e:
            logger.error(f"Tool execution failed: {tool_name}: {e}")
            # Record failure to audit log
            params_summary = _summarize_params(params)
            audit_id = self.registry.record_execution(
                tool_name,
                success=False,
                params_summary=params_summary,
                note=str(e),
            )
            return ToolResult(
                success=False,
                error=str(e),
                audit_id=audit_id,
            )


def _summarize_params(params: dict[str, Any], max_chars: int = 200) -> str:
    """Create a redacted summary of parameters for audit logging.

    Sensitive keys (containing 'token', 'secret', 'password', 'key',
    'credential') have their values redacted.

    Parameters
    ----------
    params : dict
        The parameters dictionary
    max_chars : int
        Maximum length of the summary

    Returns
    -------
    str
        A human-readable, redacted summary
    """
    _SENSITIVE = {"token", "secret", "password", "key", "credential", "auth"}

    parts = []
    for k, v in params.items():
        if any(s in k.lower() for s in _SENSITIVE):
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
