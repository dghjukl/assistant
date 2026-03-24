from __future__ import annotations

import importlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from runtime.boot import load_config
from runtime.launch_catalog import service_label
from runtime.server_activation import normalize_activation_config
from runtime.topology import RuntimeTopology, build_topology_from_config

logger = logging.getLogger("eos.discovery")
_LOGGED_DEGRADED_COMPONENTS: set[str] = set()


@dataclass
class ServiceProbe:
    key: str
    label: str
    status: str
    detail: str = ""
    endpoint: str | None = None
    latency_ms: int | None = None
    fallback: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "endpoint": self.endpoint,
            "latency_ms": self.latency_ms,
            "fallback": self.fallback,
        }


@dataclass
class RuntimeDiscovery:
    config: dict[str, Any]
    topology: RuntimeTopology
    services: dict[str, ServiceProbe]
    capabilities: dict[str, str]

    def service_lines(self) -> list[str]:
        lines: list[str] = []
        ordered = ["primary", "tool", "thinking", "creativity", "vision", "stt", "tts"]
        for key in ordered:
            probe = self.services.get(key)
            if not probe:
                continue
            extra = f" ({probe.detail})" if probe.detail else ""
            lines.append(f"{probe.label}: {probe.status}{extra}")
        return lines

    def capability_lines(self) -> list[str]:
        ordered = ["chat", "tools", "reasoning", "creativity", "vision", "voice"]
        return [f"{name}: {self.capabilities[name]}" for name in ordered if name in self.capabilities]

    def to_dict(self) -> dict[str, Any]:
        return {
            "services": {key: value.to_dict() for key, value in self.services.items()},
            "capabilities": dict(self.capabilities),
            "topology": self.topology.status_summary(),
        }


def load_runtime_config(config_path: str | Path) -> dict[str, Any]:
    return normalize_activation_config(load_config(config_path))


def discover_runtime(config_path: str | Path, root: Path | None = None) -> RuntimeDiscovery:
    config_path = Path(config_path)
    if root is None:
        root = config_path.parent

    cfg = load_runtime_config(config_path)
    topology = build_topology_from_config(cfg)
    threshold_ms = int(cfg.get("health_probe", {}).get("degraded_latency_ms", 2000))

    services: dict[str, ServiceProbe] = {}
    for role, srv_cfg in cfg.get("servers", {}).items():
        try:
            label = service_label(role)
        except KeyError:
            label = role.replace("_", " ").title()
        host = srv_cfg.get("host", "127.0.0.1")
        port = srv_cfg.get("port", 0)
        endpoint = f"http://{host}:{port}"

        if not srv_cfg.get("enabled", False):
            topology.mark_absent(role, intentional=True)
            services[role] = ServiceProbe(
                key=role,
                label=label,
                status="unavailable",
                detail="disabled in config",
                endpoint=endpoint,
            )
            continue

        activation_mode = str(srv_cfg.get("activation_mode", "persistent"))
        residency = str(srv_cfg.get("residency", "resident"))

        status, latency_ms, detail = _probe_http_health(endpoint, threshold_ms)
        if status in ("active", "degraded"):
            topology.mark_ready(role)
        elif activation_mode == "on_demand" and residency == "auxiliary":
            topology.mark_absent(role, intentional=True)
            status = "degraded"
            detail = "managed on-demand; currently inactive"
        else:
            topology.mark_error(role, detail)

        fallback = None
        if role in {"tool", "thinking", "creativity"} and status == "unavailable":
            fallback = "fallback to main"
            detail = f"{detail}; fallback to main" if detail else "fallback to main"
        elif role in {"tool", "thinking", "creativity"} and activation_mode == "on_demand" and status == "degraded":
            fallback = "on-demand auxiliary"

        services[role] = ServiceProbe(
            key=role,
            label=label,
            status=status,
            detail=detail,
            endpoint=endpoint,
            latency_ms=latency_ms,
            fallback=fallback,
        )

    services["stt"] = _probe_stt(cfg, root)
    services["tts"] = _probe_tts(cfg, root)

    capabilities = _build_capabilities(cfg, services)
    return RuntimeDiscovery(cfg, topology, services, capabilities)


def format_runtime_summary(discovery: RuntimeDiscovery) -> str:
    lines = [*discovery.service_lines(), "", "Effective capabilities:", ""]
    lines.extend(discovery.capability_lines())
    return "\n".join(lines)


def _probe_http_health(endpoint: str, threshold_ms: int) -> tuple[str, int | None, str]:
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{endpoint}/health")
        latency_ms = int((time.perf_counter() - started) * 1000)
        body = resp.text.strip()
        if resp.status_code != 200:
            return "unavailable", latency_ms, f"health returned HTTP {resp.status_code}"
        if not body:
            return "degraded", latency_ms, "health endpoint returned an empty body"
        if latency_ms >= threshold_ms:
            return "degraded", latency_ms, f"health ok but latency is {latency_ms} ms"
        return "active", latency_ms, f"health ok in {latency_ms} ms"
    except httpx.HTTPError as exc:
        return "unavailable", None, str(exc)
    except Exception as exc:  # pragma: no cover - defensive
        return "unavailable", None, str(exc)


def _log_degraded_once(component: str, reason: str, impact: str) -> None:
    if component in _LOGGED_DEGRADED_COMPONENTS:
        return
    _LOGGED_DEGRADED_COMPONENTS.add(component)
    logger.warning("component=%s status=degraded reason=%s impact=%s", component, reason, impact)


def _probe_dependency(module_name: str) -> tuple[bool, str | None]:
    try:
        importlib.import_module(module_name)
        return True, None
    except Exception as exc:  # pragma: no cover - dependency/OS specific
        return False, f"{module_name}: {type(exc).__name__}: {exc}"


def _probe_stt(cfg: dict[str, Any], root: Path) -> ServiceProbe:
    stt_cfg = cfg.get("stt", {})
    if not stt_cfg:
        return ServiceProbe("stt", "STT", "unavailable", "not configured")

    model_path = root / stt_cfg.get("model_path", "")
    dep_checks = [
        _probe_dependency("faster_whisper"),
        _probe_dependency("sounddevice"),
        _probe_dependency("numpy"),
    ]
    failures = [detail for ok, detail in dep_checks if not ok and detail]

    try:
        from services import stt as stt_service

        stt_runtime_available = bool(getattr(stt_service, "STT_AVAILABLE", False))
        stt_runtime_reason = getattr(stt_service, "STT_IMPORT_ERROR", "runtime STT import error")
    except Exception as exc:
        stt_runtime_available = False
        stt_runtime_reason = f"stt module load failed: {type(exc).__name__}: {exc}"

    if model_path.is_file() and not failures and stt_runtime_available:
        return ServiceProbe("stt", "STT", "active", "model and runtime dependencies ready")

    if not model_path.is_file():
        reason = f"model not found: {model_path}"
        _log_degraded_once("stt", reason, "voice input unavailable")
        return ServiceProbe("stt", "STT", "unavailable", reason)

    reason = "; ".join(failures + ([str(stt_runtime_reason)] if not stt_runtime_available else []))
    _log_degraded_once("stt", reason, "voice input unavailable")
    return ServiceProbe("stt", "STT", "degraded", reason)


def _probe_tts(cfg: dict[str, Any], root: Path) -> ServiceProbe:
    tts_cfg = cfg.get("tts", {})
    if not tts_cfg:
        return ServiceProbe("tts", "TTS", "unavailable", "not configured")

    binary = root / tts_cfg.get("binary", "")
    model = root / tts_cfg.get("model_path", "")
    sidecar = Path(str(model) + ".json")

    missing: list[str] = []
    if not binary.is_file():
        missing.append(f"binary {binary}")
    if not model.is_file():
        missing.append(f"model {model}")
    if not sidecar.is_file():
        missing.append(f"voice config {sidecar}")

    if not missing:
        return ServiceProbe("tts", "TTS", "active", "binary and voice files present")

    reason = f"missing {', '.join(missing)}"
    _log_degraded_once("tts", reason, "voice output unavailable")
    if len(missing) < 3:
        return ServiceProbe("tts", "TTS", "degraded", reason)
    return ServiceProbe("tts", "TTS", "unavailable", reason)


def _build_capabilities(cfg: dict[str, Any], services: dict[str, ServiceProbe]) -> dict[str, str]:
    primary = services.get("primary")
    tool = services.get("tool")
    thinking = services.get("thinking")
    creativity = services.get("creativity")
    vision = services.get("vision")
    stt = services.get("stt")
    tts = services.get("tts")

    caps: dict[str, str] = {}

    caps["chat"] = _status_from_primary(primary)

    if caps["chat"] == "unavailable":
        caps["tools"] = "unavailable"
        caps["reasoning"] = "unavailable"
        caps["creativity"] = "unavailable"
    else:
        caps["tools"] = "available" if _is_usable_or_elastic(tool) else "degraded"
        caps["reasoning"] = "available" if _is_usable_or_elastic(thinking) else "degraded"
        caps["creativity"] = "available" if _is_usable_or_elastic(creativity) else "degraded"

    primary_cfg = cfg.get("servers", {}).get("primary", {})
    if _is_usable(vision):
        caps["vision"] = "available" if _is_active(vision) else "degraded"
    elif primary_cfg.get("is_multimodal", False) and caps["chat"] != "unavailable":
        caps["vision"] = "available"
    else:
        caps["vision"] = "unavailable"

    stt_ok = _is_usable(stt)
    tts_ok = _is_usable(tts)
    if stt_ok and tts_ok:
        caps["voice"] = "available"
    elif stt_ok or tts_ok:
        caps["voice"] = "degraded"
    else:
        caps["voice"] = "unavailable"

    return caps


def _status_from_primary(primary: ServiceProbe | None) -> str:
    if primary is None:
        return "unavailable"
    if primary.status == "active":
        return "available"
    if primary.status == "degraded":
        return "degraded"
    return "unavailable"


def _is_active(probe: ServiceProbe | None) -> bool:
    return probe is not None and probe.status == "active"


def _is_usable(probe: ServiceProbe | None) -> bool:
    return probe is not None and probe.status in {"active", "degraded"}


def _is_usable_or_elastic(probe: ServiceProbe | None) -> bool:
    return probe is not None and probe.status in {"active", "degraded"}
