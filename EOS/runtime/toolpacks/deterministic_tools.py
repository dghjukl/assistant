"""Deterministic Tools — Time, Calculator, System Info

Simple, stateless, read-only tools that provide system information and
safe mathematical operations.
"""

from __future__ import annotations

import ast
import json
import math
import os
import platform
import random
import uuid
from datetime import datetime, timezone
from typing import Any, Dict


def _jdump(x: Any) -> str:
    """JSON dump with fallback to str()."""
    try:
        return json.dumps(x, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


def register(registry: Any, config: Dict[str, Any]) -> None:
    """Register deterministic tools into the registry."""
    from runtime.tool_registry import ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy

    # time_now
    def time_now_handler(params: Dict[str, Any]) -> str:
        now_utc = datetime.now(timezone.utc)
        now_local = datetime.now().astimezone()
        utc_offset_minutes = int(now_local.utcoffset().total_seconds() // 60)
        utc_offset_hours = utc_offset_minutes / 60
        offset_sign = "+" if utc_offset_hours >= 0 else ""
        return _jdump({
            "utc_time": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "local_time": now_local.strftime("%Y-%m-%d %H:%M:%S"),
            "utc_offset": f"UTC{offset_sign}{utc_offset_hours:g}",
            "day_of_week": now_local.strftime("%A"),
        })

    registry.register(ToolSpec(
        name="time_now",
        description="Get current date and time (UTC and local with offset).",
        pack="deterministic_tools",
        tags=["system", "time"],
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=time_now_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
    ))

    # calculator
    def calculator_handler(params: Dict[str, Any]) -> str:
        expression = str(params.get("expression", "")).strip()
        if not expression:
            return _jdump({"error": "expression is required"})
        try:
            result = _safe_eval_math(expression)
            return _jdump({"expression": expression, "result": result})
        except Exception as e:
            return _jdump({"error": str(e)})

    registry.register(ToolSpec(
        name="calculator",
        description="Evaluate a mathematical expression safely.",
        pack="deterministic_tools",
        tags=["system", "utility"],
        parameters={
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "Expression like 2*(3+4) or sqrt(16)"},
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
        handler=calculator_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
    ))

    # random_number
    def random_number_handler(params: Dict[str, Any]) -> str:
        try:
            minimum = float(params["min"])
            maximum = float(params["max"])
        except (KeyError, TypeError, ValueError) as e:
            return _jdump({"error": f"min and max must be numeric: {e}"})

        if maximum < minimum:
            return _jdump({"error": "max must be >= min"})

        integer_mode = bool(params.get("integer", False))
        try:
            if integer_mode:
                imin = math.ceil(minimum)
                imax = math.floor(maximum)
                if imax < imin:
                    return _jdump({"error": "no integer exists in the requested range"})
                value = random.randint(imin, imax)
            else:
                value = random.uniform(minimum, maximum)

            return _jdump({
                "min": minimum,
                "max": maximum,
                "integer": integer_mode,
                "value": value,
            })
        except Exception as e:
            return _jdump({"error": str(e)})

    registry.register(ToolSpec(
        name="random_number",
        description="Generate a random number within a range.",
        pack="deterministic_tools",
        tags=["system", "utility"],
        parameters={
            "type": "object",
            "properties": {
                "min": {"type": "number"},
                "max": {"type": "number"},
                "integer": {"type": "boolean", "default": False},
            },
            "required": ["min", "max"],
            "additionalProperties": False,
        },
        handler=random_number_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
    ))

    # uuid_generate
    def uuid_generate_handler(params: Dict[str, Any]) -> str:
        return _jdump({"uuid": str(uuid.uuid4())})

    registry.register(ToolSpec(
        name="uuid_generate",
        description="Generate a random UUID.",
        pack="deterministic_tools",
        tags=["system", "utility"],
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=uuid_generate_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
    ))

    # system_info
    def system_info_handler(params: Dict[str, Any]) -> str:
        return _jdump({
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "python_version": platform.python_version(),
            "cpu_count": os.cpu_count(),
        })

    registry.register(ToolSpec(
        name="system_info",
        description="Return system and Python runtime information.",
        pack="deterministic_tools",
        tags=["system", "utility"],
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=system_info_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
    ))


# ---------------------------------------------------------------------------
# Safe math evaluation
# ---------------------------------------------------------------------------

_ALLOWED_MATH_FUNCTIONS: Dict[str, Any] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "pow": pow,
    "sqrt": math.sqrt,
    "ceil": math.ceil,
    "floor": math.floor,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "pi": math.pi,
    "e": math.e,
}


def _safe_eval_math(expression: str) -> Any:
    """Safely evaluate a mathematical expression."""
    try:
        node = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"Invalid mathematical expression: {e}")

    def _eval(n: ast.AST) -> Any:
        if isinstance(n, ast.Expression):
            return _eval(n.body)

        if isinstance(n, ast.Constant):
            if isinstance(n.value, (int, float)):
                return n.value
            raise ValueError("Only numeric constants are allowed")

        if isinstance(n, ast.BinOp):
            left = _eval(n.left)
            right = _eval(n.right)
            if isinstance(n.op, ast.Add):
                return left + right
            if isinstance(n.op, ast.Sub):
                return left - right
            if isinstance(n.op, ast.Mult):
                return left * right
            if isinstance(n.op, ast.Div):
                return left / right
            if isinstance(n.op, ast.FloorDiv):
                return left // right
            if isinstance(n.op, ast.Mod):
                return left % right
            if isinstance(n.op, ast.Pow):
                return left ** right
            raise ValueError("Unsupported operator")

        if isinstance(n, ast.UnaryOp):
            operand = _eval(n.operand)
            if isinstance(n.op, ast.UAdd):
                return +operand
            if isinstance(n.op, ast.USub):
                return -operand
            raise ValueError("Unsupported unary operator")

        if isinstance(n, ast.Call):
            if not isinstance(n.func, ast.Name):
                raise ValueError("Unsupported function call")
            func_name = n.func.id
            func = _ALLOWED_MATH_FUNCTIONS.get(func_name)
            if func is None or not callable(func):
                raise ValueError(f"Function not allowed: {func_name}")
            args = [_eval(arg) for arg in n.args]
            return func(*args)

        if isinstance(n, ast.Name):
            value = _ALLOWED_MATH_FUNCTIONS.get(n.id)
            if value is None:
                raise ValueError(f"Name not allowed: {n.id}")
            return value

        raise ValueError("Unsupported expression")

    return _eval(node)
