"""machine_stats: rich utilisation snapshot + windowed summary."""

from __future__ import annotations

from shared.protocol import Command, Reply


async def handle_machine_stats(hctx, cmd: Command) -> Reply:
    """Return current snapshot + summary stats over a window.

    Args:
      window_minutes (int, default 5) — how far back to summarise. Capped at 60.
      top_processes (int, default 10) — how many to return per ranking. 0 to skip.
    """
    minutes = int(cmd.args.get("window_minutes", 5) or 5)
    minutes = max(1, min(60, minutes))
    top_n = int(cmd.args.get("top_processes", 10) or 0)
    top_n = max(0, min(50, top_n))

    collector = hctx.runner.stats
    data = {
        "current": collector.latest(),
        "window": collector.summarize(minutes * 60),
    }
    if top_n > 0:
        data["top_processes"] = collector.top_processes(top_n)
    return Reply(id=cmd.id, ok=True, data=data)
