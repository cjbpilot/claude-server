"""Cheap liveness probe for the external watchdog."""

from __future__ import annotations

import time

from shared.protocol import Command, Reply


async def handle_ping(hctx, cmd: Command) -> Reply:
    return Reply(id=cmd.id, ok=True, data={"ts": time.time()})
