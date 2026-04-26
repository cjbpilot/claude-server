"""Arbitrary command execution on the agent host.

Designed for self-healing and ad-hoc inspection. Runs as the agent's user
account (SYSTEM by default), so it can do anything Windows admin tools can.

Caller picks the shell:
  - 'powershell' (default): Windows PowerShell 5.1 / pwsh fallback
  - 'pwsh'      : explicit PowerShell 7+
  - 'cmd'       : cmd.exe

Output is captured to memory, capped at 256 KB per stream, returned
synchronously. Timeout default 60s, max 600s.

For long-running streaming output use run_deploy or extend with a stream
flag later - keep this tool simple.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from shared.protocol import Command, Reply

log = logging.getLogger("agent.run_command")

_MAX_OUTPUT_BYTES = 256 * 1024
_MAX_TIMEOUT_S = 600


def _shell_argv(shell: str, command: str) -> list[str] | None:
    s = (shell or "powershell").lower()
    if s == "powershell":
        return ["powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", command]
    if s == "pwsh":
        return ["pwsh.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", command]
    if s == "cmd":
        return ["cmd.exe", "/c", command]
    return None


def _truncate(b: bytes) -> str:
    if len(b) <= _MAX_OUTPUT_BYTES:
        return b.decode("utf-8", errors="replace")
    head = b[:_MAX_OUTPUT_BYTES // 2].decode("utf-8", errors="replace")
    tail = b[-_MAX_OUTPUT_BYTES // 2:].decode("utf-8", errors="replace")
    return f"{head}\n... [{len(b) - _MAX_OUTPUT_BYTES} bytes truncated] ...\n{tail}"


async def handle_run_command(hctx, cmd: Command) -> Reply:
    command = cmd.args.get("command")
    if not command or not isinstance(command, str):
        return Reply(id=cmd.id, ok=False, error="missing 'command' (string)")

    shell = (cmd.args.get("shell") or "powershell").lower()
    cwd_arg = cmd.args.get("cwd")
    try:
        timeout = int(cmd.args.get("timeout_s") or 60)
    except (TypeError, ValueError):
        return Reply(id=cmd.id, ok=False, error="timeout_s must be an integer")
    timeout = max(1, min(timeout, _MAX_TIMEOUT_S))

    argv = _shell_argv(shell, command)
    if argv is None:
        return Reply(id=cmd.id, ok=False,
                     error=f"unknown shell '{shell}'; use powershell, pwsh, or cmd")

    if cwd_arg:
        cwd = Path(cwd_arg)
        if not cwd.is_absolute():
            return Reply(id=cmd.id, ok=False, error="cwd must be absolute")
        if not cwd.exists():
            return Reply(id=cmd.id, ok=False, error=f"cwd does not exist: {cwd}")
        cwd_str = str(cwd)
    else:
        cwd_str = str(hctx.cfg.workspace_dir)

    log.info("run_command shell=%s timeout=%d cwd=%s cmd=%r",
             shell, timeout, cwd_str, command[:160])

    env = os.environ.copy()

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd_str,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        return Reply(id=cmd.id, ok=False, error=f"spawn failed: {e!r}")

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return Reply(id=cmd.id, ok=False, error=f"timeout after {timeout}s")
    except Exception as e:
        return Reply(id=cmd.id, ok=False, error=repr(e))

    return Reply(id=cmd.id, ok=True, data={
        "shell": shell,
        "cwd": cwd_str,
        "exit_code": proc.returncode,
        "stdout": _truncate(stdout_bytes or b""),
        "stderr": _truncate(stderr_bytes or b""),
    })
