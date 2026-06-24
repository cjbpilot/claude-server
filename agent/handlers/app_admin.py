"""App management handlers: register/unregister/start/stop/restart/list/logs."""

from __future__ import annotations

from typing import Optional

from agent.app_manager import MaintenanceModeError
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
    try:
        ok = await _mgr(hctx).start_app(name)
    except MaintenanceModeError as e:
        return Reply(id=cmd.id, ok=False, error=str(e))
    if not ok:
        return Reply(id=cmd.id, ok=False, error=f"unknown app: {name}")
    return Reply(id=cmd.id, ok=True, data={"name": name})


async def handle_stop_app(hctx, cmd: Command) -> Reply:
    name = (cmd.args.get("name") or "").strip()
    if not name:
        return Reply(id=cmd.id, ok=False, error="missing 'name'")
    try:
        ok = await _mgr(hctx).stop_app(name)
    except MaintenanceModeError as e:
        return Reply(id=cmd.id, ok=False, error=str(e))
    if not ok:
        return Reply(id=cmd.id, ok=False, error=f"unknown app: {name}")
    return Reply(id=cmd.id, ok=True, data={"name": name})


async def handle_restart_app(hctx, cmd: Command) -> Reply:
    name = (cmd.args.get("name") or "").strip()
    if not name:
        return Reply(id=cmd.id, ok=False, error="missing 'name'")
    try:
        ok = await _mgr(hctx).restart_app(name)
    except MaintenanceModeError as e:
        return Reply(id=cmd.id, ok=False, error=str(e))
    if not ok:
        return Reply(id=cmd.id, ok=False, error=f"unknown app: {name}")
    return Reply(id=cmd.id, ok=True, data={"name": name})


_AUTO_UPDATE_SETTABLE = {
    "enabled": bool,
    "at_utc": str,
    "window_minutes": int,
    "skip_if_maintenance": bool,
    "rollback_on_health_fail": bool,
    "notify_telegram": bool,
}


def _coerce_auto_update_partial(raw: dict) -> tuple[dict, Optional[str]]:
    """Pull operator-settable keys out of `raw`, type-check them, and return
    a clean dict to merge. Returns (partial, error_or_None)."""
    out: dict = {}
    for key, want_type in _AUTO_UPDATE_SETTABLE.items():
        if key not in raw:
            continue
        v = raw[key]
        if want_type is bool:
            if isinstance(v, bool):
                out[key] = v
            elif isinstance(v, str):
                lv = v.strip().lower()
                if lv in ("true", "1", "yes", "on"):
                    out[key] = True
                elif lv in ("false", "0", "no", "off"):
                    out[key] = False
                else:
                    return {}, f"{key}: expected bool, got {v!r}"
            else:
                return {}, f"{key}: expected bool, got {v!r}"
        elif want_type is int:
            try:
                out[key] = int(v)
            except (TypeError, ValueError):
                return {}, f"{key}: expected int, got {v!r}"
            if out[key] < 1 or out[key] > 24 * 60:
                return {}, f"{key}: window_minutes must be between 1 and 1440"
        elif want_type is str:
            if not isinstance(v, str):
                return {}, f"{key}: expected str, got {v!r}"
            if key == "at_utc":
                try:
                    h_str, m_str = v.split(":")
                    h, m = int(h_str), int(m_str)
                    if not (0 <= h < 24 and 0 <= m < 60):
                        raise ValueError
                except (ValueError, AttributeError):
                    return {}, f"at_utc: expected 'HH:MM' in 24-hour UTC, got {v!r}"
                out[key] = f"{h:02d}:{m:02d}"
            else:
                out[key] = v
    return out, None


async def handle_set_auto_update(hctx, cmd: Command) -> Reply:
    name = (cmd.args.get("name") or "").strip()
    if not name:
        return Reply(id=cmd.id, ok=False, error="missing 'name'")
    partial = {k: v for k, v in cmd.args.items() if k != "name"}
    if not partial:
        return Reply(
            id=cmd.id, ok=False,
            error="no auto-update fields supplied; pass at least one of "
                  "enabled, at_utc, window_minutes, skip_if_maintenance, "
                  "rollback_on_health_fail, notify_telegram",
        )
    clean, err = _coerce_auto_update_partial(partial)
    if err is not None:
        return Reply(id=cmd.id, ok=False, error=err)
    if not clean:
        return Reply(
            id=cmd.id, ok=False,
            error="no recognised auto-update fields in the request",
        )
    rec = await _mgr(hctx).set_auto_update(name, clean)
    if rec is None:
        return Reply(id=cmd.id, ok=False, error=f"unknown app: {name}")
    return Reply(id=cmd.id, ok=True, data={
        "name": rec.name,
        "auto_update": rec.auto_update,
    })


async def handle_auto_update_now(hctx, cmd: Command) -> Reply:
    name = (cmd.args.get("name") or "").strip()
    if not name:
        return Reply(id=cmd.id, ok=False, error="missing 'name'")
    try:
        result = await _mgr(hctx).auto_update_now(name)
    except Exception as e:
        return Reply(id=cmd.id, ok=False, error=repr(e))
    if not result.get("ok"):
        return Reply(id=cmd.id, ok=False, error=result.get("error") or "failed")
    return Reply(id=cmd.id, ok=True, data=result)


_PRODUCT_OWNER_SETTABLE = {
    "enabled": bool,
    "day_of_week": int,
    "at_utc": str,
    "feedback_window_hours": int,
    "max_specs": int,
    "skip_if_maintenance": bool,
    "notify_telegram": bool,
}


def _coerce_product_owner_partial(raw: dict) -> tuple[dict, Optional[str]]:
    out: dict = {}
    for key, want_type in _PRODUCT_OWNER_SETTABLE.items():
        if key not in raw:
            continue
        v = raw[key]
        if want_type is bool:
            if isinstance(v, bool):
                out[key] = v
            elif isinstance(v, str):
                lv = v.strip().lower()
                if lv in ("true", "1", "yes", "on"):
                    out[key] = True
                elif lv in ("false", "0", "no", "off"):
                    out[key] = False
                else:
                    return {}, f"{key}: expected bool, got {v!r}"
            else:
                return {}, f"{key}: expected bool, got {v!r}"
        elif want_type is int:
            try:
                out[key] = int(v)
            except (TypeError, ValueError):
                return {}, f"{key}: expected int, got {v!r}"
            if key == "day_of_week":
                if not (-1 <= out[key] <= 6):
                    return {}, "day_of_week must be -1 (daily) or 0..6 (Mon..Sun)"
            elif key == "feedback_window_hours":
                if not (0 <= out[key] <= 168):
                    return {}, "feedback_window_hours must be 0..168"
            elif key == "max_specs":
                if not (1 <= out[key] <= 10):
                    return {}, "max_specs must be 1..10"
        elif want_type is str:
            if not isinstance(v, str):
                return {}, f"{key}: expected str, got {v!r}"
            if key == "at_utc":
                try:
                    h_str, m_str = v.split(":")
                    h, m = int(h_str), int(m_str)
                    if not (0 <= h < 24 and 0 <= m < 60):
                        raise ValueError
                except (ValueError, AttributeError):
                    return {}, f"at_utc: expected 'HH:MM' UTC, got {v!r}"
                out[key] = f"{h:02d}:{m:02d}"
            else:
                out[key] = v
    return out, None


async def handle_set_product_owner(hctx, cmd: Command) -> Reply:
    name = (cmd.args.get("name") or "").strip()
    if not name:
        return Reply(id=cmd.id, ok=False, error="missing 'name'")
    partial = {k: v for k, v in cmd.args.items() if k != "name"}
    if not partial:
        return Reply(
            id=cmd.id, ok=False,
            error="no product-owner fields supplied; pass at least one of "
                  "enabled, day_of_week, at_utc, feedback_window_hours, "
                  "max_specs, skip_if_maintenance, notify_telegram",
        )
    clean, err = _coerce_product_owner_partial(partial)
    if err is not None:
        return Reply(id=cmd.id, ok=False, error=err)
    if not clean:
        return Reply(
            id=cmd.id, ok=False,
            error="no recognised product-owner fields in the request",
        )
    rec = await _mgr(hctx).set_product_owner(name, clean)
    if rec is None:
        return Reply(id=cmd.id, ok=False, error=f"unknown app: {name}")
    return Reply(id=cmd.id, ok=True, data={
        "name": rec.name,
        "product_owner": rec.product_owner,
    })


async def handle_product_owner_now(hctx, cmd: Command) -> Reply:
    name = (cmd.args.get("name") or "").strip()
    if not name:
        return Reply(id=cmd.id, ok=False, error="missing 'name'")
    try:
        result = await _mgr(hctx).product_owner_now(name)
    except Exception as e:
        return Reply(id=cmd.id, ok=False, error=repr(e))
    if not result.get("ok"):
        return Reply(id=cmd.id, ok=False, error=result.get("error") or "failed")
    return Reply(id=cmd.id, ok=True, data=result)


async def handle_set_app_mode(hctx, cmd: Command) -> Reply:
    name = (cmd.args.get("name") or "").strip()
    mode = (cmd.args.get("mode") or "").strip()
    if not name:
        return Reply(id=cmd.id, ok=False, error="missing 'name'")
    if not mode:
        return Reply(id=cmd.id, ok=False, error="missing 'mode' (active|maintenance)")
    try:
        rec = await _mgr(hctx).set_mode(name, mode)
    except ValueError as e:
        return Reply(id=cmd.id, ok=False, error=str(e))
    if rec is None:
        return Reply(id=cmd.id, ok=False, error=f"unknown app: {name}")
    return Reply(id=cmd.id, ok=True, data={
        "name": rec.name, "mode": rec.mode, "desired": rec.desired_state,
    })


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
