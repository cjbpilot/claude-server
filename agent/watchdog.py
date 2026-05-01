"""External watchdog: probes the agent over NATS, kills+restarts it if dead.

Runs as its own scheduled task (`ClaudeAgentWatchdog`), every 60s, totally
independent of the main agent process. The agent's internal watchdog can't
recover certain hang modes because it's running inside the broken event
loop. This script lives outside, so when the agent is wedged it can:

  1. Detect the wedge via a 10s `ping` request that doesn't reply.
  2. taskkill /F /T the agent's process tree using the PID written by the
     agent on boot at C:\\ProgramData\\ClaudeAgent\\agent.pid.
  3. Trigger `schtasks /Run /TN ClaudeAgent` so the main task relaunches.
  4. Append the kill to a state file so we can track frequency over time.

Restart history is at C:\\ProgramData\\ClaudeAgent\\watchdog.json.
Per-tick log at C:\\ProgramData\\ClaudeAgent\\logs\\watchdog.log.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make sibling 'shared' importable when run as `python -m agent.watchdog`.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import nats  # noqa: E402

from agent import config  # noqa: E402
from shared import protocol, subjects  # noqa: E402

STATE_PATH = Path(r"C:\ProgramData\ClaudeAgent\watchdog.json")
LOG_PATH = Path(r"C:\ProgramData\ClaudeAgent\logs\watchdog.log")
PID_PATH = Path(r"C:\ProgramData\ClaudeAgent\agent.pid")
PROBE_TIMEOUT_S = 10
ALARM_THRESHOLD_PER_HOUR = 5


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        # Last resort - print to stderr (will be lost in scheduled task context
        # but better than nothing if logging is broken).
        try:
            print(f"[{ts}] {msg}", file=sys.stderr)
        except Exception:
            pass


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"history": [], "totals": {"all_time": 0}, "last_probe": None}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"history": [], "totals": {"all_time": 0}, "last_probe": None}


def _save_state(s: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
        # Lock down ACL like the other agent secret/state files.
        if sys.platform == "win32":
            try:
                subprocess.run(
                    ["icacls", str(STATE_PATH), "/inheritance:r",
                     "/grant", "SYSTEM:F", "Administrators:F"],
                    check=False, capture_output=True,
                )
            except Exception:
                pass
    except Exception:
        _log(f"could not persist state: {STATE_PATH}")


def _compute_totals(history: list) -> dict:
    now = time.time()
    return {
        "all_time": len(history),
        "last_1h": sum(1 for e in history if e.get("ts", 0) >= now - 3600),
        "last_24h": sum(1 for e in history if e.get("ts", 0) >= now - 86400),
        "last_7d": sum(1 for e in history if e.get("ts", 0) >= now - 7 * 86400),
    }


def _record_event(kind: str, **fields) -> dict:
    s = _load_state()
    history = s.setdefault("history", [])
    entry = {"ts": int(time.time()), "kind": kind, **fields}
    history.append(entry)
    if len(history) > 1000:
        del history[: len(history) - 1000]
    s["totals"] = _compute_totals(history)
    if kind == "probe_ok" or kind == "probe_fail":
        s["last_probe"] = entry
    _save_state(s)
    return s


async def probe(cfg) -> bool:
    """NATS-request the agent's ping handler. True on success."""
    nc = None
    try:
        nc = await nats.connect(
            servers=[cfg.nats_url],
            user_credentials=str(cfg.nats_creds),
            name="claude-agent-watchdog",
            connect_timeout=PROBE_TIMEOUT_S,
        )
        cmd = protocol.Command(tool="ping")
        msg = await nc.request(
            subjects.cmd(cfg.host_id, "ping"),
            cmd.to_json(),
            timeout=PROBE_TIMEOUT_S,
        )
        reply = protocol.Reply.from_json(msg.data)
        return reply.ok
    except Exception as e:
        _log(f"probe failed: {type(e).__name__}: {e!r}")
        return False
    finally:
        if nc is not None:
            try:
                await nc.close()
            except Exception:
                pass


def kill_agent() -> int | None:
    """Kill the agent process tree using its PID file. Returns the PID killed."""
    if not PID_PATH.exists():
        _log("no agent.pid file - cannot identify agent process")
        return None
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8").strip())
    except Exception as e:
        _log(f"could not read pid file: {e!r}")
        return None
    try:
        r = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            check=False, capture_output=True, text=True, timeout=20,
        )
        if r.returncode == 0:
            _log(f"taskkill ok: pid {pid}")
        else:
            _log(f"taskkill rc={r.returncode}: {r.stdout.strip()} {r.stderr.strip()}")
        return pid
    except Exception as e:
        _log(f"taskkill exception: {e!r}")
        return None


def trigger_task_run() -> None:
    try:
        r = subprocess.run(
            ["schtasks", "/Run", "/TN", "ClaudeAgent"],
            check=False, capture_output=True, text=True, timeout=20,
        )
        if r.returncode == 0:
            _log("schtasks /Run ClaudeAgent ok")
        else:
            _log(f"schtasks /Run rc={r.returncode}: {r.stderr.strip()}")
    except Exception as e:
        _log(f"schtasks /Run exception: {e!r}")


async def run_once() -> int:
    cfg = config.load()
    healthy = await probe(cfg)
    if healthy:
        _record_event("probe_ok")
        return 0

    _log("agent unresponsive; killing")
    pid = kill_agent()
    state = _record_event("kill", reason="probe_timeout", pid_killed=pid)

    last_1h = state["totals"].get("last_1h", 0)
    if last_1h >= ALARM_THRESHOLD_PER_HOUR:
        _log(f"ALARM: {last_1h} kills in last hour - investigate; "
             f"all_time={state['totals'].get('all_time', 0)}")
        _record_event("alarm", last_1h=last_1h)

    # Give Windows a beat to release handles, then trigger relaunch.
    await asyncio.sleep(2)
    trigger_task_run()
    return 0


def main() -> int:
    try:
        return asyncio.run(run_once())
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        _log(f"unhandled: {e!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
