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

    async def _call(self, tool: str, args: Optional[dict] = None,
                    update: Optional[Update] = None) -> str:
        """Run a command and return the immediate Telegram response.

        If the handler kicks off a background job (returns reply.job_id),
        we additionally spawn a watcher that posts a follow-up message
        to the same chat when the job completes - so the user finds out
        when 'pull stronghold' actually finishes installing and cycling.
        """
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

        # Job-based: ack immediately, post follow-up when done
        if reply.job_id and update is not None:
            chat = update.effective_chat
            msg = update.effective_message
            if chat is not None:
                self.runner.spawn(self._follow_job(
                    chat_id=chat.id,
                    reply_to=msg.message_id if msg else None,
                    tool=tool,
                    job_id=reply.job_id,
                    initial_data=reply.data or {},
                ))
            return _fmt_job_started(tool, reply)

        if reply.data is None:
            return "OK"
        formatter = _FORMATTERS.get(tool)
        if formatter is not None:
            try:
                return formatter(reply.data)
            except Exception:
                log.exception("formatter for %s failed; falling back to JSON", tool)
        return "OK\n" + json.dumps(reply.data, indent=2, default=str)

    async def _follow_job(self, chat_id: int, reply_to: Optional[int],
                          tool: str, job_id: str, initial_data: dict) -> None:
        """Subscribe to job_done for `job_id` and post a follow-up message."""
        from shared import protocol as _proto, subjects as _subj
        nc = self.runner.nc
        if nc is None:
            return
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()

        async def on_done(msg):
            if fut.done():
                return
            try:
                fut.set_result(_proto.JobDone.from_json(msg.data))
            except Exception as e:
                fut.set_exception(e)

        try:
            sub = await nc.subscribe(
                _subj.job_done(self.cfg.host_id, job_id),
                cb=on_done,
            )
        except Exception:
            log.exception("could not subscribe to job_done for %s", job_id)
            return

        try:
            try:
                done = await asyncio.wait_for(fut, timeout=1800)  # 30 min cap
            except asyncio.TimeoutError:
                await self._send_chat(
                    chat_id,
                    f"⏱️ {tool} job {job_id[:8]} still running after 30 min — check /apps or /app_logs",
                    reply_to=reply_to,
                )
                return
            await self._send_chat(
                chat_id,
                _fmt_job_done(tool, done, initial_data),
                reply_to=reply_to,
            )
        finally:
            try:
                await sub.unsubscribe()
            except Exception:
                pass

    async def _send_chat(self, chat_id: int, text: str, reply_to: Optional[int] = None) -> None:
        if not self.is_running() or self.application is None:
            return
        try:
            for chunk in _chunked(text, TG_MAX):
                await self.application.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    reply_to_message_id=reply_to,
                )
                reply_to = None  # only attach the reply_to to the first chunk
        except Exception:
            log.exception("send_chat failed")

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
        result = await self._call(tool, args, update=update)
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
        result = await self._call("app_logs", {"name": name, "lines": lines}, update=u)
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
        await self._send(u, await self._call("host_restart", {"delay_s": delay}, update=u))

    async def _with_arg(self, u, c, tool, arg_name):
        if not self._authorized(u):
            await self._reject(u)
            return
        if not c.args:
            await u.effective_message.reply_text(f"usage: /{tool} <{arg_name}>")
            return
        await self._send(u, await self._call(tool, {arg_name: c.args[0]}, update=u))


def _chunked(text: str, n: int):
    if len(text) <= n:
        yield text
        return
    pos = 0
    while pos < len(text):
        yield text[pos:pos + n]
        pos += n


# ---------------- Pretty formatters for /command output ----------------


def _fmt_duration(secs) -> str:
    try:
        s = int(secs)
    except (TypeError, ValueError):
        return "?"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def _fmt_status(d: dict) -> str:
    L = []
    L.append(f"Agent v{d.get('version', '?')} on {d.get('host', '?')}")
    L.append(f"Uptime: {_fmt_duration(d.get('uptime_s', 0))}")
    m = d.get("machine") or {}
    L.append(
        f"CPU {m.get('cpu_pct', 0):.1f}%  "
        f"RAM {m.get('ram_pct', 0):.1f}% "
        f"({m.get('ram_used_gb', 0)}/{m.get('ram_total_gb', 0)} GB)  "
        f"Disk {m.get('disk_pct', 0):.1f}% ({m.get('disk_free_gb', 0)} GB free)"
    )
    L.append(f"Ollama: {'up' if d.get('ollama_up') else 'DOWN'}")
    tg = d.get("telegram") or {}
    L.append(f"Telegram: {'running' if tg.get('running') else 'down'}, {tg.get('allowlisted', 0)} user(s)")

    apps = d.get("apps") or []
    if apps:
        L.append("")
        L.append("Apps:")
        for a in apps:
            state = "alive" if a.get("alive") else f"DOWN (rc={a.get('last_exit_code')})"
            extras = []
            if a.get("last_health"):
                extras.append(f"health={a['last_health']}")
            if a.get("uptime_s") is not None:
                extras.append(f"up {_fmt_duration(a['uptime_s'])}")
            if a.get("restarts_recent"):
                extras.append(f"restarts={a['restarts_recent']}")
            extra_str = " | ".join(extras)
            line = f"  {a.get('name')}: {state} (desired={a.get('desired')})"
            if extra_str:
                line += f"  [{extra_str}]"
            L.append(line)

    repos = d.get("repos") or {}
    if repos.get("dynamic"):
        L.append(f"\nRepos (dynamic): {', '.join(repos['dynamic'])}")
    if repos.get("static"):
        L.append(f"Repos (static):  {', '.join(repos['static'])}")
    deploys = d.get("deploys") or []
    if deploys:
        L.append(f"Deploys: {', '.join(deploys)}")
    services = d.get("services") or []
    if services:
        L.append(f"Services: {', '.join(services)}")
    return "\n".join(L)


def _fmt_apps(rows: list) -> str:
    if not rows:
        return "no apps registered"
    L = ["Managed apps:"]
    for a in rows:
        state = "alive" if a.get("alive") else f"DOWN (rc={a.get('last_exit_code')})"
        line = f"  {a.get('name')}: {state} (desired={a.get('desired')})"
        bits = []
        if a.get("pid"):
            bits.append(f"pid={a['pid']}")
        if a.get("uptime_s") is not None:
            bits.append(f"up {_fmt_duration(a['uptime_s'])}")
        if a.get("last_health"):
            bits.append(f"health={a['last_health']}")
        if a.get("restart_count_recent") or a.get("restarts_recent"):
            bits.append(f"restarts={a.get('restart_count_recent', a.get('restarts_recent'))}")
        if bits:
            line += f"  [{' | '.join(bits)}]"
        L.append(line)
    return "\n".join(L)


def _fmt_repos(rows: list) -> str:
    if not rows:
        return "no repos registered"
    L = ["Registered repos:"]
    for r in rows:
        flags = []
        if r.get("has_token"):
            flags.append("auth")
        if r.get("source"):
            flags.append(r["source"])
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        L.append(f"  {r.get('name')}: {r.get('url')}  ({r.get('branch', 'main')}){suffix}")
    return "\n".join(L)


def _fmt_services(rows) -> str:
    if not rows:
        return "no services allowlisted"
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        # If it ever changes shape, fall back gracefully.
        return "Services:\n" + "\n".join(f"  {r}" for r in rows)
    return "Services: " + ", ".join(rows)


def _fmt_deploys(rows: list) -> str:
    if not rows:
        return "no deploys configured"
    L = ["Deploys:"]
    for d in rows:
        L.append(f"  {d.get('name')}: {d.get('cmd')} (cwd={d.get('cwd')}, timeout={d.get('timeout_s')}s)")
    return "\n".join(L)


def _fmt_machine(d: dict) -> str:
    L = []
    cur = d.get("current") or {}
    if cur:
        L.append(
            f"Now: CPU {cur.get('cpu_pct', 0):.1f}%  "
            f"RAM {cur.get('ram_pct', 0):.1f}% ({cur.get('ram_used_gb', 0)}/{cur.get('ram_total_gb', 0)} GB)  "
            f"Disk {cur.get('disk_pct', 0):.1f}% ({cur.get('disk_free_gb', 0)} GB free)  "
            f"Net ↑{cur.get('net_sent_kbps', 0):.1f} ↓{cur.get('net_recv_kbps', 0):.1f} kbps"
        )
    w = d.get("window") or {}
    if w and w.get("samples"):
        mins = w.get("window_s", 0) // 60
        cpu = w.get("cpu_pct") or {}
        ram = w.get("ram_pct") or {}
        L.append(
            f"Last {mins}m ({w.get('samples')} samples):  "
            f"CPU avg {cpu.get('avg', 0):.1f}% / p95 {cpu.get('p95', 0):.1f}% / max {cpu.get('max', 0):.1f}%  "
            f"RAM avg {ram.get('avg', 0):.1f}% / max {ram.get('max', 0):.1f}%"
        )
    tp = d.get("top_processes") or {}
    by_cpu = tp.get("by_cpu") or []
    by_ram = tp.get("by_ram") or []
    if by_cpu:
        L.append("\nTop CPU:")
        for p in by_cpu[:6]:
            name = p.get("name") or "?"
            L.append(f"  {p.get('cpu_pct', 0):>5.1f}%  {name} (pid {p.get('pid')})")
    if by_ram:
        L.append("\nTop RAM:")
        for p in by_ram[:6]:
            name = p.get("name") or "?"
            L.append(f"  {p.get('ram_mb', 0):>6.0f} MB  {name} (pid {p.get('pid')})")
    return "\n".join(L) or "no samples yet"


def _fmt_ollama_list(d) -> str:
    models = (d or {}).get("models") if isinstance(d, dict) else None
    if not models:
        return "no models installed"
    L = ["Installed Ollama models:"]
    for m in models:
        size_gb = (m.get("size") or 0) / 1024**3
        L.append(f"  {m.get('name')}  ({size_gb:.1f} GB)")
    return "\n".join(L)


def _fmt_ollama_ps(d) -> str:
    models = (d or {}).get("models") if isinstance(d, dict) else None
    if not models:
        return "no models loaded"
    L = ["Loaded Ollama models:"]
    for m in models:
        size_gb = (m.get("size") or 0) / 1024**3
        L.append(f"  {m.get('name')}  ({size_gb:.1f} GB resident)")
    return "\n".join(L)


def _fmt_app_logs(d: dict) -> str:
    name = d.get("name", "?")
    rows = d.get("lines") or []
    if not rows:
        return f"app {name}: log empty"
    return f"--- {name} (last {len(rows)} lines) ---\n" + "\n".join(rows)


def _fmt_simple_named(d: dict) -> str:
    """For tools that return {'name': ...} or close variants."""
    if not isinstance(d, dict):
        return str(d)
    name = (d.get("name") or d.get("repo") or d.get("deploy")
            or d.get("service") or d.get("model") or "?")
    extras = {k: v for k, v in d.items()
              if k not in {"name", "repo", "deploy", "service", "model"}}
    if not extras:
        return f"OK: {name}"
    extra_lines = "\n".join(f"  {k}: {v}" for k, v in extras.items())
    return f"OK: {name}\n{extra_lines}"


def _fmt_self_update(d: dict) -> str:
    return ("🔄 self_update queued — agent will exit in ~3s and the task "
            "scheduler will relaunch it. Run /status in 30–60s to see the "
            "new version.")


def _fmt_host_restart(d: dict) -> str:
    delay = d.get("restart_in_s", 30)
    reason = d.get("reason", "")
    extra = f" — {reason}" if reason else ""
    return (f"⚠️ Host reboot scheduled in {delay}s{extra}. "
            f"Use /host_cancel within the window to abort.")


def _fmt_host_cancel(d: dict) -> str:
    if d.get("cancelled"):
        return "✅ Host reboot cancelled."
    return "ℹ️ " + (d.get("note") or "no shutdown was pending")


def _fmt_telegram_status(d: dict) -> str:
    running = d.get("running")
    allow = d.get("allowlist") or []
    state = "running" if running else "down"
    return f"Telegram bot: {state}\nAllowlisted users: {len(allow)} ({', '.join(str(a) for a in allow) or '—'})"


def _fmt_job_started(tool: str, reply) -> str:
    """Initial ack for tools that return a job_id; the watcher will follow up."""
    data = reply.data or {}
    name = (data.get("repo") or data.get("deploy") or data.get("service")
            or data.get("model") or "")
    suffix = f" {name}" if name else ""
    short = (reply.job_id or "")[:8]
    return f"⏳ {tool}{suffix} started (job {short}) — I'll let you know when it's done."


def _fmt_job_done(tool: str, done, initial_data: dict) -> str:
    icon = "✅" if done.ok else "❌"
    name = (initial_data.get("repo") or initial_data.get("deploy")
            or initial_data.get("service") or initial_data.get("model") or "")
    suffix = f" {name}" if name else ""
    secs = (done.duration_ms or 0) / 1000
    rc = "" if done.exit_code is None else f", rc={done.exit_code}"
    line = f"{icon} {tool}{suffix} finished — {secs:.1f}s{rc}"
    if done.error:
        line += f"\n  error: {done.error}"
    return line


_FORMATTERS = {
    "status": _fmt_status,
    "list_apps": _fmt_apps,
    "list_repos": _fmt_repos,
    "list_services": _fmt_services,
    "list_deploys": _fmt_deploys,
    "machine_stats": _fmt_machine,
    "ollama_list": _fmt_ollama_list,
    "ollama_ps": _fmt_ollama_ps,
    "ollama_stop": _fmt_simple_named,
    "app_logs": _fmt_app_logs,
    "start_app": _fmt_simple_named,
    "stop_app": _fmt_simple_named,
    "restart_app": _fmt_simple_named,
    "register_app": _fmt_simple_named,
    "unregister_app": _fmt_simple_named,
    "register_repo": _fmt_simple_named,
    "unregister_repo": _fmt_simple_named,
    "update_repo_token": _fmt_simple_named,
    "git_pull": _fmt_simple_named,            # used only when no job_id
    "service_restart": _fmt_simple_named,     # ditto
    "self_update": _fmt_self_update,
    "host_restart": _fmt_host_restart,
    "host_cancel_restart": _fmt_host_cancel,
    "telegram_status": _fmt_telegram_status,
    "telegram_allow": _fmt_telegram_status,
    "telegram_revoke": _fmt_telegram_status,
    "telegram_reload": _fmt_telegram_status,
}
