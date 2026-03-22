"""Notification Tools — Multi-backend notification dispatch

Configuration:
  notifications:
    enabled: true
    backends: [platform, webhook, email, log_only]
"""

from __future__ import annotations

import json
import shutil
import smtplib
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict


def _jdump(x: Any) -> str:
    try:
        return json.dumps(x, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(x)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _try_windows_toast(title: str, message: str, level: str) -> bool:
    if sys.platform != "win32":
        return False
    ps_script = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null
[Windows.UI.Notifications.ToastNotification,Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom,ContentType=WindowsRuntime] | Out-Null
$xml = [Windows.Data.Xml.Dom.XmlDocument]::new()
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>{title}</text><text>{message}</text></binding></visual></toast>')
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("EOS").Show($toast)
""".replace("{title}", title.replace('"', '`"').replace("'", "`'")).replace(
        "{message}", message.replace('"', '`"').replace("'", "`'")[:200]
    )
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script], timeout=5, capture_output=True)
        return result.returncode == 0
    except Exception:
        return False


def _try_linux_notify_send(title: str, message: str, level: str) -> bool:
    if not sys.platform.startswith("linux"):
        return False
    notify_send = shutil.which("notify-send")
    if not notify_send:
        return False
    urgency = "normal" if level not in ("error", "warning") else ("critical" if level == "error" else "low")
    try:
        result = subprocess.run([notify_send, "-u", urgency, title, message[:500]], timeout=3, capture_output=True)
        return result.returncode == 0
    except Exception:
        return False


def _try_macos_osascript(title: str, message: str, level: str) -> bool:
    if sys.platform != "darwin":
        return False
    osascript = shutil.which("osascript")
    if not osascript:
        return False
    esc_title = title.replace('"', '\\"')
    esc_message = message.replace('"', '\\"')[:500]
    script = f'display notification "{esc_message}" with title "{esc_title}"'
    try:
        result = subprocess.run([osascript, "-e", script], timeout=3, capture_output=True)
        return result.returncode == 0
    except Exception:
        return False


def _dispatch_platform_notification(title: str, message: str, level: str) -> Dict[str, Any]:
    dispatch = {"platform": sys.platform, "handler": "none", "dispatched": False}
    if sys.platform == "win32":
        ok = _try_windows_toast(title, message, level)
        dispatch.update({"handler": "windows_toast", "dispatched": ok})
    elif sys.platform.startswith("linux"):
        available = bool(shutil.which("notify-send"))
        ok = _try_linux_notify_send(title, message, level) if available else False
        dispatch.update({"handler": "notify-send", "available": available, "dispatched": ok})
    elif sys.platform == "darwin":
        available = bool(shutil.which("osascript"))
        ok = _try_macos_osascript(title, message, level) if available else False
        dispatch.update({"handler": "osascript", "available": available, "dispatched": ok})
    return dispatch


def _dispatch_webhook_notification(url: str, title: str, message: str, level: str, timeout_s: float = 4.0) -> Dict[str, Any]:
    payload = {"title": title, "message": message, "level": level, "ts": _utc_iso()}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url=url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            code = int(getattr(response, "status", 0) or 0)
            ok = 200 <= code < 300
            return {"handler": "webhook", "dispatched": ok, "status_code": code}
    except urllib.error.HTTPError as exc:
        return {"handler": "webhook", "dispatched": False, "status_code": int(exc.code), "error": str(exc)}
    except Exception as exc:
        return {"handler": "webhook", "dispatched": False, "error": str(exc)}


def _dispatch_discord_notification(webhook_url: str, title: str, message: str, level: str) -> Dict[str, Any]:
    content = f"**[{level.upper()}] {title}**\n{message}".strip()
    req = urllib.request.Request(
        url=webhook_url,
        data=json.dumps({"content": content[:1900]}, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=4.0) as response:
            code = int(getattr(response, "status", 0) or 0)
            return {"handler": "discord", "dispatched": 200 <= code < 300, "status_code": code}
    except urllib.error.HTTPError as exc:
        return {"handler": "discord", "dispatched": False, "status_code": int(exc.code), "error": str(exc)}
    except Exception as exc:
        return {"handler": "discord", "dispatched": False, "error": str(exc)}


def _dispatch_email_notification(email_cfg: Dict[str, Any], title: str, message: str, level: str) -> Dict[str, Any]:
    host = str(email_cfg.get("smtp_host") or "").strip()
    sender = str(email_cfg.get("from") or "").strip()
    recipient = str(email_cfg.get("to") or "").strip()
    if not host or not sender or not recipient:
        return {"handler": "email", "dispatched": False, "error": "Missing smtp_host/from/to"}
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = f"[{level.upper()}] {title}"
    msg.set_content(message)
    port = int(email_cfg.get("smtp_port") or 587)
    username = str(email_cfg.get("username") or "").strip()
    password = str(email_cfg.get("password") or "")
    starttls = bool(email_cfg.get("starttls", True))
    timeout_s = float(email_cfg.get("timeout_seconds") or 5.0)
    try:
        with smtplib.SMTP(host=host, port=port, timeout=timeout_s) as smtp:
            if starttls:
                smtp.starttls()
            if username:
                smtp.login(username, password)
            smtp.send_message(msg)
        return {"handler": "email", "dispatched": True}
    except Exception as exc:
        return {"handler": "email", "dispatched": False, "error": str(exc)}


def _dispatch_log_only(title: str, message: str, level: str) -> Dict[str, Any]:
    return {"handler": "log_only", "dispatched": True, "note": "log-only fallback"}


def register(registry: Any, config: Dict[str, Any]) -> None:
    from runtime.tool_registry import ToolSpec, ToolRiskLevel, ToolTrustLevel, ConfirmationPolicy

    notif_cfg = config.get("notifications", {}) if isinstance(config, dict) else {}
    enabled = bool(notif_cfg.get("enabled", True))
    selected = notif_cfg.get("backends") if isinstance(notif_cfg.get("backends"), list) else None
    if isinstance(notif_cfg.get("backend"), str) and notif_cfg.get("backend"):
        selected = [str(notif_cfg.get("backend"))]
    selected_backends = [str(x).strip() for x in (selected or ["platform"]) if str(x).strip()] or ["platform"]

    project_root = Path(config.get("project_root", ".")).resolve()
    log_dir = project_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    notif_log = log_dir / "notifications.log"

    def send_notification_handler(params: Dict[str, Any]) -> str:
        if not enabled:
            return _jdump({"error": "Notifications disabled"})
        title = str(params.get("title") or "Notification").strip()
        message = str(params.get("message") or "").strip()
        level = str(params.get("level") or "info").strip().lower()
        if level not in ("info", "warning", "error", "success"):
            level = "info"
        rec = {"ts": _utc_iso(), "title": title, "message": message, "level": level}
        try:
            with notif_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
        attempts = []
        dispatched = False
        final_backend = None
        for backend in selected_backends:
            backend_name = str(backend or "").strip().lower()
            if not backend_name:
                continue
            if backend_name == "platform":
                out = _dispatch_platform_notification(title, message, level)
            elif backend_name == "webhook":
                out = _dispatch_webhook_notification(str(notif_cfg.get("webhook_url") or "").strip(), title, message, level)
            elif backend_name == "discord":
                out = _dispatch_discord_notification(str(notif_cfg.get("discord_webhook_url") or "").strip(), title, message, level)
            elif backend_name == "email":
                email_cfg = notif_cfg.get("email") if isinstance(notif_cfg.get("email"), dict) else {}
                out = _dispatch_email_notification(email_cfg, title, message, level)
            elif backend_name == "log_only":
                out = _dispatch_log_only(title, message, level)
            else:
                out = {"handler": backend_name, "dispatched": False, "error": f"Unknown backend: {backend_name}"}
            attempts.append({"backend": backend_name, **out})
            if out.get("dispatched"):
                dispatched = True
                final_backend = backend_name
                break
        if not dispatched and "log_only" not in [str(b).strip().lower() for b in selected_backends]:
            fallback = _dispatch_log_only(title, message, level)
            attempts.append({"backend": "log_only", **fallback})
            dispatched = True
            final_backend = "log_only"
        rec["dispatch"] = {"backends": selected_backends, "attempts": attempts, "dispatched": dispatched, "final_backend": final_backend}
        return _jdump({"ok": True, "logged_to": str(notif_log), "record": rec})

    registry.register(ToolSpec(
        name="send_notification",
        description="Send a notification via configured backends.",
        pack="notifications_tools",
        tags=["notifications"],
        parameters={"type": "object", "properties": {"title": {"type": "string"}, "message": {"type": "string"}, "level": {"type": "string", "enum": ["info", "warning", "error", "success"]}}, "required": ["title"]},
        handler=send_notification_handler,
        risk_level=ToolRiskLevel.IRREVERSIBLE_COMMIT,
        trust_level=ToolTrustLevel.VERIFIED_USER,
        confirmation_policy=ConfirmationPolicy.SOFT_CONFIRM,
        enabled=enabled,
    ))
