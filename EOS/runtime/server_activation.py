from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger("eos.server_activation")

_DEFAULT_ACTIVE_THRESHOLD = 0.82
_DEFAULT_IDLE_THRESHOLD = 0.45
_DEFAULT_IDLE_TIMEOUT = 300.0
_DEFAULT_COOLDOWN = 90.0
_DEFAULT_ACTIVE_GRACE = 900.0
_DEFAULT_MIN_FREE_RAM_MB = 2048.0
_DEFAULT_MIN_FREE_VRAM_MB = 1024.0
_DEFAULT_MAX_CPU_PERCENT = 85.0
_DEFAULT_MAX_GPU_PERCENT = 90.0

_DEFAULT_ROLE_NEED = {
    "tool": 0.70,
    "thinking": 0.78,
    "creativity": 0.74,
}

_TASK_NEED_OVERRIDES = {
    "tool_extraction": 0.76,
    "tooling": 0.72,
    "deep_reasoning": 0.88,
    "background_reasoning": 0.68,
    "reflection": 0.60,
    "idle_reflection": 0.56,
    "brainstorming": 0.80,
    "creative_exploration": 0.78,
    "stuck_recovery": 0.84,
}


class OperatingMode(str, Enum):
    ACTIVE_INTERACTION = "active_interaction"
    IDLE_REFLECTION = "idle_reflection"


@dataclass
class ResourceSnapshot:
    sampled_at: float
    cpu_percent: float | None = None
    ram_used_percent: float | None = None
    ram_free_mb: float | None = None
    gpu_percent: float | None = None
    vram_free_mb: float | None = None
    available_signals: list[str] = field(default_factory=list)
    source: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "sampled_at": self.sampled_at,
            "cpu_percent": self.cpu_percent,
            "ram_used_percent": self.ram_used_percent,
            "ram_free_mb": self.ram_free_mb,
            "gpu_percent": self.gpu_percent,
            "vram_free_mb": self.vram_free_mb,
            "available_signals": list(self.available_signals),
            "source": self.source,
        }


@dataclass
class ActivationRequest:
    role: str
    reason: str = ""
    task_type: str = "general"
    escalation: bool = False
    operating_mode: OperatingMode | None = None
    priority: float = 0.0
    requested_by: str = "executive"


@dataclass
class ActivationDecision:
    allowed: bool
    role: str
    mode: str
    action: str
    reason: str
    score: float
    threshold: float
    resource_ok: bool
    cooldown_remaining_seconds: float = 0.0
    resource_snapshot: ResourceSnapshot | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "role": self.role,
            "mode": self.mode,
            "action": self.action,
            "reason": self.reason,
            "score": round(self.score, 3),
            "threshold": round(self.threshold, 3),
            "resource_ok": self.resource_ok,
            "cooldown_remaining_seconds": round(self.cooldown_remaining_seconds, 2),
            "resource_snapshot": self.resource_snapshot.to_dict() if self.resource_snapshot else None,
            "detail": dict(self.detail),
        }


@dataclass
class ManagedRolePolicy:
    role: str
    residency: str
    activation_mode: str
    idle_timeout_seconds: float
    cooldown_seconds: float
    min_uptime_seconds: float
    role_need_score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "residency": self.residency,
            "activation_mode": self.activation_mode,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "cooldown_seconds": self.cooldown_seconds,
            "min_uptime_seconds": self.min_uptime_seconds,
            "role_need_score": self.role_need_score,
        }


@dataclass
class ActivationPolicyConfig:
    baseline_roles: list[str]
    auxiliary_roles: list[str]
    active_threshold: float
    idle_threshold: float
    active_grace_seconds: float
    resource_limits: dict[str, float | bool]
    role_policies: dict[str, ManagedRolePolicy]

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_roles": list(self.baseline_roles),
            "auxiliary_roles": list(self.auxiliary_roles),
            "active_threshold": self.active_threshold,
            "idle_threshold": self.idle_threshold,
            "active_grace_seconds": self.active_grace_seconds,
            "resource_limits": dict(self.resource_limits),
            "role_policies": {key: value.to_dict() for key, value in self.role_policies.items()},
        }


def _server_section(cfg: dict[str, Any], role: str) -> dict[str, Any]:
    return dict((cfg.get("servers", {}) or {}).get(role, {}) or {})


def normalize_activation_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Populate first-class activation config while remaining compatible with old configs."""
    activation = dict(cfg.get("server_activation") or {})
    legacy_on_demand = dict(cfg.get("on_demand") or {})
    servers = cfg.setdefault("servers", {})

    baseline_default = [role for role in ("primary", "tool", "vision") if role in servers]
    auxiliary_default = [role for role in ("thinking", "creativity") if role in servers]

    baseline_roles = list(activation.get("baseline_roles") or baseline_default)
    auxiliary_roles = list(activation.get("auxiliary_roles") or auxiliary_default)

    roles_cfg = dict(activation.get("roles") or {})
    # Backward compatibility: hydrate multimodal flags into servers.primary.
    legacy_primary = dict(cfg.get("primary") or {})
    if "primary" in servers and legacy_primary:
        primary_server = servers["primary"]
        for key in ("is_multimodal", "requires_mmproj", "mmproj_path"):
            if key in legacy_primary and key not in primary_server:
                primary_server[key] = legacy_primary[key]

    for role, srv_cfg in servers.items():
        role_cfg = dict(roles_cfg.get(role) or {})
        residency = role_cfg.get("residency") or srv_cfg.get("residency")
        if not residency:
            residency = "auxiliary" if role in auxiliary_roles else "resident"
        activation_mode = role_cfg.get("activation_mode") or srv_cfg.get("activation_mode")
        if not activation_mode:
            activation_mode = "on_demand" if residency == "auxiliary" else "persistent"
        role_cfg.setdefault("residency", residency)
        role_cfg.setdefault("activation_mode", activation_mode)
        role_cfg.setdefault(
            "idle_timeout_seconds",
            float(srv_cfg.get("idle_timeout_seconds", activation.get("idle_timeout_seconds", legacy_on_demand.get("idle_ttl_seconds", _DEFAULT_IDLE_TIMEOUT)))),
        )
        role_cfg.setdefault(
            "cooldown_seconds",
            float(srv_cfg.get("cooldown_seconds", activation.get("cooldown_seconds", _DEFAULT_COOLDOWN))),
        )
        role_cfg.setdefault(
            "min_uptime_seconds",
            float(srv_cfg.get("min_uptime_seconds", activation.get("min_uptime_seconds", 60.0))),
        )
        role_cfg.setdefault(
            "role_need_score",
            float(srv_cfg.get("role_need_score", _DEFAULT_ROLE_NEED.get(role, 0.75))),
        )
        srv_cfg.setdefault("residency", role_cfg["residency"])
        srv_cfg.setdefault("activation_mode", role_cfg["activation_mode"])
        roles_cfg[role] = role_cfg

    resource_limits = dict(activation.get("resource_thresholds") or {})
    resource_limits.setdefault("min_free_ram_mb", float(resource_limits.get("min_free_ram_mb", _DEFAULT_MIN_FREE_RAM_MB)))
    resource_limits.setdefault("min_free_vram_mb", float(resource_limits.get("min_free_vram_mb", _DEFAULT_MIN_FREE_VRAM_MB)))
    resource_limits.setdefault("max_cpu_percent", float(resource_limits.get("max_cpu_percent", _DEFAULT_MAX_CPU_PERCENT)))
    resource_limits.setdefault("max_gpu_percent", float(resource_limits.get("max_gpu_percent", _DEFAULT_MAX_GPU_PERCENT)))
    resource_limits.setdefault("allow_when_metrics_unavailable", bool(resource_limits.get("allow_when_metrics_unavailable", True)))

    activation.setdefault("baseline_roles", baseline_roles)
    activation.setdefault("auxiliary_roles", auxiliary_roles)
    activation.setdefault("active_mode_threshold", float(activation.get("active_mode_threshold", _DEFAULT_ACTIVE_THRESHOLD)))
    activation.setdefault("idle_mode_threshold", float(activation.get("idle_mode_threshold", _DEFAULT_IDLE_THRESHOLD)))
    activation.setdefault("active_interaction_grace_seconds", float(activation.get("active_interaction_grace_seconds", _DEFAULT_ACTIVE_GRACE)))
    activation.setdefault("resource_thresholds", resource_limits)
    activation.setdefault("roles", roles_cfg)
    activation.setdefault("startup_baseline_only", True)

    cfg["server_activation"] = activation
    return cfg


class OperatingModeResolver:
    def __init__(self, cfg: dict[str, Any], posture_provider: Callable[[], dict[str, Any] | None] | None = None) -> None:
        activation = cfg.get("server_activation", {})
        self._active_grace = float(activation.get("active_interaction_grace_seconds", _DEFAULT_ACTIVE_GRACE))
        self._posture_provider = posture_provider

    def resolve(self, *, explicit_mode: OperatingMode | str | None = None, last_interaction_age_s: float | None = None) -> OperatingMode:
        if explicit_mode:
            return explicit_mode if isinstance(explicit_mode, OperatingMode) else OperatingMode(str(explicit_mode))

        posture = self._posture_provider() if self._posture_provider else None
        phase = str((posture or {}).get("phase") or "")
        if phase in {"EARLY_NIGHT", "DEEP_NIGHT", "PREWAKE"}:
            return OperatingMode.IDLE_REFLECTION

        if last_interaction_age_s is not None and last_interaction_age_s > self._active_grace:
            return OperatingMode.IDLE_REFLECTION

        return OperatingMode.ACTIVE_INTERACTION


class ResourceSnapshotProvider:
    def __init__(self, sensor_provider: Callable[[], Any] | None = None) -> None:
        self._sensor_provider = sensor_provider

    def snapshot(self) -> ResourceSnapshot:
        sensor_snapshot = None
        if self._sensor_provider is not None:
            try:
                sensor_snapshot = self._sensor_provider()
            except Exception as exc:
                logger.debug("Sensor provider failed: %s", exc)

        if sensor_snapshot is not None:
            snap = self._from_sensor_snapshot(sensor_snapshot)
            if snap is not None:
                return snap

        return self._from_psutil()

    def _from_sensor_snapshot(self, sensor_snapshot: Any) -> ResourceSnapshot | None:
        try:
            cpu_percent = getattr(getattr(sensor_snapshot, "cpu", None), "cpu_percent", None)
            ram = getattr(sensor_snapshot, "ram", None)
            gpu = (getattr(sensor_snapshot, "gpus", None) or [None])[0]
            available_signals: list[str] = []
            if cpu_percent is not None:
                available_signals.append("cpu")
            if ram is not None:
                available_signals.append("ram")
            if gpu is not None:
                available_signals.append("gpu")
            vram_free_mb = None
            if gpu is not None and getattr(gpu, "mem_total_bytes", None) is not None and getattr(gpu, "mem_used_bytes", None) is not None:
                vram_free_mb = max(0.0, float(gpu.mem_total_bytes - gpu.mem_used_bytes) / (1024 * 1024))
            ram_free_mb = None
            if ram is not None and getattr(ram, "total_bytes", None) is not None and getattr(ram, "used_bytes", None) is not None:
                ram_free_mb = max(0.0, float(ram.total_bytes - ram.used_bytes) / (1024 * 1024))
            return ResourceSnapshot(
                sampled_at=time.time(),
                cpu_percent=float(cpu_percent) if cpu_percent is not None else None,
                ram_used_percent=float(getattr(ram, "used_percent", 0.0)) if ram is not None else None,
                ram_free_mb=ram_free_mb,
                gpu_percent=float(getattr(gpu, "util_percent", 0.0)) if gpu is not None and getattr(gpu, "util_percent", None) is not None else None,
                vram_free_mb=vram_free_mb,
                available_signals=available_signals,
                source="system_sensors",
            )
        except Exception as exc:
            logger.debug("Failed to convert SensorPoller snapshot: %s", exc)
            return None

    def _from_psutil(self) -> ResourceSnapshot:
        available_signals: list[str] = []
        cpu_percent = None
        ram_used_percent = None
        ram_free_mb = None
        try:
            import psutil

            cpu_percent = float(psutil.cpu_percent(interval=0.1))
            available_signals.append("cpu")
            vm = psutil.virtual_memory()
            ram_used_percent = float(vm.percent)
            ram_free_mb = float(vm.available) / (1024 * 1024)
            available_signals.append("ram")
        except Exception as exc:
            logger.debug("psutil resource sampling unavailable: %s", exc)

        gpu_percent = None
        vram_free_mb = None
        try:
            import pynvml

            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            gpu_percent = float(util.gpu)
            vram_free_mb = float(mem.free) / (1024 * 1024)
            available_signals.append("gpu")
        except Exception:
            pass

        return ResourceSnapshot(
            sampled_at=time.time(),
            cpu_percent=cpu_percent,
            ram_used_percent=ram_used_percent,
            ram_free_mb=ram_free_mb,
            gpu_percent=gpu_percent,
            vram_free_mb=vram_free_mb,
            available_signals=available_signals,
            source="live_probe",
        )


class ServerActivationPolicy:
    def __init__(
        self,
        cfg: dict[str, Any],
        *,
        resource_provider: ResourceSnapshotProvider,
        operating_mode_resolver: OperatingModeResolver,
    ) -> None:
        cfg = normalize_activation_config(cfg)
        activation = cfg.get("server_activation", {})
        self._cfg = cfg
        self._resource_provider = resource_provider
        self._mode_resolver = operating_mode_resolver
        self._config = self._build_config(activation)

    @property
    def config(self) -> ActivationPolicyConfig:
        return self._config

    def is_managed_role(self, role: str) -> bool:
        policy = self._config.role_policies.get(role)
        return bool(policy and policy.activation_mode == "on_demand")

    def residency_for(self, role: str) -> str:
        policy = self._config.role_policies.get(role)
        return policy.residency if policy else "resident"

    def activation_mode_for(self, role: str) -> str:
        policy = self._config.role_policies.get(role)
        return policy.activation_mode if policy else "persistent"

    def evaluate(
        self,
        request: ActivationRequest,
        *,
        last_interaction_age_s: float | None,
        cooldown_remaining_seconds: float = 0.0,
    ) -> ActivationDecision:
        role_policy = self._config.role_policies.get(request.role)
        if role_policy is None:
            return ActivationDecision(
                allowed=False,
                role=request.role,
                mode="unknown",
                action="deny",
                reason="no activation policy configured for role",
                score=0.0,
                threshold=1.0,
                resource_ok=False,
                detail={"requested_by": request.requested_by},
            )

        mode = self._mode_resolver.resolve(
            explicit_mode=request.operating_mode,
            last_interaction_age_s=last_interaction_age_s,
        )
        resource_snapshot = self._resource_provider.snapshot()
        resource_ok, resource_reason, resource_detail = self._resources_allow(resource_snapshot)
        threshold = self._threshold_for(mode)
        score = self._score_request(request, role_policy, mode)

        if cooldown_remaining_seconds > 0 and score < 0.98:
            return ActivationDecision(
                allowed=False,
                role=request.role,
                mode=mode.value,
                action="deny",
                reason="cooldown active",
                score=score,
                threshold=threshold,
                resource_ok=resource_ok,
                cooldown_remaining_seconds=cooldown_remaining_seconds,
                resource_snapshot=resource_snapshot,
                detail={
                    "requested_by": request.requested_by,
                    "task_type": request.task_type,
                    "resource_reason": resource_reason,
                    **resource_detail,
                },
            )

        if not resource_ok:
            return ActivationDecision(
                allowed=False,
                role=request.role,
                mode=mode.value,
                action="deny",
                reason=resource_reason,
                score=score,
                threshold=threshold,
                resource_ok=False,
                resource_snapshot=resource_snapshot,
                detail={
                    "requested_by": request.requested_by,
                    "task_type": request.task_type,
                    **resource_detail,
                },
            )

        if score < threshold:
            return ActivationDecision(
                allowed=False,
                role=request.role,
                mode=mode.value,
                action="deny",
                reason="escalation threshold not met for current operating mode",
                score=score,
                threshold=threshold,
                resource_ok=True,
                resource_snapshot=resource_snapshot,
                detail={
                    "requested_by": request.requested_by,
                    "task_type": request.task_type,
                    "role_need_score": role_policy.role_need_score,
                    **resource_detail,
                },
            )

        return ActivationDecision(
            allowed=True,
            role=request.role,
            mode=mode.value,
            action="start",
            reason="policy allows on-demand activation",
            score=score,
            threshold=threshold,
            resource_ok=True,
            resource_snapshot=resource_snapshot,
            detail={
                "requested_by": request.requested_by,
                "task_type": request.task_type,
                "role_need_score": role_policy.role_need_score,
                **resource_detail,
            },
        )

    def _build_config(self, activation: dict[str, Any]) -> ActivationPolicyConfig:
        roles_cfg = activation.get("roles", {})
        role_policies: dict[str, ManagedRolePolicy] = {}
        for role, role_cfg in roles_cfg.items():
            role_policies[role] = ManagedRolePolicy(
                role=role,
                residency=str(role_cfg.get("residency", "resident")),
                activation_mode=str(role_cfg.get("activation_mode", "persistent")),
                idle_timeout_seconds=float(role_cfg.get("idle_timeout_seconds", _DEFAULT_IDLE_TIMEOUT)),
                cooldown_seconds=float(role_cfg.get("cooldown_seconds", _DEFAULT_COOLDOWN)),
                min_uptime_seconds=float(role_cfg.get("min_uptime_seconds", 60.0)),
                role_need_score=float(role_cfg.get("role_need_score", _DEFAULT_ROLE_NEED.get(role, 0.75))),
            )
        return ActivationPolicyConfig(
            baseline_roles=list(activation.get("baseline_roles", [])),
            auxiliary_roles=list(activation.get("auxiliary_roles", [])),
            active_threshold=float(activation.get("active_mode_threshold", _DEFAULT_ACTIVE_THRESHOLD)),
            idle_threshold=float(activation.get("idle_mode_threshold", _DEFAULT_IDLE_THRESHOLD)),
            active_grace_seconds=float(activation.get("active_interaction_grace_seconds", _DEFAULT_ACTIVE_GRACE)),
            resource_limits=dict(activation.get("resource_thresholds", {})),
            role_policies=role_policies,
        )

    def _threshold_for(self, mode: OperatingMode) -> float:
        if mode == OperatingMode.IDLE_REFLECTION:
            return self._config.idle_threshold
        return self._config.active_threshold

    def _score_request(self, request: ActivationRequest, role_policy: ManagedRolePolicy, mode: OperatingMode) -> float:
        score = role_policy.role_need_score
        score = max(score, _TASK_NEED_OVERRIDES.get(request.task_type, score))
        if request.escalation:
            score += 0.08
        score += float(request.priority or 0.0)
        if mode == OperatingMode.IDLE_REFLECTION and request.task_type in {"reflection", "idle_reflection", "background_reasoning", "creative_exploration", "brainstorming"}:
            score += 0.05
        return max(0.0, min(1.0, score))

    def _resources_allow(self, snapshot: ResourceSnapshot) -> tuple[bool, str, dict[str, Any]]:
        limits = self._config.resource_limits
        detail = {"resource_source": snapshot.source, "available_signals": list(snapshot.available_signals)}
        allow_unknown = bool(limits.get("allow_when_metrics_unavailable", True))

        if snapshot.ram_free_mb is not None:
            detail["ram_free_mb"] = round(snapshot.ram_free_mb, 2)
            if snapshot.ram_free_mb < float(limits.get("min_free_ram_mb", _DEFAULT_MIN_FREE_RAM_MB)):
                return False, "insufficient free RAM for auxiliary activation", detail
        elif not allow_unknown:
            return False, "RAM telemetry unavailable", detail

        if snapshot.cpu_percent is not None:
            detail["cpu_percent"] = round(snapshot.cpu_percent, 2)
            if snapshot.cpu_percent > float(limits.get("max_cpu_percent", _DEFAULT_MAX_CPU_PERCENT)):
                return False, "CPU load too high for auxiliary activation", detail
        elif not allow_unknown:
            return False, "CPU telemetry unavailable", detail

        if snapshot.gpu_percent is not None:
            detail["gpu_percent"] = round(snapshot.gpu_percent, 2)
            if snapshot.gpu_percent > float(limits.get("max_gpu_percent", _DEFAULT_MAX_GPU_PERCENT)):
                return False, "GPU load too high for auxiliary activation", detail

        if snapshot.vram_free_mb is not None:
            detail["vram_free_mb"] = round(snapshot.vram_free_mb, 2)
            if snapshot.vram_free_mb < float(limits.get("min_free_vram_mb", _DEFAULT_MIN_FREE_VRAM_MB)):
                return False, "insufficient free VRAM for auxiliary activation", detail

        return True, "resource headroom available", detail
