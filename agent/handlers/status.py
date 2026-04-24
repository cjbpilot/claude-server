"""Read-only status / listing handlers."""

from __future__ import annotations

import time

import httpx
import psutil

from agent import __version__
from shared.protocol import Command, Reply

_started = time.time()


async def _ollama_up(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=1.5) as c:
            r = await c.get(f"{url}/api/tags")
            return r.status_code == 200
    except Exception:
        return False


async def handle_status(ctx, cmd: Command) -> Reply:
    cfg = ctx.cfg
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(str(cfg.workspace_dir.anchor or "C:\\"))
    data = {
        "host": cfg.host_id,
        "version": __version__,
        "uptime_s": round(time.time() - _started, 1),
        "cpu_pct": psutil.cpu_percent(interval=None),
        "ram_pct": mem.percent,
        "disk_pct": disk.percent,
        "ollama_up": await _ollama_up(cfg.ollama_url),
        "repos": list(cfg.repos.keys()),
        "deploys": list(cfg.deploys.keys()),
        "services": cfg.allowed_services,
    }
    return Reply(id=cmd.id, ok=True, data=data)


async def handle_list_repos(ctx, cmd: Command) -> Reply:
    data = [
        {"name": r.name, "path": str(ctx.cfg.resolve_path(r.path)), "remote": r.remote, "branch": r.branch}
        for r in ctx.cfg.repos.values()
    ]
    return Reply(id=cmd.id, ok=True, data=data)


async def handle_list_deploys(ctx, cmd: Command) -> Reply:
    data = [
        {"name": d.name, "cwd": str(ctx.cfg.resolve_path(d.cwd)), "cmd": d.cmd, "timeout_s": d.timeout_s}
        for d in ctx.cfg.deploys.values()
    ]
    return Reply(id=cmd.id, ok=True, data=data)


async def handle_list_services(ctx, cmd: Command) -> Reply:
    return Reply(id=cmd.id, ok=True, data=ctx.cfg.allowed_services)
