"""App management handlers: register/unregister/start/stop/restart/list/logs."""

from __future__ import annotations

from shared.protocol import Command, Reply


def _mgr(hctx):
    return hctx.runner.app_manager


async def handle_register_app(hctx, cmd: Command) -> Reply:
    repo = (cmd.args.get("repo") or "").strip()
    name = (cmd.args.get("name") or "").strip() or None
    if not repo:
        return Reply(id=cmd.id, ok=False, error="missing 'repo'")
    try:
        result = await _mgr(hctx).register(repo_name=repo, name=name)
    except ValueError as e:
        return Reply(id=cmd.id, ok=False, error=str(e))
    except Exception as e:
        return Reply(id=cmd.id, ok=False, error=repr(e))
    return Reply(id=cmd.id, ok=True, data=result)


async def handle_unregister_app(hctx, cmd: Command) -> Reply:
    name = (cmd.args.get("name") or "").strip()
    if not name:
        return Reply(id=cmd.id, ok=False, error="missing 'name'")
    removed = await _mgr(hctx).unregister(name)
    return Reply(id=cmd.id, ok=True, data={"name": name, "removed": removed})


async def handle_start_app(hctx, cmd: Command) -> Reply:
    name = (cmd.args.get("name") or "").strip()
    if not name:
        return Reply(id=cmd.id, ok=False, error="missing 'name'")
    ok = await _mgr(hctx).start_app(name)
    if not ok:
        return Reply(id=cmd.id, ok=False, error=f"unknown app: {name}")
    return Reply(id=cmd.id, ok=True, data={"name": name})


async def handle_stop_app(hctx, cmd: Command) -> Reply:
    name = (cmd.args.get("name") or "").strip()
    if not name:
        return Reply(id=cmd.id, ok=False, error="missing 'name'")
    ok = await _mgr(hctx).stop_app(name)
    if not ok:
        return Reply(id=cmd.id, ok=False, error=f"unknown app: {name}")
    return Reply(id=cmd.id, ok=True, data={"name": name})


async def handle_restart_app(hctx, cmd: Command) -> Reply:
    name = (cmd.args.get("name") or "").strip()
    if not name:
        return Reply(id=cmd.id, ok=False, error="missing 'name'")
    ok = await _mgr(hctx).restart_app(name)
    if not ok:
        return Reply(id=cmd.id, ok=False, error=f"unknown app: {name}")
    return Reply(id=cmd.id, ok=True, data={"name": name})


async def handle_list_apps(hctx, cmd: Command) -> Reply:
    return Reply(id=cmd.id, ok=True, data=_mgr(hctx).list_apps())


async def handle_app_logs(hctx, cmd: Command) -> Reply:
    name = (cmd.args.get("name") or "").strip()
    lines = int(cmd.args.get("lines") or 100)
    lines = max(1, min(lines, 2000))
    if not name:
        return Reply(id=cmd.id, ok=False, error="missing 'name'")
    rows = _mgr(hctx).tail_log(name, lines=lines)
    return Reply(id=cmd.id, ok=True, data={"name": name, "lines": rows})
