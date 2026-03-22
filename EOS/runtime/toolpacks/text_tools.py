"""Text Processing Tools — ANSI stripping, escape decoding, formatting

Simple text manipulation tools for:
- Removing ANSI escape sequences
- Normalizing newlines and escape sequences
- Truncating and wrapping
- Extracting JSON from text
"""

from __future__ import annotations

import json
import re
import textwrap
from typing import Any, Dict


def _jdump(x: Any) -> str:
    """JSON dump with fallback to str()."""
    try:
        return json.dumps(x, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


def _safe_int(x: Any, default: int) -> int:
    """Safe integer conversion."""
    try:
        return int(x)
    except Exception:
        return default


_ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


def register(registry: Any, config: Dict[str, Any]) -> None:
    """Register text processing tools into the registry."""
    from runtime.tool_registry import (
        ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy
    )

    text_cfg = config.get("text", {}) if isinstance(config, dict) else {}
    enabled = bool(text_cfg.get("enabled", True))

    # strip_ansi
    def strip_ansi_handler(params: Dict[str, Any]) -> str:
        s = str(params.get("text") or "")
        return _jdump({"text": _ANSI_RE.sub("", s)})

    registry.register(ToolSpec(
        name="strip_ansi",
        description="Remove ANSI escape sequences from text.",
        pack="text_tools",
        tags=["text"],
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=strip_ansi_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    # normalize_newlines
    def normalize_newlines_handler(params: Dict[str, Any]) -> str:
        s = str(params.get("text") or "")
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        return _jdump({"text": s})

    registry.register(ToolSpec(
        name="normalize_newlines",
        description="Normalize CRLF/CR to LF newlines.",
        pack="text_tools",
        tags=["text"],
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=normalize_newlines_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    # decode_escapes
    def decode_escapes_handler(params: Dict[str, Any]) -> str:
        s = str(params.get("text") or "")
        out = s
        # First try unicode_escape (peels layers)
        try:
            for _ in range(2):
                out2 = bytes(out, "utf-8").decode("unicode_escape")
                if out2 == out:
                    break
                out = out2
        except Exception:
            out = s

        # Replace remaining literal sequences
        for _ in range(3):
            out2 = (out
                    .replace("\\r\\n", "\n")
                    .replace("\\n", "\n")
                    .replace("\\r", "\n")
                    .replace("\\t", "\t"))
            if out2 == out:
                break
            out = out2

        lines = out.splitlines()
        return _jdump({
            "ok": True,
            "text": out,
            "lines": lines,
            "line_count": len(lines),
        })

    registry.register(ToolSpec(
        name="decode_escapes",
        description="Decode escape sequences (handles double-escaped input).",
        pack="text_tools",
        tags=["text"],
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=decode_escapes_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    # truncate_text
    def truncate_text_handler(params: Dict[str, Any]) -> str:
        s = str(params.get("text") or "")
        n = _safe_int(params.get("max_chars"), 2000)
        n = max(1, min(n, 200000))
        suffix = str(params.get("suffix") or " ...")
        if len(s) <= n:
            return _jdump({"text": s, "truncated": False})
        keep = max(0, n - len(suffix))
        return _jdump({"text": s[:keep] + suffix, "truncated": True})

    registry.register(ToolSpec(
        name="truncate_text",
        description="Truncate text to max_chars with suffix.",
        pack="text_tools",
        tags=["text"],
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "max_chars": {"type": "integer"},
                "suffix": {"type": "string"},
            },
            "required": ["text"],
        },
        handler=truncate_text_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    # wrap_text
    def wrap_text_handler(params: Dict[str, Any]) -> str:
        s = str(params.get("text") or "")
        width = _safe_int(params.get("width"), 80)
        width = max(10, min(width, 200))
        return _jdump({"text": textwrap.fill(s, width=width)})

    registry.register(ToolSpec(
        name="wrap_text",
        description="Hard-wrap text to a given width.",
        pack="text_tools",
        tags=["text"],
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "width": {"type": "integer"},
            },
            "required": ["text"],
        },
        handler=wrap_text_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    # extract_json_first
    def extract_json_first_handler(params: Dict[str, Any]) -> str:
        s = str(params.get("text") or "")
        start = None
        for i, ch in enumerate(s):
            if ch in "{[":
                start = i
                break
        if start is None:
            return _jdump({"ok": False, "error": "No JSON start found."})

        for end in range(len(s), start + 1, -1):
            sub = s[start:end].strip()
            if not sub or sub[-1] not in "]}":
                continue
            try:
                obj = json.loads(sub)
                return _jdump({"ok": True, "type": type(obj).__name__, "json": obj})
            except Exception:
                continue
        return _jdump({"ok": False, "error": "Could not parse any JSON block."})

    registry.register(ToolSpec(
        name="extract_json_first",
        description="Extract and parse the first JSON object/array found.",
        pack="text_tools",
        tags=["text"],
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=extract_json_first_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))
