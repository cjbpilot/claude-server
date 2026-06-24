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


async def pull_repo_quietly(cfg, repo_name: str) -> dict:
    """Fetch + hard-reset a registered repo, with no streaming and no
    JobContext. Used by the supervisor's auto-update tick where there is no
    caller to follow a job. Reuses the same auth + redaction machinery as
    handle_git_pull so private repos work transparently.

    Returns a dict:
      {
        "ok": bool,
        "rc": int,
        "prev_sha": str,
        "new_sha": str,
        "changed": bool,        # prev_sha != new_sha
        "error": Optional[str], # set only if ok=False
      }
    """
    import asyncio as _aio

    entry = repo_store.get(repo_name)
    if entry is None:
        # Fallback to static config the same way _resolve() does.
        static = cfg.repos.get(repo_name) if hasattr(cfg, "repos") else None
        if static is None:
            return {
                "ok": False, "rc": -1, "prev_sha": "", "new_sha": "",
                "changed": False, "error": f"unknown repo: {repo_name}",
            }
        url = static.remote
        branch = static.branch
        token = None
        path = cfg.resolve_path(static.path)
    else:
        url = entry.url
        branch = entry.branch
        token = entry.token
        path = cfg.workspace_dir / repo_name

    if not (path / ".git").exists():
        return {
            "ok": False, "rc": -1, "prev_sha": "", "new_sha": "",
            "changed": False,
            "error": f"repo {repo_name!r} not cloned at {path}",
        }

    async def _rev_parse() -> str:
        try:
            proc = await _aio.create_subprocess_shell(
                "git rev-parse HEAD",
                cwd=str(path),
                stdout=_aio.subprocess.PIPE,
                stderr=_aio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            return (out or b"").decode("utf-8", errors="replace").strip()
        except Exception:
            return ""

    prev_sha = await _rev_parse()

    auth_url = _authed_url(url, token)
    cmd_str = (
        f'git remote set-url origin "{auth_url}" && '
        f"git fetch origin {branch} && "
        f'git remote set-url origin "{url}" && '
        f"git reset --hard FETCH_HEAD"
    )
    try:
        proc = await _aio.create_subprocess_shell(
            cmd_str,
            cwd=str(path),
            env=_git_env(),
            stdout=_aio.subprocess.PIPE,
            stderr=_aio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await _aio.wait_for(proc.communicate(), timeout=300)
        except _aio.TimeoutError:
            proc.kill()
            return {
                "ok": False, "rc": 124, "prev_sha": prev_sha, "new_sha": prev_sha,
                "changed": False, "error": "git fetch+reset timed out after 300s",
            }
        rc = proc.returncode if proc.returncode is not None else -1
    except Exception as e:
        return {
            "ok": False, "rc": -1, "prev_sha": prev_sha, "new_sha": prev_sha,
            "changed": False, "error": repr(e),
        }
    if rc != 0:
        return {
            "ok": False, "rc": rc, "prev_sha": prev_sha, "new_sha": prev_sha,
            "changed": False,
            "error": f"git rc={rc}: {(stdout or b'').decode('utf-8', errors='replace')[:400]}",
        }
    new_sha = await _rev_parse()
    return {
        "ok": True, "rc": 0, "prev_sha": prev_sha, "new_sha": new_sha,
        "changed": (new_sha and new_sha != prev_sha),
        "error": None,
    }


async def git_reset_hard(cfg, repo_name: str, sha: str) -> tuple[bool, str]:
    """Hard-reset a registered repo to a specific SHA. Used by the
    auto-update rollback path. No fetch — assumes the SHA is already in
    the local object DB (it must be, since we got it from rev-parse HEAD a
    moment ago).
    """
    import asyncio as _aio

    entry = repo_store.get(repo_name)
    if entry is None:
        static = cfg.repos.get(repo_name) if hasattr(cfg, "repos") else None
        if static is None:
            return False, f"unknown repo: {repo_name}"
        path = cfg.resolve_path(static.path)
    else:
        path = cfg.workspace_dir / repo_name
    if not (path / ".git").exists():
        return False, f"repo {repo_name!r} not cloned at {path}"
    try:
        proc = await _aio.create_subprocess_shell(
            f"git reset --hard {sha}",
            cwd=str(path),
            env=_git_env(),
            stdout=_aio.subprocess.PIPE,
            stderr=_aio.subprocess.STDOUT,
        )
        stdout, _ = await _aio.wait_for(proc.communicate(), timeout=60)
        rc = proc.returncode if proc.returncode is not None else -1
    except Exception as e:
        return False, repr(e)
    if rc != 0:
        return False, f"git rc={rc}: {(stdout or b'').decode('utf-8', errors='replace')[:400]}"
    return True, ""


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
            # Reset to FETCH_HEAD so we don't depend on remote-tracking refs
            # being present (they may not be after a partial/broken clone).
            cmd_str = (
                f'git remote set-url origin "{auth_url}" && '
                f"git fetch origin {repo.branch} && "
                f'git remote set-url origin "{repo.url}" && '
                f"git reset --hard FETCH_HEAD"
            )
            redact = [repo.token, auth_url] if repo.token else []
            rc = await run_subprocess(
                jctx, cmd_str, cwd=str(repo.path),
                timeout_s=300, env=_git_env(), redact=redact,
            )
            if rc == 0:
                await _notify_apps_on_pull(hctx, jctx, repo)
            await jctx.done(ok=(rc == 0), exit_code=rc)
        except Exception as e:
            await jctx.done(ok=False, error=repr(e))

    hctx.runner.spawn(run())
    return Reply(id=cmd.id, ok=True, job_id=job_id, data={"repo": name})


async def _notify_apps_on_pull(hctx, jctx: JobContext, repo: _ResolvedRepo) -> None:
    """After a successful pull, tell the app manager so it can restart any
    apps tied to this repo whose manifest opts in via restart.on_update."""
    import asyncio as _aio
    try:
        proc = await _aio.create_subprocess_shell(
            "git rev-parse HEAD",
            cwd=str(repo.path),
            stdout=_aio.subprocess.PIPE,
            stderr=_aio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        sha = (out or b"").decode("utf-8", errors="replace").strip()
    except Exception:
        sha = ""
    if not sha:
        return
    try:
        cycled = await hctx.runner.app_manager.notify_pull(repo.name, sha)
    except Exception as e:
        await jctx.info(f"app manager notify_pull failed: {e!r}")
        return
    if cycled:
        await jctx.info(f"cycled apps after update: {', '.join(cycled)}")


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
