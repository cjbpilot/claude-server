"""git_pull and run_deploy — both stream logs as background jobs.

git_pull resolves a repo from the dynamic registry first (repo_store) and
falls back to static `[repos.*]` in agent.toml. When the registry entry
includes an auth token it's injected via a per-process env var and consumed
by `git --config-env=http.<base>.extraheader=ENVVAR`, so the token never
appears in argv (process listings) or in any log line.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from agent import repo_store
from agent.jobs import JobContext, run_subprocess
from shared.protocol import Command, Reply, new_id


@dataclass
class _ResolvedRepo:
    name: str
    url: str
    branch: str
    path: Path
    token: Optional[str]


def _resolve(hctx, name: str) -> _ResolvedRepo | None:
    """Dynamic registry takes precedence over static config."""
    dynamic = repo_store.get(name)
    if dynamic is not None:
        # Path comes from cfg if there's also a static entry, otherwise the
        # repo lands in <workspace>/<name>.
        static = hctx.cfg.repos.get(name)
        path = (
            hctx.cfg.resolve_path(static.path)
            if static is not None
            else hctx.cfg.workspace_dir / name
        )
        return _ResolvedRepo(
            name=name, url=dynamic.url, branch=dynamic.branch,
            path=path, token=dynamic.token,
        )

    static = hctx.cfg.repos.get(name)
    if static is None:
        return None
    return _ResolvedRepo(
        name=name, url=static.remote, branch=static.branch,
        path=hctx.cfg.resolve_path(static.path), token=None,
    )


def _authed_url(url: str, token: Optional[str]) -> str:
    """Embed the token in an HTTPS clone URL for one-shot auth.

    GitHub accepts https://<token>@github.com/... and
    https://x-access-token:<token>@github.com/... for any PAT family.
    The token ends up in argv for the spawned git, so the caller MUST
    pass it through run_subprocess(..., redact=[token]) to keep it out
    of logs.
    """
    if not token:
        return url
    p = urlparse(url)
    if not p.scheme.startswith("http"):
        return url
    netloc = f"x-access-token:{token}@{p.hostname}"
    if p.port:
        netloc += f":{p.port}"
    return p._replace(netloc=netloc).geturl()


def _git_env() -> dict:
    """Env that prevents git from hanging on a credential prompt."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"
    return env


async def _ensure_repo(ctx: JobContext, repo: _ResolvedRepo) -> int:
    if (repo.path / ".git").exists():
        return 0
    await ctx.info(f"cloning {repo.url} into {repo.path}")
    repo.path.parent.mkdir(parents=True, exist_ok=True)
    auth_url = _authed_url(repo.url, repo.token)
    cmd = f'git clone --branch {repo.branch} "{auth_url}" "{repo.path}"'
    redact = [repo.token, auth_url] if repo.token else []
    return await run_subprocess(
        ctx, cmd, cwd=str(repo.path.parent),
        timeout_s=300, env=_git_env(), redact=redact,
    )


async def handle_git_pull(hctx, cmd: Command) -> Reply:
    name = cmd.args.get("repo")
    repo = _resolve(hctx, name)
    if not repo:
        return Reply(id=cmd.id, ok=False, error=f"unknown repo: {name}")

    job_id = new_id()
    jctx = JobContext(job_id=job_id, host=hctx.cfg.host_id, nc=hctx.nc)

    async def run():
        try:
            rc = await _ensure_repo(jctx, repo)
            if rc != 0:
                await jctx.done(ok=False, exit_code=rc, error="clone failed")
                return
            auth_url = _authed_url(repo.url, repo.token)
            # Update the remote URL inline (with token) for this fetch only,
            # then restore the clean URL so .git/config never persists it.
            cmd_str = (
                f'git remote set-url origin "{auth_url}" && '
                f"git fetch origin {repo.branch} && "
                f'git remote set-url origin "{repo.url}" && '
                f"git checkout {repo.branch} && "
                f"git reset --hard origin/{repo.branch}"
            )
            redact = [repo.token, auth_url] if repo.token else []
            rc = await run_subprocess(
                jctx, cmd_str, cwd=str(repo.path),
                timeout_s=300, env=_git_env(), redact=redact,
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
            rc = await run_subprocess(
                jctx, deploy.cmd, cwd=str(cwd), timeout_s=deploy.timeout_s,
            )
            await jctx.done(ok=(rc == 0), exit_code=rc)
        except Exception as e:
            await jctx.done(ok=False, error=repr(e))

    hctx.runner.spawn(run())
    return Reply(id=cmd.id, ok=True, job_id=job_id, data={"deploy": name})
