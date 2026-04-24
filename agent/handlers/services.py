"""Windows service control — restart only, from an allowlist."""

from __future__ import annotations

from agent.jobs import JobContext, run_subprocess
from shared.protocol import Command, Reply, new_id


async def handle_service_restart(hctx, cmd: Command) -> Reply:
    name = cmd.args.get("name")
    if name not in hctx.cfg.allowed_services:
        return Reply(id=cmd.id, ok=False, error=f"service not in allowlist: {name}")

    job_id = new_id()
    jctx = JobContext(job_id=job_id, host=hctx.cfg.host_id, nc=hctx.nc)

    async def run():
        try:
            # Use PowerShell Restart-Service so it handles stop+start and waits.
            rc = await run_subprocess(
                jctx,
                f"powershell -NoProfile -Command \"Restart-Service -Name '{name}' -Force\"",
                cwd=str(hctx.cfg.install_dir),
                timeout_s=120,
            )
            await jctx.done(ok=(rc == 0), exit_code=rc)
        except Exception as e:
            await jctx.done(ok=False, error=repr(e))

    hctx.runner.spawn(run())
    return Reply(id=cmd.id, ok=True, job_id=job_id, data={"service": name})
