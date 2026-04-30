"""Main async runner. Connects to NATS, dispatches commands, emits heartbeats."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import nats
import psutil

from agent import __version__, config
from agent.handlers import REGISTRY, HandlerCtx
from agent.handlers.self_update import UPDATE_REPORT_PATH
from agent.handlers.status import _ollama_up
from shared import protocol, subjects

log = logging.getLogger("agent.runner")


class Runner:
    def __init__(self, cfg: config.Config):
        self.cfg = cfg
        self.nc = None
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._started_at = time.time()
        self._stop_reason: str | None = None
        # Lazily imported to avoid pulling httpx into modules that don't need it.
        from agent.app_manager import AppManager
        from agent.stats import StatsCollector
        self.app_manager = AppManager(cfg, self)
        self.stats = StatsCollector(disk_path=str(cfg.workspace_dir.anchor or "C:\\"))
        # Telegram bot: optional, only starts if a token file exists.
        try:
            from agent.telegram_bot import TelegramBot
            self.telegram = TelegramBot(cfg, self)
        except Exception:
            log.exception("could not construct TelegramBot (telegram lib missing?)")
            self.telegram = None

    def spawn(self, coro) -> None:
        """Fire-and-forget a background coroutine tied to the runner."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def request_stop(self, reason: str = "") -> None:
        self._stop_reason = reason
        self._stop.set()

    async def _handle_message(self, msg) -> None:
        try:
            cmd = protocol.Command.from_json(msg.data)
        except Exception as e:
            log.exception("bad command payload: %s", e)
            if msg.reply:
                await self.nc.publish(
                    msg.reply,
                    protocol.Reply(id="unknown", ok=False, error=f"bad payload: {e!r}").to_json(),
                )
            return

        # The subject's last token tells us the tool. Trust only that, not cmd.tool,
        # to avoid subject/tool mismatch abuse.
        tool = msg.subject.rsplit(".", 1)[-1]
        if tool != cmd.tool:
            reply = protocol.Reply(id=cmd.id, ok=False, error="subject/tool mismatch")
            if msg.reply:
                await self.nc.publish(msg.reply, reply.to_json())
            return

        handler = REGISTRY.get(tool)
        if not handler:
            reply = protocol.Reply(id=cmd.id, ok=False, error=f"unknown tool: {tool}")
            if msg.reply:
                await self.nc.publish(msg.reply, reply.to_json())
            return

        hctx = HandlerCtx(self.cfg, self.nc, self)
        try:
            reply = await handler(hctx, cmd)
        except Exception as e:
            log.exception("handler error")
            reply = protocol.Reply(id=cmd.id, ok=False, error=repr(e))

        if msg.reply:
            await self.nc.publish(msg.reply, reply.to_json())

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                mem = psutil.virtual_memory()
                disk = psutil.disk_usage(str(self.cfg.workspace_dir.anchor or "C:\\"))
                hb = protocol.Heartbeat(
                    host=self.cfg.host_id,
                    version=__version__,
                    uptime_s=round(time.time() - self._started_at, 1),
                    cpu_pct=psutil.cpu_percent(interval=None),
                    ram_pct=mem.percent,
                    disk_pct=disk.percent,
                    ollama_up=await _ollama_up(self.cfg.ollama_url),
                )
                await self.nc.publish(subjects.status(self.cfg.host_id), hb.to_json())
            except Exception:
                log.exception("heartbeat failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.heartbeat_s)
            except asyncio.TimeoutError:
                pass

    async def _publish_event(self, kind: str, message: str = "", data: dict | None = None) -> None:
        ev = protocol.Event(kind=kind, message=message, data=data or {})
        try:
            await self.nc.publish(subjects.events(self.cfg.host_id), ev.to_json())
        except Exception:
            log.exception("event publish failed")

    async def _connect_nats(self):
        """(Re)build the NATS client and subscribe to the command wildcard."""
        nc = await nats.connect(
            servers=[self.cfg.nats_url],
            user_credentials=str(self.cfg.nats_creds),
            name=f"claude-agent/{self.cfg.host_id}",
            max_reconnect_attempts=-1,
            reconnect_time_wait=2,
        )
        await nc.subscribe(
            subjects.cmd_wildcard(self.cfg.host_id),
            cb=self._handle_message,
        )
        return nc

    async def _nats_watchdog(self) -> None:
        """Periodically verify the NATS link is alive; rebuild it if not.

        nats-py's built-in reconnect doesn't always recover from every
        failure mode (websocket-layer hangs, server-initiated drops mid-
        long-stream). When we detect the connection is closed or a flush
        round-trip fails, we tear it down and reconnect from scratch -
        which also re-subscribes to the command wildcard. The Telegram
        bot keeps polling regardless because it's independent of NATS.
        """
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30)
                break
            except asyncio.TimeoutError:
                pass

            nc = self.nc
            if nc is None:
                continue

            healthy = False
            try:
                if getattr(nc, "is_connected", False) and not getattr(nc, "is_closed", True):
                    await asyncio.wait_for(nc.flush(timeout=2), timeout=4)
                    healthy = True
            except Exception:
                healthy = False

            if healthy:
                continue

            log.warning(
                "NATS unhealthy (connected=%s closed=%s); rebuilding",
                getattr(nc, "is_connected", "?"),
                getattr(nc, "is_closed", "?"),
            )
            try:
                await asyncio.wait_for(nc.close(), timeout=5)
            except Exception:
                pass

            try:
                self.nc = await self._connect_nats()
                log.info("NATS reconnected after watchdog")
                # Re-announce so MCP clients see we're back.
                try:
                    await self._publish_event("started", f"agent {__version__} reconnected")
                except Exception:
                    pass
            except Exception:
                log.exception("NATS reconnect attempt failed; will retry next tick")

    async def _announce_update_report(self) -> None:
        """If the updater left a report, publish it once then delete it."""
        try:
            if UPDATE_REPORT_PATH.exists():
                report = json.loads(UPDATE_REPORT_PATH.read_text(encoding="utf-8"))
                await self._publish_event("updated", "agent updated", report)
                UPDATE_REPORT_PATH.unlink(missing_ok=True)
        except Exception:
            log.exception("could not publish update report")

    async def run(self) -> None:
        log.info("connecting to %s as host=%s version=%s", self.cfg.nats_url, self.cfg.host_id, __version__)
        self.nc = await self._connect_nats()
        try:
            log.info("NATS connected - agent %s online as host=%s", __version__, self.cfg.host_id)
            await self._publish_event("started", f"agent {__version__} online")
            await self._announce_update_report()

            self.spawn(self._heartbeat_loop())
            self.spawn(self._nats_watchdog())

            # Start the rolling-window utilisation sampler.
            try:
                await self.stats.start(self)
            except Exception:
                log.exception("stats collector failed to start")

            # Boot the app supervisor: load registry, launch desired-running apps.
            try:
                await self.app_manager.start()
            except Exception:
                log.exception("app supervisor failed to start")

            # Optional Telegram bot.
            if self.telegram is not None:
                try:
                    await self.telegram.start()
                except Exception:
                    log.exception("telegram bot failed to start")

            await self._stop.wait()
            await self._publish_event("stopping", self._stop_reason or "stop requested")
        finally:
            if self.telegram is not None:
                try:
                    await self.telegram.stop()
                except Exception:
                    log.exception("telegram bot stop failed")
            try:
                await self.app_manager.stop()
            except Exception:
                log.exception("app supervisor stop failed")
            try:
                await self.stats.stop()
            except Exception:
                pass
            # Give in-flight publishes a moment to flush before closing.
            try:
                await asyncio.wait_for(self.nc.flush(timeout=2), timeout=3)
            except Exception:
                pass
            await self.nc.close()
            # Cancel any outstanding spawned tasks.
            for t in list(self._tasks):
                t.cancel()


def _setup_logging() -> None:
    logdir = Path(r"C:\ProgramData\ClaudeAgent\logs")
    try:
        logdir.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(logdir / "agent.log", encoding="utf-8")
    except Exception:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def _prevent_sleep() -> None:
    """Tell Windows to stay awake while the agent is running.

    Sets ES_CONTINUOUS | ES_SYSTEM_REQUIRED so the system does not enter
    sleep, but allows the display to power off normally. The flag persists
    for the calling thread (the agent's main thread) and clears
    automatically when the process exits, so we don't need a teardown.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        )
        log.info("sleep prevention engaged (ES_CONTINUOUS | ES_SYSTEM_REQUIRED)")
    except Exception:
        log.exception("could not set thread execution state; machine may sleep")


def _prefer_ipv4_on_windows() -> None:
    """Filter DNS results to prefer IPv4 on Windows."""
    if sys.platform != "win32":
        return
    import socket as _socket

    _orig = _socket.getaddrinfo

    def _patched(host, port, family=0, type=0, proto=0, flags=0):
        results = _orig(host, port, family, type, proto, flags)
        v4 = [r for r in results if r[0] == _socket.AF_INET]
        return v4 or results

    _socket.getaddrinfo = _patched


def _install_sync_connect_shim() -> None:
    """Avoid asyncio ProactorEventLoop ConnectEx failures in session-0.

    On Windows, running in a SYSTEM service / scheduled-task context,
    asyncio.open_connection with ssl= consistently fails during ConnectEx
    with WinError 10049 on some machines - even when a plain synchronous
    socket.connect to the same host:port from the same process succeeds.

    Work around it by doing the TCP connect synchronously with the stdlib
    socket module, then handing the already-connected socket to asyncio
    with sock= + ssl= so asyncio only drives the TLS handshake. The exact
    same code path as direct asyncio otherwise.
    """
    if sys.platform != "win32":
        return
    import asyncio as _asyncio
    import socket as _socket

    _orig_open_connection = _asyncio.open_connection

    async def _patched(host=None, port=None, *, limit=2 ** 16, ssl=None,
                       sock=None, local_addr=None, server_hostname=None,
                       ssl_handshake_timeout=None, **kwargs):
        log.info("shim: open_connection host=%r port=%r ssl=%s sock=%s sh=%r extra=%r",
                 host, port, ssl is not None, sock, server_hostname, list(kwargs))
        if sock is not None or host is None or port is None:
            log.info("shim: early-return path")
            return await _orig_open_connection(
                host=host, port=port, limit=limit, ssl=ssl, sock=sock,
                local_addr=local_addr, server_hostname=server_hostname,
                ssl_handshake_timeout=ssl_handshake_timeout, **kwargs
            )

        log.info("shim: sync-connect path")
        raw = _socket.create_connection((host, port))
        raw.setblocking(False)
        log.info("shim: sync connected fd=%d peer=%r", raw.fileno(), raw.getpeername())

        kw: dict = {"sock": raw, "limit": limit}
        if ssl is not None:
            kw["ssl"] = ssl
            kw["server_hostname"] = server_hostname or host
        if ssl_handshake_timeout is not None:
            kw["ssl_handshake_timeout"] = ssl_handshake_timeout
        kw.update(kwargs)
        log.info("shim: handing off to asyncio with keys=%r", list(kw.keys()))
        try:
            result = await _orig_open_connection(**kw)
        except BaseException:
            log.exception("shim: asyncio handoff raised")
            raise
        log.info("shim: handoff succeeded")
        return result

    _asyncio.open_connection = _patched


def main() -> int:
    import signal

    _setup_logging()
    _prevent_sleep()
    _prefer_ipv4_on_windows()
    _install_sync_connect_shim()
    cfg = config.load()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    agent = Runner(cfg)

    def _on_signal(*_):
        loop.call_soon_threadsafe(agent.request_stop, "signal")

    signal.signal(signal.SIGINT, _on_signal)
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signal.signal(sigbreak, _on_signal)
    sigterm = getattr(signal, "SIGTERM", None)
    if sigterm is not None:
        try:
            signal.signal(sigterm, _on_signal)
        except (ValueError, OSError):
            pass

    try:
        loop.run_until_complete(agent.run())
    except KeyboardInterrupt:
        pass
    finally:
        try:
            loop.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
