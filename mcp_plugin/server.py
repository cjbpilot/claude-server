"""MCP stdio server exposing Claude-agent tools to Claude Code / Cowork.

Register with Claude Code:

    claude mcp add claude-agent \
      -e CLAUDE_AGENT_HOST_ID=my-desktop \
      -e CLAUDE_AGENT_NATS_URL=tls://connect.ngs.global \
      -e CLAUDE_AGENT_CREDS=/path/to/nats.creds \
      -- python -m mcp_plugin.server

Each tool maps 1:1 to a handler in the Windows agent.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

# Make sibling 'shared' package importable when run as a script.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp.server.fastmcp import FastMCP  # type: ignore

from mcp_plugin.config import load as load_cfg  # type: ignore
from mcp_plugin.nats_client import AgentClient  # type: ignore
from shared import protocol  # type: ignore


_cfg = load_cfg()
_client: AgentClient | None = None
_client_lock = asyncio.Lock()

mcp = FastMCP("claude-agent")


async def _get_client() -> AgentClient:
    global _client
    async with _client_lock:
        if _client is None:
            c = AgentClient(_cfg.host_id, _cfg.nats_url, str(_cfg.creds_file))
            await c.connect()
            _client = c
        return _client


def _reply_payload(reply: protocol.Reply) -> dict:
    return {k: v for k, v in asdict(reply).items() if v is not None}


async def _call(tool: str, args: dict | None = None, timeout: float | None = None) -> str:
    client = await _get_client()
    reply = await client.request(tool, args or {}, timeout=timeout or _cfg.request_timeout_s)
    return json.dumps(_reply_payload(reply), indent=2)


async def _call_and_follow(tool: str, args: dict, tail: bool = True) -> str:
    """Issue a command that may return a job_id, then stream logs until done."""
    client = await _get_client()
    reply = await client.request(tool, args, timeout=_cfg.request_timeout_s)

    if not reply.ok:
        return json.dumps(_reply_payload(reply), indent=2)
    if not reply.job_id or not tail:
        return json.dumps(_reply_payload(reply), indent=2)

    lines: list[str] = [f"job {reply.job_id} started"]
    async with client.follow_job(reply.job_id, max_s=_cfg.tail_max_s) as events:
        async for kind, payload in events:
            if kind == "log":
                prefix = {"stdout": "", "stderr": "! ", "info": "# "}.get(payload.stream, "")
                lines.append(f"{prefix}{payload.line}")
            elif kind == "done":
                lines.append(
                    f"-- done ok={payload.ok} exit={payload.exit_code} "
                    f"duration_ms={payload.duration_ms} err={payload.error or ''}"
                )
            elif kind == "timeout":
                lines.append(f"-- follow timeout after {_cfg.tail_max_s}s — job may still be running")
                break
    return "\n".join(lines)


# ---------------- Tools ----------------


@mcp.tool()
async def status() -> str:
    """Return host status: CPU, RAM, disk, uptime, Ollama health, agent version, registered repos/deploys/services."""
    return await _call("status")


@mcp.tool()
async def list_repos() -> str:
    """List the repos this agent is allowed to git_pull."""
    return await _call("list_repos")


@mcp.tool()
async def list_deploys() -> str:
    """List the deploy scripts this agent is allowed to run."""
    return await _call("list_deploys")


@mcp.tool()
async def list_services() -> str:
    """List the Windows services this agent is allowed to restart."""
    return await _call("list_services")


@mcp.tool()
async def git_pull(repo: str) -> str:
    """Pull latest for a registered repo. Streams log output until the pull finishes."""
    return await _call_and_follow("git_pull", {"repo": repo})


@mcp.tool()
async def register_repo(name: str, url: str, token: str | None = None, branch: str = "main") -> str:
    """Register a repo the agent can git_pull. Provide an HTTPS clone url and a
    GitHub PAT (ghp_/gho_/github_pat_/ghs_) for private repos. Pass token=null
    or omit it for public repos. If 'name' is already registered, the entry is
    replaced (use this also to change the URL or branch). The token is stored
    on the agent host with SYSTEM-only ACL and never echoed in any reply or
    log line."""
    args: dict = {"name": name, "url": url, "branch": branch}
    if token is not None:
        args["token"] = token
    return await _call("register_repo", args)


@mcp.tool()
async def update_repo_token(name: str, token: str | None = None) -> str:
    """Rotate the auth token for a registered repo. Pass token=null to clear
    it (e.g. if the repo became public)."""
    args: dict = {"name": name}
    if token is not None:
        args["token"] = token
    return await _call("update_repo_token", args)


@mcp.tool()
async def unregister_repo(name: str) -> str:
    """Remove a repo from the dynamic registry. Does not delete the on-disk checkout."""
    return await _call("unregister_repo", {"name": name})


# ---------------- Managed apps ----------------


@mcp.tool()
async def register_app(repo: str, name: str | None = None) -> str:
    """Register a managed app from a previously-registered repo. The agent reads
    the claude-agent manifest from the repo's README.md (a fenced ```claude-agent
    block) or from a .claude-agent.toml file at the repo root, runs the install
    command if defined, and starts the app under supervision. Provide `name`
    only if you want to override the default (which is the repo name)."""
    args: dict = {"repo": repo}
    if name:
        args["name"] = name
    return await _call("register_app", args, timeout=120)


@mcp.tool()
async def unregister_app(name: str) -> str:
    """Stop and unregister a managed app. Process is killed; on-disk repo is left in place."""
    return await _call("unregister_app", {"name": name}, timeout=30)


@mcp.tool()
async def start_app(name: str) -> str:
    """Start a registered app (if currently stopped)."""
    return await _call("start_app", {"name": name})


@mcp.tool()
async def stop_app(name: str) -> str:
    """Stop a registered app. Supervisor will not auto-restart while desired_state is 'stopped'."""
    return await _call("stop_app", {"name": name})


@mcp.tool()
async def restart_app(name: str) -> str:
    """Cycle a registered app: stop then start."""
    return await _call("restart_app", {"name": name})


@mcp.tool()
async def list_apps() -> str:
    """List managed apps with their live status (alive/desired/pid/uptime/health)."""
    return await _call("list_apps")


@mcp.tool()
async def app_logs(name: str, lines: int = 100) -> str:
    """Tail the per-app log file (stdout+stderr captured by the supervisor)."""
    return await _call("app_logs", {"name": name, "lines": lines})


# ---------------- Host power ----------------


@mcp.tool()
async def host_restart(delay_s: int = 30, force: bool = True, reason: str = "claude-agent host_restart") -> str:
    """Schedule a Windows reboot of the host machine. Default 30-second delay
    so you can call host_cancel_restart to abort. After reboot, the agent
    auto-starts via Scheduled Task and re-launches every app whose
    desired_state was 'running'."""
    return await _call("host_restart", {"delay_s": delay_s, "force": force, "reason": reason})


@mcp.tool()
async def host_cancel_restart() -> str:
    """Abort a pending host_restart while it's still in the delay window."""
    return await _call("host_cancel_restart")


@mcp.tool()
async def run_deploy(deploy: str) -> str:
    """Run a registered deploy script. Streams stdout/stderr until exit."""
    return await _call_and_follow("run_deploy", {"deploy": deploy})


@mcp.tool()
async def service_restart(name: str) -> str:
    """Restart an allowlisted Windows service."""
    return await _call_and_follow("service_restart", {"name": name})


@mcp.tool()
async def ollama_list() -> str:
    """List locally installed Ollama models."""
    return await _call("ollama_list")


@mcp.tool()
async def ollama_ps() -> str:
    """Show currently loaded Ollama models."""
    return await _call("ollama_ps")


@mcp.tool()
async def ollama_pull(model: str) -> str:
    """Pull a model via Ollama, streaming progress until it finishes."""
    return await _call_and_follow("ollama_pull", {"model": model})


@mcp.tool()
async def ollama_stop(model: str) -> str:
    """Unload a loaded Ollama model."""
    return await _call("ollama_stop", {"model": model})


@mcp.tool()
async def self_update() -> str:
    """Update the Windows agent: git pull in the install dir, reinstall deps, restart the service.

    The agent exits to allow the Windows Service Manager to relaunch it. Because
    of this, this tool returns as soon as the updater is spawned — the fresh
    agent publishes an 'updated' event on reconnect. Call `status` a few seconds
    later to confirm the new version is online.
    """
    return await _call("self_update")


def main() -> int:
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
