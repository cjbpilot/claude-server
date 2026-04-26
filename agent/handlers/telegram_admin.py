"""Admin tools for the embedded Telegram bot: allow, revoke, reload."""

from __future__ import annotations

from agent import telegram_bot
from shared.protocol import Command, Reply


async def handle_telegram_allow(hctx, cmd: Command) -> Reply:
    raw = cmd.args.get("user_id")
    if raw is None:
        return Reply(id=cmd.id, ok=False, error="missing 'user_id'")
    try:
        uid = int(raw)
    except (ValueError, TypeError):
        return Reply(id=cmd.id, ok=False, error="user_id must be an integer")
    ids = telegram_bot.add_allowed(uid)
    bot = getattr(hctx.runner, "telegram", None)
    if bot is not None:
        bot.allowlist = set(ids)
    return Reply(id=cmd.id, ok=True, data={"user_id": uid, "allowlist": sorted(ids)})


async def handle_telegram_revoke(hctx, cmd: Command) -> Reply:
    raw = cmd.args.get("user_id")
    if raw is None:
        return Reply(id=cmd.id, ok=False, error="missing 'user_id'")
    try:
        uid = int(raw)
    except (ValueError, TypeError):
        return Reply(id=cmd.id, ok=False, error="user_id must be an integer")
    ids = telegram_bot.remove_allowed(uid)
    bot = getattr(hctx.runner, "telegram", None)
    if bot is not None:
        bot.allowlist = set(ids)
    return Reply(id=cmd.id, ok=True, data={"user_id": uid, "allowlist": sorted(ids)})


async def handle_telegram_reload(hctx, cmd: Command) -> Reply:
    bot = getattr(hctx.runner, "telegram", None)
    if bot is None:
        return Reply(id=cmd.id, ok=False, error="telegram bot not initialised")
    try:
        result = await bot.reload()
    except Exception as e:
        return Reply(id=cmd.id, ok=False, error=repr(e))
    return Reply(id=cmd.id, ok=True, data={"state": result, "allowlist": sorted(bot.allowlist)})


async def handle_telegram_status(hctx, cmd: Command) -> Reply:
    bot = getattr(hctx.runner, "telegram", None)
    if bot is None:
        return Reply(id=cmd.id, ok=True, data={"running": False, "reason": "not initialised"})
    return Reply(id=cmd.id, ok=True, data={
        "running": bot.application is not None,
        "token_present": telegram_bot.load_token() is not None,
        "allowlist": sorted(bot.allowlist),
    })
