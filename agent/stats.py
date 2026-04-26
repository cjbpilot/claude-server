"""Background machine-utilisation sampler.

Runs as a task on the agent's event loop, ticking every 5s. Keeps roughly
the last hour of samples in memory in a bounded deque. Cheap to query for
windowed summaries (avg/min/max/p50/p95).

Sampled metrics:
  - CPU: overall percent + per-core
  - Memory: RAM percent + used GB + swap percent
  - Disk: percent + free GB on the workspace volume
  - Network: bytes/s sent and received (delta from previous sample)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import psutil

log = logging.getLogger("agent.stats")

SAMPLE_INTERVAL_S = 5
WINDOW_S = 60 * 60  # keep last hour
MAX_SAMPLES = WINDOW_S // SAMPLE_INTERVAL_S


@dataclass
class Sample:
    ts: float
    cpu_pct: float
    cpu_per_core: list = field(default_factory=list)
    ram_pct: float = 0.0
    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    swap_pct: float = 0.0
    disk_pct: float = 0.0
    disk_free_gb: float = 0.0
    net_sent_kbps: float = 0.0
    net_recv_kbps: float = 0.0


def _stats(values: list) -> dict:
    if not values:
        return {"avg": 0.0, "min": 0.0, "max": 0.0, "p50": 0.0, "p95": 0.0}
    s = sorted(values)
    n = len(s)
    return {
        "avg": round(sum(s) / n, 2),
        "min": round(s[0], 2),
        "max": round(s[-1], 2),
        "p50": round(s[n // 2], 2),
        "p95": round(s[min(n - 1, int(n * 0.95))], 2),
    }


class StatsCollector:
    def __init__(self, disk_path: str = "C:\\"):
        self.samples: deque[Sample] = deque(maxlen=MAX_SAMPLES)
        self.disk_path = disk_path
        self._stop = asyncio.Event()
        self._running = False

    async def start(self, runner) -> None:
        if self._running:
            return
        self._running = True
        runner.spawn(self._loop())

    async def stop(self) -> None:
        self._stop.set()

    async def _loop(self) -> None:
        log.info("stats collector started (interval=%ds, window=%ds)",
                 SAMPLE_INTERVAL_S, WINDOW_S)
        try:
            prev_net = psutil.net_io_counters()
        except Exception:
            prev_net = None
        prev_t = time.time()
        # Prime cpu_percent so the first reading is meaningful.
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass

        while not self._stop.is_set():
            try:
                now = time.time()
                dt = max(0.001, now - prev_t)
                vmem = psutil.virtual_memory()
                swap = psutil.swap_memory()
                disk = psutil.disk_usage(self.disk_path)
                try:
                    net = psutil.net_io_counters()
                except Exception:
                    net = None

                if prev_net is not None and net is not None:
                    sent_kbps = max(0, (net.bytes_sent - prev_net.bytes_sent)) / dt / 1024
                    recv_kbps = max(0, (net.bytes_recv - prev_net.bytes_recv)) / dt / 1024
                else:
                    sent_kbps = 0.0
                    recv_kbps = 0.0

                sample = Sample(
                    ts=now,
                    cpu_pct=psutil.cpu_percent(interval=None),
                    cpu_per_core=psutil.cpu_percent(interval=None, percpu=True),
                    ram_pct=vmem.percent,
                    ram_used_gb=round(vmem.used / 1024**3, 2),
                    ram_total_gb=round(vmem.total / 1024**3, 2),
                    swap_pct=swap.percent,
                    disk_pct=disk.percent,
                    disk_free_gb=round(disk.free / 1024**3, 2),
                    net_sent_kbps=round(sent_kbps, 2),
                    net_recv_kbps=round(recv_kbps, 2),
                )
                self.samples.append(sample)
                prev_net = net
                prev_t = now
            except Exception:
                log.exception("sample failed")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=SAMPLE_INTERVAL_S)
            except asyncio.TimeoutError:
                pass

        log.info("stats collector stopped")

    def latest(self) -> Optional[dict]:
        if not self.samples:
            return None
        s = self.samples[-1]
        return {
            "ts": s.ts,
            "cpu_pct": s.cpu_pct,
            "cpu_per_core": list(s.cpu_per_core),
            "ram_pct": s.ram_pct,
            "ram_used_gb": s.ram_used_gb,
            "ram_total_gb": s.ram_total_gb,
            "swap_pct": s.swap_pct,
            "disk_pct": s.disk_pct,
            "disk_free_gb": s.disk_free_gb,
            "net_sent_kbps": s.net_sent_kbps,
            "net_recv_kbps": s.net_recv_kbps,
        }

    def summarize(self, window_s: int) -> dict:
        if not self.samples:
            return {"window_s": window_s, "samples": 0}
        cutoff = time.time() - window_s
        rel = [s for s in self.samples if s.ts >= cutoff]
        if not rel:
            return {"window_s": window_s, "samples": 0}
        return {
            "window_s": window_s,
            "samples": len(rel),
            "first_ts": rel[0].ts,
            "last_ts": rel[-1].ts,
            "cpu_pct": _stats([s.cpu_pct for s in rel]),
            "ram_pct": _stats([s.ram_pct for s in rel]),
            "ram_used_gb": _stats([s.ram_used_gb for s in rel]),
            "swap_pct": _stats([s.swap_pct for s in rel]),
            "disk_pct": _stats([s.disk_pct for s in rel]),
            "disk_free_gb": _stats([s.disk_free_gb for s in rel]),
            "net_sent_kbps": _stats([s.net_sent_kbps for s in rel]),
            "net_recv_kbps": _stats([s.net_recv_kbps for s in rel]),
        }

    def top_processes(self, n: int = 10) -> dict:
        """Snapshot of top processes by CPU and RAM right now."""
        rows = []
        try:
            # Prime cpu_percent for accurate readings on next sample.
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    p.cpu_percent(interval=None)
                except Exception:
                    pass
            time.sleep(0.2)
            for p in psutil.process_iter(["pid", "name", "memory_info"]):
                try:
                    rows.append({
                        "pid": p.info["pid"],
                        "name": p.info["name"] or "",
                        "cpu_pct": round(p.cpu_percent(interval=None), 1),
                        "ram_mb": round((p.info["memory_info"].rss if p.info["memory_info"] else 0) / 1024**2, 1),
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            log.exception("top_processes failed")
            return {"by_cpu": [], "by_ram": []}
        by_cpu = sorted(rows, key=lambda r: r["cpu_pct"], reverse=True)[:n]
        by_ram = sorted(rows, key=lambda r: r["ram_mb"], reverse=True)[:n]
        return {"by_cpu": by_cpu, "by_ram": by_ram}
