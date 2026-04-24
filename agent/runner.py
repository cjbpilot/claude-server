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
        self.nc = await nats.connect(
            servers=[self.cfg.nats_url],
            user_credentials=str(self.cfg.nats_creds),
            name=f"claude-agent/{self.cfg.host_id}",
            max_reconnect_attempts=-1,
            reconnect_time_wait=2,
        )
        try:
            await self._publish_event("started", f"agent {__version__} online")
            await self._announce_update_report()

            # One wildcard subscription covers every tool.
            await self.nc.subscribe(
                subjects.cmd_wildcard(self.cfg.host_id),
                cb=self._handle_message,
            )

            self.spawn(self._heartbeat_loop())

            await self._stop.wait()
            await self._publish_event("stopping", self._stop_reason or "stop requested")
        finally:
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


def main() -> int:
    import signal

    _setup_logging()
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
