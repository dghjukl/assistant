"""Secret Detection and Redaction Tools

Configuration:
  secrets:
    enabled: true
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _jdump(x: Any) -> str:
    try:
        return json.dumps(x, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


# Secret patterns
_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]{30,}\b")),
    ("api_key", re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^'\"]\s]{8,}['\"]?")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]+?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
]


def _find(text: str) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    if not text:
        return findings
    for name, pat in _PATTERNS:
        for m in pat.finditer(text):
            s = m.group(0)
            snippet = s[:8] + "…" + s[-6:] if len(s) > 20 else s
            findings.append({"type": name, "match": snippet, "start": m.start(), "end": m.end()})
    return findings


def _redact(text: str, replacement: str = "[REDACTED]") -> Tuple[str, List[Dict[str, Any]]]:
    if not text:
        return text, []
    findings = _find(text)
    spans = [(f["start"], f["end"], f["type"]) for f in findings]
    spans.sort(key=lambda x: x[0], reverse=True)
    out = text
    for start, end, _t in spans:
        try:
            out = out[:start] + replacement + out[end:]
        except Exception:
            pass
    return out, findings


def register(registry: Any, config: Dict[str, Any]) -> None:
    from runtime.tool_registry import ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy

    scfg = config.get("secrets", {}) if isinstance(config, dict) else {}
    enabled = bool(scfg.get("enabled", True))
    replacement = str(scfg.get("replacement", "[REDACTED]"))
    project_root = Path(str(config.get("project_root", "."))).resolve()
    allowed_roots = [(project_root / d).resolve() for d in ("logs", "data", "config")]

    def _resolve_allowed(path_str: str) -> Tuple[Optional[Path], Optional[str]]:
        if not path_str:
            return None, "Missing path."
        p = Path(path_str)
        if not p.is_absolute():
            p = (project_root / p)
        try:
            p = p.resolve()
        except Exception as e:
            return None, f"Could not resolve path: {e}"
        if not any(str(p).lower().startswith(str(ar).lower()) for ar in allowed_roots):
            return None, f"Path not allowed: {p}"
        return p, None

    def scan_for_secrets_handler(params: Dict[str, Any]) -> str:
        if not enabled:
            return "Secrets scanning disabled"
        text = params.get("text")
        path = str(params.get("path") or "").strip()
        if text is None and path:
            p, err = _resolve_allowed(path)
            if err:
                return err
            if not p.exists():
                return f"File not found: {p}"
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception as e:
                return f"Could not read file: {e}"
        if text is None:
            return "Provide 'text' or 'path'."
        findings = _find(str(text))
        return _jdump({"count": len(findings), "findings": findings})

    registry.register(ToolSpec(
        name="scan_for_secrets",
        description="Scan text or file for secrets (API keys, tokens, private keys).",
        pack="secrets_tools",
        tags=["security", "secrets"],
        parameters={"type": "object", "properties": {"text": {"type": "string"}, "path": {"type": "string"}}, "required": []},
        handler=scan_for_secrets_handler,
        risk_level=ToolRiskLevel.READ_ONLY,
        trust_level=ToolTrustLevel.PUBLIC,
        confirmation_policy=ConfirmationPolicy.NONE,
        enabled=enabled,
    ))

    def redact_secrets_handler(params: Dict[str, Any]) -> str:
        if not enabled:
            return "Secrets redaction disabled"
        text = params.get("text")
        path = str(params.get("path") or "").strip()
        in_place = bool(params.get("in_place", False))
        if text is None and not path:
            return "Provide 'text' or 'path'."
        if text is None and path:
            p, err = _resolve_allowed(path)
            if err:
                return err
            if not p.exists():
                return f"File not found: {p}"
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception as e:
                return f"Could not read file: {e}"
            redacted, findings = _redact(str(text), replacement=replacement)
            if in_place:
                try:
                    p.write_text(redacted, encoding="utf-8")
                except Exception as e:
                    return f"Failed to write redacted file: {e}"
                return _jdump({"ok": True, "path": str(p), "count": len(findings)})
            return _jdump({"ok": True, "count": len(findings), "redacted_text": redacted})
        redacted, findings = _redact(str(text), replacement=replacement)
        return _jdump({"ok": True, "count": len(findings), "redacted_text": redacted})

    registry.register(ToolSpec(
        name="redact_secrets",
        description="Redact secrets in text or file.",
        pack="secrets_tools",
        tags=["security", "secrets"],
        parameters={"type": "object", "properties": {"text": {"type": "string"}, "path": {"type": "string"}, "in_place": {"type": "boolean"}}, "required": []},
        handler=redact_secrets_handler,
        risk_level=ToolRiskLevel.DRAFT,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.SOFT_CONFIRM,
        enabled=enabled,
    ))
