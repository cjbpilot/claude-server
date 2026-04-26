"""Wire protocol for agent <-> MCP plugin messages.

All messages are JSON. Commands return either a synchronous result or a job_id
that the caller can follow on job_log / job_done subjects.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


def new_id() -> str:
    return uuid.uuid4().hex


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Command:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=new_id)
    ts: int = field(default_factory=now_ms)

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes) -> "Command":
        obj = json.loads(data.decode("utf-8"))
        return cls(**obj)


@dataclass
class Reply:
    """Synchronous reply to a Command via NATS request/reply."""

    id: str
    ok: bool
    data: Any = None
    error: str | None = None
    # If the command started a background job, job_id is set. The caller should
    # then subscribe to job_log / job_done subjects.
    job_id: str | None = None

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes) -> "Reply":
        obj = json.loads(data.decode("utf-8"))
        return cls(**obj)


@dataclass
class LogLine:
    job_id: str
    stream: str  # "stdout" | "stderr" | "info"
    line: str
    ts: int = field(default_factory=now_ms)

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes) -> "LogLine":
        obj = json.loads(data.decode("utf-8"))
        return cls(**obj)


@dataclass
class JobDone:
    job_id: str
    ok: bool
    exit_code: int | None = None
    error: str | None = None
    duration_ms: int = 0
    ts: int = field(default_factory=now_ms)

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes) -> "JobDone":
        obj = json.loads(data.decode("utf-8"))
        return cls(**obj)


@dataclass
class Heartbeat:
    host: str
    version: str
    uptime_s: float
    cpu_pct: float
    ram_pct: float
    disk_pct: float
    ollama_up: bool
    ts: int = field(default_factory=now_ms)

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes) -> "Heartbeat":
        obj = json.loads(data.decode("utf-8"))
        return cls(**obj)


@dataclass
class Event:
    kind: str  # "starting" | "started" | "stopping" | "updating" | "updated" | "error"
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    ts: int = field(default_factory=now_ms)

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes) -> "Event":
        obj = json.loads(data.decode("utf-8"))
        return cls(**obj)


# Canonical tool names — kept in one place so both sides agree.
TOOLS = {
    "status",
    "list_repos",
    "list_deploys",
    "list_services",
    "git_pull",
    "run_deploy",
    "register_repo",
    "unregister_repo",
    "update_repo_token",
    "tail_logs",
    "service_restart",
    "ollama_list",
    "ollama_pull",
    "ollama_ps",
    "ollama_stop",
    "self_update",
}
