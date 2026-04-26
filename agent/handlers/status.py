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

    # Repos: merge dynamic registry with static config.
    from agent import repo_store
    dyn_repo_names = {r.name for r in repo_store.list_entries()}
    static_repo_names = set(cfg.repos.keys())
    repos_summary = {
        "registered": sorted(dyn_repo_names | static_repo_names),
        "dynamic": sorted(dyn_repo_names),
        "static": sorted(static_repo_names - dyn_repo_names),
    }

    # Apps: ask the supervisor for live state.
    apps_summary: list[dict] = []
    try:
        for a in ctx.runner.app_manager.list_apps():
            apps_summary.append({
                "name": a["name"],
                "alive": a["alive"],
                "desired": a["desired"],
                "pid": a["pid"],
                "uptime_s": a["uptime_s"],
                "last_health": a["last_health"],
                "last_exit_code": a["last_exit_code"],
                "restarts_recent": a["restart_count_recent"],
            })
    except Exception:
        pass

    # Telegram bot status.
    tg = {"running": False, "allowlisted": 0}
    bot = getattr(ctx.runner, "telegram", None)
    if bot is not None:
        tg = {
            "running": bot.application is not None,
            "allowlisted": len(bot.allowlist),
        }

    data = {
        "host": cfg.host_id,
        "version": __version__,
        "uptime_s": round(time.time() - _started, 1),
        "machine": {
            "cpu_pct": psutil.cpu_percent(interval=None),
            "ram_pct": mem.percent,
            "ram_used_gb": round(mem.used / 1024**3, 1),
            "ram_total_gb": round(mem.total / 1024**3, 1),
            "disk_pct": disk.percent,
            "disk_free_gb": round(disk.free / 1024**3, 1),
        },
        "ollama_up": await _ollama_up(cfg.ollama_url),
        "repos": repos_summary,
        "apps": apps_summary,
        "deploys": list(cfg.deploys.keys()),
        "services": cfg.allowed_services,
        "telegram": tg,
    }
    return Reply(id=cmd.id, ok=True, data=data)


async def handle_list_repos(ctx, cmd: Command) -> Reply:
    from agent import repo_store

    rows: dict[str, dict] = {}
    # Dynamic registry first.
    for e in repo_store.list_entries():
        rows[e.name] = {
            "name": e.name,
            "url": e.url,
            "branch": e.branch,
            "has_token": bool(e.token),
            "source": "dynamic",
            "path": str(ctx.cfg.workspace_dir / e.name),
            "added_at": e.added_at,
            "updated_at": e.updated_at,
        }
    # Static entries fill in only if not overridden by dynamic.
    for r in ctx.cfg.repos.values():
        if r.name in rows:
            continue
        rows[r.name] = {
            "name": r.name,
            "url": r.remote,
            "branch": r.branch,
            "has_token": False,
            "source": "static",
            "path": str(ctx.cfg.resolve_path(r.path)),
        }
    return Reply(id=cmd.id, ok=True, data=list(rows.values()))


async def handle_list_deploys(ctx, cmd: Command) -> Reply:
    data = [
        {"name": d.name, "cwd": str(ctx.cfg.resolve_path(d.cwd)), "cmd": d.cmd, "timeout_s": d.timeout_s}
        for d in ctx.cfg.deploys.values()
    ]
    return Reply(id=cmd.id, ok=True, data=data)


async def handle_list_services(ctx, cmd: Command) -> Reply:
    return Reply(id=cmd.id, ok=True, data=ctx.cfg.allowed_services)
