"""MCP plugin config. Reads from environment variables so Claude Code's
MCP server definition in settings.json / .mcp.json can configure it.

    CLAUDE_AGENT_HOST_ID   required — host segment in NATS subjects
    CLAUDE_AGENT_NATS_URL  required — e.g. tls://connect.ngs.global
    CLAUDE_AGENT_CREDS     required — path to Synadia creds file
    CLAUDE_AGENT_TIMEOUT   optional — request/reply timeout seconds (default 30)
    CLAUDE_AGENT_TAIL_MAX  optional — max seconds to follow job logs (default 600)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PluginConfig:
    host_id: str
    nats_url: str
    creds_file: Path
    request_timeout_s: float
    tail_max_s: float


def load() -> PluginConfig:
    def require(name: str) -> str:
        v = os.environ.get(name)
        if not v:
            raise RuntimeError(f"missing env var: {name}")
        return v

    return PluginConfig(
        host_id=require("CLAUDE_AGENT_HOST_ID"),
        nats_url=require("CLAUDE_AGENT_NATS_URL"),
        creds_file=Path(require("CLAUDE_AGENT_CREDS")),
        request_timeout_s=float(os.environ.get("CLAUDE_AGENT_TIMEOUT", "30")),
        tail_max_s=float(os.environ.get("CLAUDE_AGENT_TAIL_MAX", "600")),
    )
