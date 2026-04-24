"""git_pull and run_deploy — both stream logs as background jobs."""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent.jobs import JobContext, run_subprocess
from shared.protocol import Command, Reply, new_id


async def _ensure_repo(ctx: JobContext, repo_dir: Path, remote: str, branch: str) -> int:
    if (repo_dir / ".git").exists():
        return 0
    await ctx.info(f"cloning {remote} into {repo_dir}")
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    return await run_subprocess(
        ctx,
        f'git clone --branch {branch} "{remote}" "{repo_dir}"',
        cwd=str(repo_dir.parent),
        timeout_s=300,
    )


async def handle_git_pull(hctx, cmd: Command) -> Reply:
    name = cmd.args.get("repo")
    repo = hctx.cfg.repos.get(name)
    if not repo:
        return Reply(id=cmd.id, ok=False, error=f"unknown repo: {name}")

    job_id = new_id()
    jctx = JobContext(job_id=job_id, host=hctx.cfg.host_id, nc=hctx.nc)
    repo_dir = hctx.cfg.resolve_path(repo.path)

    async def run():
        try:
            rc = await _ensure_repo(jctx, repo_dir, repo.remote, repo.branch)
            if rc != 0:
                await jctx.done(ok=False, exit_code=rc, error="clone failed")
                return
            rc = await run_subprocess(
                jctx,
                f"git fetch origin {repo.branch} && git checkout {repo.branch} && git reset --hard origin/{repo.branch}",
                cwd=str(repo_dir),
                timeout_s=300,
            )
            await jctx.done(ok=(rc == 0), exit_code=rc)
        except Exception as e:
            await jctx.done(ok=False, error=repr(e))

    hctx.runner.spawn(run())
    return Reply(id=cmd.id, ok=True, job_id=job_id, data={"repo": name})


async def handle_run_deploy(hctx, cmd: Command) -> Reply:
    name = cmd.args.get("deploy")
    deploy = hctx.cfg.deploys.get(name)
    if not deploy:
        return Reply(id=cmd.id, ok=False, error=f"unknown deploy: {name}")

    job_id = new_id()
    jctx = JobContext(job_id=job_id, host=hctx.cfg.host_id, nc=hctx.nc)
    cwd = hctx.cfg.resolve_path(deploy.cwd)

    async def run():
        try:
            rc = await run_subprocess(jctx, deploy.cmd, cwd=str(cwd), timeout_s=deploy.timeout_s)
            await jctx.done(ok=(rc == 0), exit_code=rc)
        except Exception as e:
            await jctx.done(ok=False, error=repr(e))

    hctx.runner.spawn(run())
    return Reply(id=cmd.id, ok=True, job_id=job_id, data={"deploy": name})
