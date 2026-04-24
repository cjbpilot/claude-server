"""Thin NATS wrapper used by the MCP server to talk to a specific host."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

import nats
from nats.errors import TimeoutError as NatsTimeout

from shared import protocol, subjects


class AgentClient:
    def __init__(self, host_id: str, nats_url: str, creds_file: str):
        self.host_id = host_id
        self.nats_url = nats_url
        self.creds_file = creds_file
        self.nc = None

    async def connect(self) -> None:
        self.nc = await nats.connect(
            servers=[self.nats_url],
            user_credentials=self.creds_file,
            name=f"claude-mcp/{self.host_id}",
        )

    async def close(self) -> None:
        if self.nc:
            await self.nc.drain()

    async def request(self, tool: str, args: dict[str, Any] | None = None, timeout: float = 30) -> protocol.Reply:
        assert self.nc is not None
        cmd = protocol.Command(tool=tool, args=args or {})
        try:
            msg = await self.nc.request(
                subjects.cmd(self.host_id, tool),
                cmd.to_json(),
                timeout=timeout,
            )
        except NatsTimeout:
            return protocol.Reply(
                id=cmd.id, ok=False, error=f"agent did not reply within {timeout}s — is it online?"
            )
        return protocol.Reply.from_json(msg.data)

    @asynccontextmanager
    async def follow_job(self, job_id: str, max_s: float = 600):
        """Yield an async iterator of (kind, payload) for a job until it finishes.

        kind is 'log' (payload=LogLine) or 'done' (payload=JobDone).
        """
        assert self.nc is not None
        queue: asyncio.Queue = asyncio.Queue()

        async def on_log(msg):
            try:
                await queue.put(("log", protocol.LogLine.from_json(msg.data)))
            except Exception as e:
                await queue.put(("log", protocol.LogLine(job_id=job_id, stream="stderr", line=f"bad log: {e!r}")))

        async def on_done(msg):
            try:
                await queue.put(("done", protocol.JobDone.from_json(msg.data)))
            except Exception as e:
                await queue.put(("done", protocol.JobDone(job_id=job_id, ok=False, error=repr(e))))

        sub_log = await self.nc.subscribe(subjects.job_log(self.host_id, job_id), cb=on_log)
        sub_done = await self.nc.subscribe(subjects.job_done(self.host_id, job_id), cb=on_done)

        async def iterator():
            deadline = time.time() + max_s
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    yield ("timeout", None)
                    return
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    yield ("timeout", None)
                    return
                yield (kind, payload)
                if kind == "done":
                    return

        try:
            yield iterator()
        finally:
            await sub_log.unsubscribe()
            await sub_done.unsubscribe()
