"""Agent configuration loader."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


DEFAULT_CONFIG_PATH = Path(r"C:\ProgramData\ClaudeAgent\agent.toml")


@dataclass
class RepoEntry:
    name: str
    path: str
    remote: str
    branch: str = "main"


@dataclass
class DeployEntry:
    name: str
    cwd: str
    cmd: str
    timeout_s: int = 600


@dataclass
class Config:
    host_id: str
    workspace_dir: Path
    install_dir: Path
    python_exe: Path
    service_name: str
    heartbeat_s: int
    nats_url: str
    nats_creds: Path
    ollama_url: str
    repos: dict[str, RepoEntry]
    deploys: dict[str, DeployEntry]
    allowed_services: list[str] = field(default_factory=list)

    def resolve_path(self, p: str) -> Path:
        path = Path(p)
        if path.is_absolute():
            return path
        return self.workspace_dir / path


def load(path: Path | None = None) -> Config:
    config_path = path or Path(os.environ.get("CLAUDE_AGENT_CONFIG", DEFAULT_CONFIG_PATH))
    if not config_path.exists():
        raise FileNotFoundError(f"agent config not found: {config_path}")

    with config_path.open("rb") as f:
        raw: dict[str, Any] = tomllib.load(f)

    agent = raw.get("agent", {})
    nats = raw.get("nats", {})
    ollama = raw.get("ollama", {})

    repos = {
        name: RepoEntry(name=name, **entry)
        for name, entry in raw.get("repos", {}).items()
    }
    deploys = {
        name: DeployEntry(name=name, **entry)
        for name, entry in raw.get("deploys", {}).items()
    }
    allowed_services = [s["name"] for s in raw.get("allowed_services", [])]

    return Config(
        host_id=agent["host_id"],
        workspace_dir=Path(agent["workspace_dir"]),
        install_dir=Path(agent["install_dir"]),
        python_exe=Path(agent["python_exe"]),
        service_name=agent.get("service_name", "ClaudeAgent"),
        heartbeat_s=int(agent.get("heartbeat_s", 10)),
        nats_url=nats["url"],
        nats_creds=Path(nats["creds_file"]),
        ollama_url=ollama.get("url", "http://127.0.0.1:11434"),
        repos=repos,
        deploys=deploys,
        allowed_services=allowed_services,
    )
