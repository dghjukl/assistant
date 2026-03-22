"""
EOS — System Sensors
Hardware and runtime self-observation layer.

Provides a lightweight polling loop that samples CPU, RAM, disk, and (optionally)
GPU metrics using psutil and pynvml.  Samples are stored in small in-memory
ring buffers so callers can read current state or a short history without
hitting the OS on every request.

Architecture
------------
SensorPoller runs as a daemon thread (or driven by the server's async loop via
``poll_once()``).  All data is kept in-memory — nothing is persisted to disk.

Sample types
------------
  CpuSample         — cpu_percent, load_avg_1m, thread_count, process_count
  RamSample         — used_bytes, total_bytes, used_percent, swap_used_bytes
  GpuSample         — gpu_id, util_percent, mem_used_bytes, mem_total_bytes,
                       temperature_c, power_draw_w (all optional)
  DiskSample        — path, used_bytes, total_bytes, used_percent
  ServerProbeSample — endpoint, latency_ms, status_code, reachable

OperationalSnapshot
-------------------
  Point-in-time struct containing the most recent sample of every type.
  ``SensorPoller.snapshot()`` returns one and is the primary API.

``build_operational_summary()`` converts a snapshot to two strings:
  admin_text — rich multi-line text for the admin panel
  model_text — compact one-liner for injection into the model's system context

Usage
-----
    from runtime.system_sensors import SensorPoller

    poller = SensorPoller(cfg=cfg, topology=topology)
    poller.start()                    # background thread, every 30 s
    snap = poller.snapshot()          # OperationalSnapshot
    print(snap.admin_summary())

    # Or without a thread (call from an async loop):
    await poller.poll_once()
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("eos.system_sensors")

UTC = timezone.utc


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# ── Sample dataclasses ────────────────────────────────────────────────────────

@dataclass
class CpuSample:
    sampled_at: str
    cpu_percent: float
    load_avg_1m: float
    thread_count: int
    process_count: int


@dataclass
class RamSample:
    sampled_at: str
    used_bytes: int
    total_bytes: int
    used_percent: float
    swap_used_bytes: int
    swap_total_bytes: int


@dataclass
class GpuSample:
    sampled_at: str
    gpu_id: int
    name: str
    util_percent: Optional[float]
    mem_used_bytes: Optional[int]
    mem_total_bytes: Optional[int]
    temperature_c: Optional[float]
    power_draw_w: Optional[float]


@dataclass
class DiskSample:
    sampled_at: str
    path: str
    used_bytes: int
    total_bytes: int
    used_percent: float


@dataclass
class ServerProbeSample:
    sampled_at: str
    role: str
    endpoint: str
    reachable: bool
    latency_ms: Optional[float]
    status_code: Optional[int]


# ── Operational snapshot ──────────────────────────────────────────────────────

@dataclass
class OperationalSnapshot:
    """Point-in-time view of all sensor readings."""
    sampled_at: str
    cpu: Optional[CpuSample]
    ram: Optional[RamSample]
    gpus: list[GpuSample]
    disks: list[DiskSample]
    servers: list[ServerProbeSample]

    def admin_summary(self) -> str:
        """Multi-line rich text for the admin panel."""
        lines = [f"System Sensors — {self.sampled_at}"]
        if self.cpu:
            lines.append(
                f"  CPU: {self.cpu.cpu_percent:.1f}%  "
                f"load_avg={self.cpu.load_avg_1m:.2f}  "
                f"threads={self.cpu.thread_count}"
            )
        if self.ram:
            used_gb = self.ram.used_bytes / (1024 ** 3)
            total_gb = self.ram.total_bytes / (1024 ** 3)
            lines.append(
                f"  RAM: {used_gb:.1f}/{total_gb:.1f} GB  "
                f"({self.ram.used_percent:.1f}%)"
            )
        for g in self.gpus:
            mem_used = (g.mem_used_bytes or 0) / (1024 ** 2)
            mem_total = (g.mem_total_bytes or 0) / (1024 ** 2)
            lines.append(
                f"  GPU[{g.gpu_id}] {g.name}: "
                f"util={g.util_percent or 0:.1f}%  "
                f"VRAM={mem_used:.0f}/{mem_total:.0f} MB  "
                f"temp={g.temperature_c or 0:.0f}°C"
            )
        for d in self.disks:
            used_gb = d.used_bytes / (1024 ** 3)
            total_gb = d.total_bytes / (1024 ** 3)
            lines.append(
                f"  Disk ({d.path}): {used_gb:.1f}/{total_gb:.1f} GB  "
                f"({d.used_percent:.1f}%)"
            )
        for s in self.servers:
            status = "✓" if s.reachable else "✗"
            lat = f" {s.latency_ms:.0f}ms" if s.latency_ms is not None else ""
            lines.append(f"  Server[{s.role}]: {status}{lat}")
        return "\n".join(lines)

    def model_summary(self) -> str:
        """Compact one-liner for model system-prompt injection."""
        parts = []
        if self.cpu:
            parts.append(f"cpu={self.cpu.cpu_percent:.0f}%")
        if self.ram:
            parts.append(f"ram={self.ram.used_percent:.0f}%")
        if self.gpus:
            g = self.gpus[0]
            parts.append(f"gpu={g.util_percent or 0:.0f}%")
        if self.disks:
            d = self.disks[0]
            parts.append(f"disk={d.used_percent:.0f}%")
        server_ok = sum(1 for s in self.servers if s.reachable)
        server_total = len(self.servers)
        if server_total:
            parts.append(f"servers={server_ok}/{server_total}")
        return "[system: " + " ".join(parts) + "]" if parts else "[system: unavailable]"

    def to_dict(self) -> dict:
        """JSON-serialisable dict for admin API."""
        def _gpu(g: GpuSample) -> dict:
            return {
                "gpu_id": g.gpu_id, "name": g.name,
                "util_percent": g.util_percent,
                "mem_used_mb": round(g.mem_used_bytes / (1024**2), 1) if g.mem_used_bytes else None,
                "mem_total_mb": round(g.mem_total_bytes / (1024**2), 1) if g.mem_total_bytes else None,
                "temperature_c": g.temperature_c,
                "power_draw_w": g.power_draw_w,
                "sampled_at": g.sampled_at,
            }
        return {
            "sampled_at": self.sampled_at,
            "cpu": {
                "cpu_percent": self.cpu.cpu_percent,
                "load_avg_1m": self.cpu.load_avg_1m,
                "thread_count": self.cpu.thread_count,
                "process_count": self.cpu.process_count,
            } if self.cpu else None,
            "ram": {
                "used_percent": self.ram.used_percent,
                "used_gb": round(self.ram.used_bytes / (1024**3), 2),
                "total_gb": round(self.ram.total_bytes / (1024**3), 2),
                "swap_used_gb": round(self.ram.swap_used_bytes / (1024**3), 2),
            } if self.ram else None,
            "gpus": [_gpu(g) for g in self.gpus],
            "disks": [
                {
                    "path": d.path,
                    "used_percent": d.used_percent,
                    "used_gb": round(d.used_bytes / (1024**3), 2),
                    "total_gb": round(d.total_bytes / (1024**3), 2),
                }
                for d in self.disks
            ],
            "servers": [
                {
                    "role": s.role,
                    "reachable": s.reachable,
                    "latency_ms": s.latency_ms,
                    "status_code": s.status_code,
                }
                for s in self.servers
            ],
        }


# ── SensorPoller ──────────────────────────────────────────────────────────────

class SensorPoller:
    """
    Background sensor polling loop.

    Polls CPU, RAM, GPU, disk, and server health on a configurable interval.
    Thread-safe.  Call ``start()`` to run as a daemon thread, or ``poll_once()``
    to sample synchronously (e.g. from an async loop).

    Parameters
    ----------
    cfg : dict
        Runtime config dict.  Reads ``sensors.poll_interval_seconds`` (default 30)
        and ``sensors.disk_paths`` (default ["."]).
    topology : RuntimeTopology, optional
        If provided, server endpoints are probed.
    """

    def __init__(self, cfg: dict, topology: Any = None) -> None:
        self._cfg = cfg
        self._topology = topology
        sensor_cfg = cfg.get("sensors", {})
        self._interval = float(sensor_cfg.get("poll_interval_seconds", 30))
        self._disk_paths: list[str] = sensor_cfg.get("disk_paths", ["."])

        self._lock = threading.Lock()
        self._latest: Optional[OperationalSnapshot] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Try importing psutil once; degrade gracefully if absent
        self._psutil_available = False
        try:
            import psutil  # noqa: F401
            self._psutil_available = True
        except ImportError:
            logger.warning("psutil not available — hardware sensors disabled")

        # Try importing pynvml once for GPU support
        self._nvml_available = False
        try:
            import pynvml
            pynvml.nvmlInit()
            self._nvml_available = True
        except Exception:
            pass  # GPU sensors silently disabled

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background polling thread (daemon)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="eos.system_sensors", daemon=True
        )
        self._thread.start()
        logger.info("SensorPoller started (interval=%.0fs)", self._interval)

    def stop(self) -> None:
        """Stop the background polling thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def snapshot(self) -> Optional[OperationalSnapshot]:
        """Return the most recent snapshot (None if no poll has completed yet)."""
        with self._lock:
            return self._latest

    def poll_once(self) -> OperationalSnapshot:
        """Sample all sensors synchronously and return a snapshot."""
        snap = self._collect()
        with self._lock:
            self._latest = snap
        return snap

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                snap = self._collect()
                with self._lock:
                    self._latest = snap
            except Exception as exc:
                logger.debug("SensorPoller error: %s", exc)
            self._stop_event.wait(timeout=self._interval)

    def _collect(self) -> OperationalSnapshot:
        now = _now_iso()
        cpu = self._collect_cpu()
        ram = self._collect_ram()
        gpus = self._collect_gpus()
        disks = self._collect_disks()
        servers = self._collect_servers()
        return OperationalSnapshot(
            sampled_at=now,
            cpu=cpu,
            ram=ram,
            gpus=gpus,
            disks=disks,
            servers=servers,
        )

    def _collect_cpu(self) -> Optional[CpuSample]:
        if not self._psutil_available:
            return None
        try:
            import psutil
            load = psutil.getloadavg()[0] if hasattr(psutil, "getloadavg") else 0.0
            return CpuSample(
                sampled_at=_now_iso(),
                cpu_percent=psutil.cpu_percent(interval=0.2),
                load_avg_1m=load,
                thread_count=sum(p.num_threads() for p in psutil.process_iter(["num_threads"]) if p.info["num_threads"]),
                process_count=len(psutil.pids()),
            )
        except Exception as e:
            logger.debug("CPU sample error: %s", e)
            return None

    def _collect_ram(self) -> Optional[RamSample]:
        if not self._psutil_available:
            return None
        try:
            import psutil
            vm = psutil.virtual_memory()
            sw = psutil.swap_memory()
            return RamSample(
                sampled_at=_now_iso(),
                used_bytes=vm.used,
                total_bytes=vm.total,
                used_percent=vm.percent,
                swap_used_bytes=sw.used,
                swap_total_bytes=sw.total,
            )
        except Exception as e:
            logger.debug("RAM sample error: %s", e)
            return None

    def _collect_gpus(self) -> list[GpuSample]:
        if not self._nvml_available:
            return []
        samples = []
        try:
            import pynvml
            count = pynvml.nvmlDeviceGetCount()
            for i in range(count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode()
                try:
                    util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                    util_pct = float(util.gpu)
                except Exception:
                    util_pct = None
                try:
                    mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                    mem_used = mem.used
                    mem_total = mem.total
                except Exception:
                    mem_used = mem_total = None
                try:
                    temp = float(pynvml.nvmlDeviceGetTemperature(
                        handle, pynvml.NVML_TEMPERATURE_GPU
                    ))
                except Exception:
                    temp = None
                try:
                    power = pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0
                except Exception:
                    power = None
                samples.append(GpuSample(
                    sampled_at=_now_iso(),
                    gpu_id=i, name=name,
                    util_percent=util_pct,
                    mem_used_bytes=mem_used, mem_total_bytes=mem_total,
                    temperature_c=temp, power_draw_w=power,
                ))
        except Exception as e:
            logger.debug("GPU sample error: %s", e)
        return samples

    def _collect_disks(self) -> list[DiskSample]:
        if not self._psutil_available:
            return []
        samples = []
        for path in self._disk_paths:
            try:
                import psutil
                usage = psutil.disk_usage(path)
                samples.append(DiskSample(
                    sampled_at=_now_iso(),
                    path=path,
                    used_bytes=usage.used,
                    total_bytes=usage.total,
                    used_percent=usage.percent,
                ))
            except Exception as e:
                logger.debug("Disk sample error (%s): %s", path, e)
        return samples

    def _collect_servers(self) -> list[ServerProbeSample]:
        if not self._topology:
            return []
        import urllib.request
        import urllib.error
        samples = []
        for role, state in self._topology.servers.items():
            if state.is_absent():
                continue
            endpoint = state.endpoint
            url = f"{endpoint}/health"
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(url, timeout=3) as resp:
                    latency_ms = (time.monotonic() - t0) * 1000
                    samples.append(ServerProbeSample(
                        sampled_at=_now_iso(),
                        role=role, endpoint=endpoint,
                        reachable=True,
                        latency_ms=latency_ms,
                        status_code=resp.status,
                    ))
            except Exception:
                latency_ms = (time.monotonic() - t0) * 1000
                samples.append(ServerProbeSample(
                    sampled_at=_now_iso(),
                    role=role, endpoint=endpoint,
                    reachable=False,
                    latency_ms=latency_ms,
                    status_code=None,
                ))
        return samples
