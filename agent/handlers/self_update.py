"""self_update: pulls latest from git, reinstalls deps, restarts the service.

Because the agent cannot safely overwrite or restart itself from within its own
process, it spawns a detached updater script and exits. The updater:

  1. Waits for the agent's PID to actually exit.
  2. Runs `git pull` in install_dir.
  3. Runs `pip install -r agent/requirements.txt` in the venv.
  4. Starts the Windows service again.
  5. Writes a short report file the new agent publishes on startup.

The caller gets an immediate reply with a job_id. Because the agent is about
to die, no job_log / job_done will arrive for this job — instead, the fresh
agent on restart publishes an 'updated' event on agent.<host>.events and
includes the report in its next heartbeat payload.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from shared.protocol import Command, Reply, new_id


UPDATE_REPORT_PATH = Path(r"C:\ProgramData\ClaudeAgent\last_update.json")


async def handle_self_update(hctx, cmd: Command) -> Reply:
    cfg = hctx.cfg
    job_id = new_id()
    updater = Path(cfg.install_dir) / "agent" / "updater.py"

    if not updater.exists():
        return Reply(id=cmd.id, ok=False, error=f"updater not found: {updater}")
    if not cfg.python_exe.exists():
        return Reply(id=cmd.id, ok=False, error=f"python_exe not found: {cfg.python_exe}")

    # Spawn the updater detached so it survives our exit.
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP keeps it alive after we die,
    # and CREATE_NO_WINDOW hides the console on service-run.
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000

    args = [
        str(cfg.python_exe),
        str(updater),
        "--pid", str(os.getpid()),
        "--install-dir", str(cfg.install_dir),
        "--service", cfg.service_name,
        "--python", str(cfg.python_exe),
        "--job-id", job_id,
    ]

    log_path = Path(r"C:\ProgramData\ClaudeAgent\logs\updater.log")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_path, "ab")
    except Exception as e:
        return Reply(id=cmd.id, ok=False, error=f"could not open updater log: {e!r}")

    try:
        subprocess.Popen(
            args,
            cwd=str(cfg.install_dir),
            creationflags=(
                DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
                if sys.platform == "win32"
                else 0
            ),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )
    except Exception as e:
        log_fh.close()
        return Reply(id=cmd.id, ok=False, error=f"failed to spawn updater: {e!r}")
    finally:
        log_fh.close()

    # Ask the runner to stop. The Windows service manager will restart us
    # because install-agent.ps1 sets SERVICE_AUTO_START with failure actions.
    hctx.runner.request_stop(reason="self_update")

    return Reply(
        id=cmd.id,
        ok=True,
        job_id=job_id,
        data={
            "message": "updater spawned; agent is restarting. Watch agent.<host>.events for 'updated'.",
            "report_path": str(UPDATE_REPORT_PATH),
        },
    )
