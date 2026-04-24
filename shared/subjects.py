"""NATS subject naming conventions.

All subjects are scoped by host_id so multiple machines can share one account.

    agent.<host>.cmd.<tool>              # request/reply, short commands
    agent.<host>.jobs.<job_id>.log       # streamed stdout/stderr lines
    agent.<host>.jobs.<job_id>.done      # terminal status for a job
    agent.<host>.status                  # periodic heartbeat
    agent.<host>.events                  # agent lifecycle (starting, updating, ...)
"""

from __future__ import annotations


def cmd(host: str, tool: str) -> str:
    return f"agent.{host}.cmd.{tool}"


def cmd_wildcard(host: str) -> str:
    return f"agent.{host}.cmd.*"


def job_log(host: str, job_id: str) -> str:
    return f"agent.{host}.jobs.{job_id}.log"


def job_done(host: str, job_id: str) -> str:
    return f"agent.{host}.jobs.{job_id}.done"


def status(host: str) -> str:
    return f"agent.{host}.status"


def events(host: str) -> str:
    return f"agent.{host}.events"
