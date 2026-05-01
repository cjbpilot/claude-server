"""Read-only access to the external watchdog's restart history."""

from __future__ import annotations

import json
from pathlib import Path

from shared.protocol import Command, Reply

STATE_PATH = Path(r"C:\ProgramData\ClaudeAgent\watchdog.json")


async def handle_watchdog_stats(hctx, cmd: Command) -> Reply:
    if not STATE_PATH.exists():
        return Reply(id=cmd.id, ok=True, data={
            "history": [], "totals": {"all_time": 0}, "last_probe": None,
            "note": "no watchdog state yet (watchdog hasn't run, or never killed the agent)",
        })
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return Reply(id=cmd.id, ok=False, error=f"could not read state: {e!r}")

    # Trim history for the wire to the last N kills only - all_time count
    # remains accurate via totals.
    history = data.get("history") or []
    kills = [e for e in history if e.get("kind") == "kill"]
    data["recent_kills"] = kills[-20:]
    data.pop("history", None)
    return Reply(id=cmd.id, ok=True, data=data)
