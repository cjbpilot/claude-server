"""Embedded Telegram bot exposing agent tools over chat.

Bootstrapping:

  1. Land the BotFather token at:
       C:\\ProgramData\\ClaudeAgent\\secrets\\telegram.token
     (use the write_file tool with secret=true to do this from Claude.)
  2. Restart the agent (or call telegram_reload) — bot starts polling.
  3. From your phone, DM the bot and run /myid. Bot replies with your
     Telegram user id even if you're not yet authorised.
  4. From Claude, call telegram_allow(user_id=<that-id>) to add yourself
     to the allowlist. From now on you can use any command.

Allowlist file (one numeric Telegram user id per line, # comments allowed):
  C:\\ProgramData\\ClaudeAgent\\secrets\\telegram_allow.txt
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from shared.protocol import Command

# NOTE: REGISTRY and HandlerCtx are imported lazily inside _call() to avoid
# a circular import (handlers/__init__.py imports telegram_admin which imports
# this module).

log = logging.getLogger("agent.telegram")

TOKEN_PATH = Path(r"C:\ProgramData\ClaudeAgent\secrets\telegram.token")
ALLOWLIST_PATH = Path(r"C:\ProgramData\ClaudeAgent\secrets\telegram_allow.txt")
TG_MAX = 4000  # safe cap below 4096


def load_token() -> Optional[str]:
    if not TOKEN_PATH.exists():
        return None
    try:
        v = TOKEN_PATH.read_text(encoding="utf-8").strip()
        return v or None
    except Exception:
        return None


def load_allowlist() -> set:
    if not ALLOWLIST_PATH.exists():
        return set()
    out = set()
    try:
        for line in ALLOWLIST_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                out.add(int(line))
            except ValueError:
                continue
    except Exception:
        log.exception("could not read %s", ALLOWLIST_PATH)
    return out


def save_allowlist(ids: set) -> None:
    ALLOWLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    body = "# Allowlisted Telegram user ids\n" + "\n".join(str(i) for i in sorted(ids)) + "\n"
    ALLOWLIST_PATH.write_text(body, encoding="utf-8")


def add_allowed(user_id: int) -> set:
    ids = load_allowlist()
    ids.add(int(user_id))
    save_allowlist(ids)
    return ids


def remove_allowed(user_id: int) -> set:
    ids = load_allowlist()
    ids.discard(int(user_id))
    save_allowlist(ids)
    return ids


HELP_TEXT = """Claude-Agent Telegram Bot

Bootstrap: send /myid to see your Telegram id, then ask Claude to call
telegram_allow user_id=<id> to authorise you.

Read-only:
  /status         agent + machine snapshot
  /machine        utilisation (5m window) + top processes
  /apps           list managed apps
  /app_logs <name> [lines]   tail per-app log
  /repos          list registered repos
  /services       list allowlisted Windows services
  /ollama         installed Ollama models
  /ollama_ps      currently loaded models
  /myid           show your Telegram user id
  /help           this message

App control:
  /app_start <name>
  /app_stop <name>
  /app_restart <name>

Repo control:
  /pull <repo>    git_pull

Service control:
  /service_restart <name>

Host control:
  /host_restart [delay_s]    schedules reboot; use /host_cancel to abort
  /host_cancel
  /self_update               pull agent code and restart

Anything else: ask Claude over MCP.
"""


class TelegramBot:
    def __init__(self, cfg, runner):
        self.cfg = cfg
        self.runner = runner
        self.application: Optional[Application] = None
        self.allowlist: set = set()
        self._stop_evt = asyncio.Event()
        self._watchdog_started = False

    def is_running(self) -> bool:
        app = self.application
        if app is None:
            return False
        u = getattr(app, "updater", None)
        return u is not None and getattr(u, "running", False)

    async def start(self) -> None:
        """Initial start: try once, kick off the watchdog regardless. The
        watchdog will keep retrying if this initial attempt fails so a flaky
        boot (transient Telegram API timeout) doesn't leave the bot dead."""
        if not load_token():
            log.info("no Telegram token at %s - bot disabled", TOKEN_PATH)
            return
        await self._try_start()
        if not self._watchdog_started:
            self._watchdog_started = True
            self.runner.spawn(self._watchdog_loop())

    async def _try_start(self) -> bool:
        token = load_token()
        if not token:
            return False
        self.allowlist = load_allowlist()
        try:
            app = Application.builder().token(token).build()
            self.application = app
            self._register_handlers()
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            log.info("Telegram bot started; %d allowlisted user(s)", len(self.allowlist))
            return True
        except Exception:
            log.exception("Telegram bot start failed (watchdog will retry)")
            try:
                if self.application is not None:
                    await self.application.shutdown()
            except Exception:
                pass
            self.application = None
            return False

    async def _watchdog_loop(self) -> None:
        """Periodically verify the bot is polling. If it's not (failed initial
        start, transient network error killed the updater, etc), try again."""
        while not self._stop_evt.is_set():
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=30)
                break  # stop requested
            except asyncio.TimeoutError:
                pass
            if not load_token():
                continue  # token removed, do nothing
            if not self.is_running():
                log.warning("Telegram bot not running, attempting restart")
                try:
                    await self.stop()
                except Exception:
                    pass
                await self._try_start()

    async def stop(self) -> None:
        app = self.application
        if app is None:
            return
        try:
            u = getattr(app, "updater", None)
            if u is not None and getattr(u, "running", False):
                await u.stop()
            await app.stop()
            await app.shutdown()
        except Exception:
            log.exception("Telegram bot shutdown error")
        finally:
            self.application = None

    async def reload(self) -> str:
        """Re-read token + allowlist and (re)start the bot."""
        await self.stop()
        await self._try_start()
        if not self._watchdog_started:
            self._watchdog_started = True
            self.runner.spawn(self._watchdog_loop())
        return "ok" if self.is_running() else ("no token" if not load_token() else "start_failed")

    # ---------------- handler plumbing ----------------

    def _register_handlers(self) -> None:
        app = self.application
        assert app is not None
        cmds = [
            ("help", self.cmd_help),
            ("start", self.cmd_help),  # Telegram convention
            ("myid", self.cmd_myid),
            ("status", self.cmd_status),
            ("machine", self.cmd_machine),
            ("apps", self.cmd_apps),
            ("repos", self.cmd_repos),
            ("services", self.cmd_services),
            ("ollama", self.cmd_ollama_list),
            ("ollama_ps", self.cmd_ollama_ps),
            ("app_start", self.cmd_app_start),
            ("app_stop", self.cmd_app_stop),
            ("app_restart", self.cmd_app_restart),
            ("app_logs", self.cmd_app_logs),
            ("pull", self.cmd_pull),
            ("service_restart", self.cmd_service_restart),
            ("host_restart", self.cmd_host_restart),
            ("host_cancel", self.cmd_host_cancel),
            ("self_update", self.cmd_self_update),
        ]
        for name, fn in cmds:
            app.add_handler(CommandHandler(name, fn))

    def _authorized(self, update: Update) -> bool:
        uid = update.effective_user.id if update.effective_user else None
        return uid is not None and uid in self.allowlist

    async def _reject(self, update: Update) -> None:
        await update.effective_message.reply_text(
            "Not authorised. Run /myid and have Claude allowlist that id."
        )

    async def _send(self, update: Update, text: str) -> None:
        for chunk in _chunked(text, TG_MAX):
            await update.effective_message.reply_text(chunk)

    async def _call(self, tool: str, args: Optional[dict] = None) -> str:
        from agent.handlers import REGISTRY, HandlerCtx  # lazy: avoid import cycle
        handler = REGISTRY.get(tool)
        if handler is None:
            return f"❌ unknown tool: {tool}"
        cmd = Command(tool=tool, args=args or {})
        hctx = HandlerCtx(self.cfg, self.runner.nc, self.runner)
        try:
            reply = await handler(hctx, cmd)
        except Exception as e:
            return f"❌ handler raised: {e!r}"
        if not reply.ok:
            return f"❌ {reply.error or 'failed'}"
        if reply.data is None:
            return "OK"
        return "OK\n" + json.dumps(reply.data, indent=2, default=str)

    # ---------------- commands ----------------

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(HELP_TEXT)

    async def cmd_myid(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        u = update.effective_user
        if not u:
            await update.effective_message.reply_text("no user info")
            return
        await update.effective_message.reply_text(
            f"Your Telegram id: {u.id}\n"
            f"Name: {u.full_name}\n"
            f"Allowlisted: {'yes' if u.id in self.allowlist else 'no'}"
        )

    async def _gated(self, update: Update, tool: str, args: Optional[dict] = None) -> None:
        if not self._authorized(update):
            await self._reject(update)
            return
        result = await self._call(tool, args)
        await self._send(update, result)

    async def cmd_status(self, u, c):           await self._gated(u, "status")
    async def cmd_machine(self, u, c):          await self._gated(u, "machine_stats", {"window_minutes": 5, "top_processes": 8})
    async def cmd_apps(self, u, c):             await self._gated(u, "list_apps")
    async def cmd_repos(self, u, c):            await self._gated(u, "list_repos")
    async def cmd_services(self, u, c):         await self._gated(u, "list_services")
    async def cmd_ollama_list(self, u, c):      await self._gated(u, "ollama_list")
    async def cmd_ollama_ps(self, u, c):        await self._gated(u, "ollama_ps")
    async def cmd_self_update(self, u, c):      await self._gated(u, "self_update")
    async def cmd_host_cancel(self, u, c):      await self._gated(u, "host_cancel_restart")

    async def cmd_app_start(self, u, c):        await self._with_arg(u, c, "start_app", "name")
    async def cmd_app_stop(self, u, c):         await self._with_arg(u, c, "stop_app", "name")
    async def cmd_app_restart(self, u, c):      await self._with_arg(u, c, "restart_app", "name")
    async def cmd_pull(self, u, c):             await self._with_arg(u, c, "git_pull", "repo")
    async def cmd_service_restart(self, u, c):  await self._with_arg(u, c, "service_restart", "name")

    async def cmd_app_logs(self, u, c):
        if not self._authorized(u):
            await self._reject(u)
            return
        args = c.args or []
        if not args:
            await u.effective_message.reply_text("usage: /app_logs <name> [lines]")
            return
        name = args[0]
        lines = 50
        if len(args) > 1:
            try:
                lines = max(1, min(200, int(args[1])))
            except ValueError:
                pass
        result = await self._call("app_logs", {"name": name, "lines": lines})
        await self._send(u, result)

    async def cmd_host_restart(self, u, c):
        if not self._authorized(u):
            await self._reject(u)
            return
        delay = 30
        if c.args:
            try:
                delay = max(0, int(c.args[0]))
            except ValueError:
                pass
        await self._send(u, await self._call("host_restart", {"delay_s": delay}))

    async def _with_arg(self, u, c, tool, arg_name):
        if not self._authorized(u):
            await self._reject(u)
            return
        if not c.args:
            await u.effective_message.reply_text(f"usage: /{tool} <{arg_name}>")
            return
        await self._send(u, await self._call(tool, {arg_name: c.args[0]}))


def _chunked(text: str, n: int):
    if len(text) <= n:
        yield text
        return
    pos = 0
    while pos < len(text):
        yield text[pos:pos + n]
        pos += n
