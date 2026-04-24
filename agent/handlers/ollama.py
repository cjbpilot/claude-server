"""Ollama HTTP API wrappers."""

from __future__ import annotations

import httpx

from agent.jobs import JobContext
from shared.protocol import Command, Reply, new_id


async def handle_list(hctx, cmd: Command) -> Reply:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{hctx.cfg.ollama_url}/api/tags")
            r.raise_for_status()
            return Reply(id=cmd.id, ok=True, data=r.json())
    except Exception as e:
        return Reply(id=cmd.id, ok=False, error=repr(e))


async def handle_ps(hctx, cmd: Command) -> Reply:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{hctx.cfg.ollama_url}/api/ps")
            r.raise_for_status()
            return Reply(id=cmd.id, ok=True, data=r.json())
    except Exception as e:
        return Reply(id=cmd.id, ok=False, error=repr(e))


async def handle_pull(hctx, cmd: Command) -> Reply:
    model = cmd.args.get("model")
    if not model:
        return Reply(id=cmd.id, ok=False, error="missing 'model'")

    job_id = new_id()
    jctx = JobContext(job_id=job_id, host=hctx.cfg.host_id, nc=hctx.nc)

    async def run():
        try:
            async with httpx.AsyncClient(timeout=None) as c:
                async with c.stream(
                    "POST",
                    f"{hctx.cfg.ollama_url}/api/pull",
                    json={"name": model, "stream": True},
                ) as r:
                    r.raise_for_status()
                    async for line in r.aiter_lines():
                        if line:
                            await jctx.log(line)
            await jctx.done(ok=True, exit_code=0)
        except Exception as e:
            await jctx.done(ok=False, error=repr(e))

    hctx.runner.spawn(run())
    return Reply(id=cmd.id, ok=True, job_id=job_id, data={"model": model})


async def handle_stop(hctx, cmd: Command) -> Reply:
    model = cmd.args.get("model")
    if not model:
        return Reply(id=cmd.id, ok=False, error="missing 'model'")
    try:
        # Ollama stops a loaded model by sending a generate request with keep_alive=0.
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{hctx.cfg.ollama_url}/api/generate",
                json={"model": model, "keep_alive": 0, "prompt": ""},
            )
            r.raise_for_status()
            return Reply(id=cmd.id, ok=True, data={"stopped": model})
    except Exception as e:
        return Reply(id=cmd.id, ok=False, error=repr(e))
