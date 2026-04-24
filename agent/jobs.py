"""Background job runner: streams stdout/stderr lines to NATS subjects."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from shared import protocol, subjects


@dataclass
class JobContext:
    job_id: str
    host: str
    nc: "object"  # nats.aio.client.Client — avoid hard import
    started_at: float = field(default_factory=time.time)

    async def log(self, line: str, stream: str = "stdout") -> None:
        msg = protocol.LogLine(job_id=self.job_id, stream=stream, line=line)
        await self.nc.publish(subjects.job_log(self.host, self.job_id), msg.to_json())

    async def info(self, line: str) -> None:
        await self.log(line, stream="info")

    async def done(self, ok: bool, exit_code: int | None = None, error: str | None = None) -> None:
        duration_ms = int((time.time() - self.started_at) * 1000)
        msg = protocol.JobDone(
            job_id=self.job_id,
            ok=ok,
            exit_code=exit_code,
            error=error,
            duration_ms=duration_ms,
        )
        await self.nc.publish(subjects.job_done(self.host, self.job_id), msg.to_json())


async def run_subprocess(ctx: JobContext, cmd: str, cwd: str, timeout_s: int = 600) -> int:
    """Run a shell command, stream its output, return exit code.

    Uses the Windows shell so PowerShell one-liners and chained commands work
    out of the box.
    """
    await ctx.info(f"$ {cmd}  (cwd={cwd})")

    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def pump(stream, name):
        assert stream is not None
        while True:
            raw = await stream.readline()
            if not raw:
                return
            try:
                text = raw.decode("utf-8", errors="replace").rstrip()
            except Exception:
                text = repr(raw)
            await ctx.log(text, stream=name)

    try:
        await asyncio.wait_for(
            asyncio.gather(
                pump(proc.stdout, "stdout"),
                pump(proc.stderr, "stderr"),
                proc.wait(),
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await ctx.log(f"timeout after {timeout_s}s", stream="stderr")
        return 124

    return proc.returncode or 0
