"""Host machine power control: restart and cancel-restart."""

from __future__ import annotations

import subprocess
import sys

from shared.protocol import Command, Reply


_DETACHED = 0x00000008
_NO_WINDOW = 0x08000000


async def handle_host_restart(hctx, cmd: Command) -> Reply:
    """Schedule a Windows reboot via shutdown.exe.

    `delay_s` (default 30) gives a window during which `host_cancel_restart`
    can abort. `force` (default true) forces running applications to close;
    the agent re-launches them on boot.
    """
    delay_s = max(0, int(cmd.args.get("delay_s", 30)))
    force = bool(cmd.args.get("force", True))
    reason = str(cmd.args.get("reason", "claude-agent host_restart"))

    if sys.platform != "win32":
        return Reply(id=cmd.id, ok=False, error="host_restart only implemented on Windows")

    args = ["shutdown.exe", "/r", "/t", str(delay_s), "/c", reason[:127]]
    if force:
        args.append("/f")

    try:
        subprocess.Popen(
            args,
            creationflags=_DETACHED | _NO_WINDOW,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as e:
        return Reply(id=cmd.id, ok=False, error=f"failed to schedule restart: {e!r}")

    return Reply(id=cmd.id, ok=True, data={
        "restart_in_s": delay_s,
        "force": force,
        "reason": reason,
        "cancel_with": "host_cancel_restart",
    })


async def handle_host_cancel_restart(hctx, cmd: Command) -> Reply:
    """Abort a pending shutdown scheduled by host_restart."""
    if sys.platform != "win32":
        return Reply(id=cmd.id, ok=False, error="windows-only")

    try:
        proc = subprocess.run(
            ["shutdown.exe", "/a"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as e:
        return Reply(id=cmd.id, ok=False, error=repr(e))

    # 1116 = "Unable to abort the system shutdown because no shutdown was in progress"
    if proc.returncode == 1116:
        return Reply(id=cmd.id, ok=True, data={"cancelled": False, "note": "no shutdown was pending"})
    if proc.returncode != 0:
        return Reply(id=cmd.id, ok=False,
                     error=f"shutdown /a rc={proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}")
    return Reply(id=cmd.id, ok=True, data={"cancelled": True})
