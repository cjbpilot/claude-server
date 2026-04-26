"""Parse a claude-agent app manifest out of a repo checkout.

Looks for either:

  1. A fenced code block tagged ```claude-agent inside README.md (preferred,
     so authors can document the manifest in-line).
  2. A standalone `.claude-agent.toml` file at the repo root (fallback, useful
     when the README is rendered platform that doesn't show fenced blocks).

The manifest is TOML. Schema:

    [app]
    name = "stronghold"           # optional; defaults to repo name

    [install]                     # optional; runs on register and after pull
    command = "pip install -e ."
    timeout_s = 600

    [start]                       # required
    command = "python -m stronghold"
    working_dir = "."             # relative to repo root, or absolute

    [env]                         # optional
    PORT = "8080"

    [health]                      # optional
    url = "http://127.0.0.1:8080/health"
    interval_s = 30
    timeout_s = 5
    expect_status = 200

    [restart]                     # all keys optional
    on_crash = true
    on_update = true
    backoff_s = 5
    max_per_hour = 10
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


@dataclass
class InstallSpec:
    command: str
    timeout_s: int = 600


@dataclass
class StartSpec:
    command: str
    working_dir: str = "."


@dataclass
class HealthSpec:
    url: str
    interval_s: int = 30
    timeout_s: int = 5
    expect_status: int = 200


@dataclass
class RestartSpec:
    on_crash: bool = True
    on_update: bool = True
    backoff_s: int = 5
    max_per_hour: int = 10


@dataclass
class OllamaSpec:
    """Optional Ollama integration. If present, the agent ensures the listed
    models are pulled before the app starts, and injects host/model env vars
    into the app's process so it can talk to local Ollama without config."""
    host: str = "http://127.0.0.1:11434"
    env_var: str = "OLLAMA_HOST"
    model_env_var: str = "OLLAMA_MODEL"
    models: list = field(default_factory=list)
    default_model: Optional[str] = None
    warm: bool = False
    keep_alive: str = "24h"


@dataclass
class AppManifest:
    name: str
    start: StartSpec
    install: Optional[InstallSpec] = None
    env: dict = field(default_factory=dict)
    health: Optional[HealthSpec] = None
    restart: RestartSpec = field(default_factory=RestartSpec)
    ollama: Optional[OllamaSpec] = None

    def to_dict(self) -> dict:
        d: dict = {
            "name": self.name,
            "start": {"command": self.start.command, "working_dir": self.start.working_dir},
            "env": dict(self.env),
            "restart": {
                "on_crash": self.restart.on_crash,
                "on_update": self.restart.on_update,
                "backoff_s": self.restart.backoff_s,
                "max_per_hour": self.restart.max_per_hour,
            },
        }
        if self.install is not None:
            d["install"] = {"command": self.install.command, "timeout_s": self.install.timeout_s}
        if self.health is not None:
            d["health"] = {
                "url": self.health.url,
                "interval_s": self.health.interval_s,
                "timeout_s": self.health.timeout_s,
                "expect_status": self.health.expect_status,
            }
        if self.ollama is not None:
            d["ollama"] = {
                "host": self.ollama.host,
                "env_var": self.ollama.env_var,
                "model_env_var": self.ollama.model_env_var,
                "models": list(self.ollama.models),
                "default_model": self.ollama.default_model,
                "warm": self.ollama.warm,
                "keep_alive": self.ollama.keep_alive,
            }
        return d


_FENCE_RE = re.compile(
    r"^```claude-agent\s*\n(.*?)^```\s*$",
    re.DOTALL | re.MULTILINE,
)


def _extract_from_readme(repo_dir: Path) -> Optional[str]:
    for name in ("README.md", "Readme.md", "readme.md"):
        p = repo_dir / name
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
        m = _FENCE_RE.search(text)
        if m:
            return m.group(1)
        return None
    return None


def _extract_from_file(repo_dir: Path) -> Optional[str]:
    p = repo_dir / ".claude-agent.toml"
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def parse_manifest(repo_dir: Path, default_name: str) -> AppManifest:
    """Parse the claude-agent manifest from a repo dir.

    Raises ValueError if neither source exists or parsing fails.
    """
    raw = _extract_from_readme(repo_dir) or _extract_from_file(repo_dir)
    if raw is None:
        raise ValueError(
            "no claude-agent manifest found "
            "(looked for ```claude-agent block in README.md and "
            ".claude-agent.toml at the repo root)"
        )

    try:
        data = tomllib.loads(raw)
    except Exception as e:
        raise ValueError(f"invalid TOML in claude-agent manifest: {e}") from e

    return _from_dict(data, default_name)


def manifest_from_dict(data: dict, default_name: str) -> AppManifest:
    """Reconstruct an AppManifest from the dict produced by `to_dict()`.

    Used when loading the registry from disk.
    """
    return _from_dict(data, default_name)


def _from_dict(data: dict, default_name: str) -> AppManifest:
    app = data.get("app") or {}
    name = (app.get("name") or default_name).strip()

    start_raw = data.get("start") or {}
    if not start_raw.get("command"):
        raise ValueError("manifest must include [start] command")
    start = StartSpec(
        command=str(start_raw["command"]),
        working_dir=str(start_raw.get("working_dir", ".")),
    )

    install: Optional[InstallSpec] = None
    install_raw = data.get("install") or {}
    if install_raw.get("command"):
        install = InstallSpec(
            command=str(install_raw["command"]),
            timeout_s=int(install_raw.get("timeout_s", 600)),
        )

    env = {str(k): str(v) for k, v in (data.get("env") or {}).items()}

    health: Optional[HealthSpec] = None
    health_raw = data.get("health") or {}
    if health_raw.get("url"):
        health = HealthSpec(
            url=str(health_raw["url"]),
            interval_s=int(health_raw.get("interval_s", 30)),
            timeout_s=int(health_raw.get("timeout_s", 5)),
            expect_status=int(health_raw.get("expect_status", 200)),
        )

    restart_raw = data.get("restart") or {}
    restart = RestartSpec(
        on_crash=bool(restart_raw.get("on_crash", True)),
        on_update=bool(restart_raw.get("on_update", True)),
        backoff_s=int(restart_raw.get("backoff_s", 5)),
        max_per_hour=int(restart_raw.get("max_per_hour", 10)),
    )

    ollama: Optional[OllamaSpec] = None
    ollama_raw = data.get("ollama") or {}
    if ollama_raw:
        ollama = OllamaSpec(
            host=str(ollama_raw.get("host", "http://127.0.0.1:11434")),
            env_var=str(ollama_raw.get("env_var", "OLLAMA_HOST")),
            model_env_var=str(ollama_raw.get("model_env_var", "OLLAMA_MODEL")),
            models=[str(m) for m in (ollama_raw.get("models") or [])],
            default_model=(str(ollama_raw["default_model"]) if ollama_raw.get("default_model") else None),
            warm=bool(ollama_raw.get("warm", False)),
            keep_alive=str(ollama_raw.get("keep_alive", "24h")),
        )

    return AppManifest(
        name=name, start=start, install=install,
        env=env, health=health, restart=restart, ollama=ollama,
    )
