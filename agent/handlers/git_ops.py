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


_AUTH_ENV_VAR = "AGENT_GIT_AUTH"


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


def _origin_base(url: str) -> str | None:
    """Return scheme://host/ for use in git's http.<base>.extraheader key."""
    p = urlparse(url)
    if not p.scheme or not p.netloc:
        return None
    return f"{p.scheme}://{p.netloc}/"


def _git_cmd(args: str, *, url: str, token: Optional[str]) -> tuple[str, dict | None, list[str]]:
    """Build (command_str, env, redact_list) for a git invocation.

    If token is provided, injects --config-env=http.<base>.extraheader=AGENT_GIT_AUTH
    immediately after the leading 'git'. The token is only ever in the env
    var value, never in argv. `redact` is included as a defensive backstop.
    """
    if not token:
        return args, None, []
    base = _origin_base(url)
    if not base:
        return args, None, []

    parts = args.split(" ", 1)
    if not parts or parts[0] != "git":
        return args, None, [token]
    rest = parts[1] if len(parts) > 1 else ""
    cfg_arg = f'--config-env=http.{base}.extraheader={_AUTH_ENV_VAR}'
    cmd = f'git {cfg_arg} {rest}'.strip()

    env = os.environ.copy()
    env[_AUTH_ENV_VAR] = f"Authorization: token {token}"
    return cmd, env, [token]


async def _ensure_repo(ctx: JobContext, repo: _ResolvedRepo) -> int:
    if (repo.path / ".git").exists():
        return 0
    await ctx.info(f"cloning {repo.url} into {repo.path}")
    repo.path.parent.mkdir(parents=True, exist_ok=True)
    raw = f'git clone --branch {repo.branch} "{repo.url}" "{repo.path}"'
    cmd, env, redact = _git_cmd(raw, url=repo.url, token=repo.token)
    return await run_subprocess(
        ctx, cmd, cwd=str(repo.path.parent),
        timeout_s=300, env=env, redact=redact,
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
            raw = (
                f"git fetch origin {repo.branch} && "
                f"git checkout {repo.branch} && "
                f"git reset --hard origin/{repo.branch}"
            )
            cmd_str, env, redact = _git_cmd(raw, url=repo.url, token=repo.token)
            rc = await run_subprocess(
                jctx, cmd_str, cwd=str(repo.path),
                timeout_s=300, env=env, redact=redact,
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
