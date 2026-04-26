"""Persistent registry of repos the agent is authorised to pull from.

State lives at C:\\ProgramData\\ClaudeAgent\\repos.json with an ACL of
SYSTEM:F + Administrators:F. Each entry stores the URL, branch, and an
optional auth token. Tokens never leave this file (or the spawned git
process's environment) - never logged, never echoed back over NATS.

Schema:

    {
      "version": 1,
      "repos": {
        "<name>": {
          "url": "https://github.com/...",
          "branch": "main",
          "token": "ghp_..." | null,
          "added_at": 1714123456,
          "updated_at": 1714123456
        }
      }
    }
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

STORE_PATH = Path(r"C:\ProgramData\ClaudeAgent\repos.json")


@dataclass
class RepoEntry:
    name: str
    url: str
    branch: str
    token: Optional[str]
    added_at: int = 0
    updated_at: int = 0

    def to_safe(self) -> dict:
        """Public-facing view: never includes the token."""
        return {
            "name": self.name,
            "url": self.url,
            "branch": self.branch,
            "has_token": bool(self.token),
            "added_at": self.added_at,
            "updated_at": self.updated_at,
        }


def _lock_acl(path: Path) -> None:
    if sys.platform != "win32":
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant",
             "SYSTEM:F", "Administrators:F"],
            check=False, capture_output=True,
        )
    except Exception:
        pass


def _load_raw() -> dict:
    if not STORE_PATH.exists():
        return {"version": 1, "repos": {}}
    try:
        data = STORE_PATH.read_bytes()
        if data.startswith(b"\xef\xbb\xbf"):
            data = data[3:]
        obj = json.loads(data.decode("utf-8") or "{}")
        obj.setdefault("version", 1)
        obj.setdefault("repos", {})
        return obj
    except Exception:
        # Corrupted file: don't blow up the agent.
        return {"version": 1, "repos": {}}


def _save_raw(obj: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    os.replace(tmp, STORE_PATH)
    _lock_acl(STORE_PATH)


def list_entries() -> list[RepoEntry]:
    obj = _load_raw()
    out = []
    for name, e in obj.get("repos", {}).items():
        out.append(RepoEntry(
            name=name,
            url=e.get("url", ""),
            branch=e.get("branch", "main"),
            token=e.get("token"),
            added_at=int(e.get("added_at", 0)),
            updated_at=int(e.get("updated_at", 0)),
        ))
    return out


def get(name: str) -> Optional[RepoEntry]:
    for e in list_entries():
        if e.name == name:
            return e
    return None


def upsert(name: str, url: str, branch: str, token: Optional[str]) -> RepoEntry:
    obj = _load_raw()
    repos = obj.setdefault("repos", {})
    now = int(time.time())
    existing = repos.get(name, {})
    entry = {
        "url": url,
        "branch": branch or "main",
        "token": token,
        "added_at": int(existing.get("added_at", now)),
        "updated_at": now,
    }
    repos[name] = entry
    _save_raw(obj)
    return RepoEntry(
        name=name,
        url=entry["url"],
        branch=entry["branch"],
        token=entry["token"],
        added_at=entry["added_at"],
        updated_at=entry["updated_at"],
    )


def set_token(name: str, token: Optional[str]) -> Optional[RepoEntry]:
    obj = _load_raw()
    repos = obj.setdefault("repos", {})
    if name not in repos:
        return None
    repos[name]["token"] = token
    repos[name]["updated_at"] = int(time.time())
    _save_raw(obj)
    e = repos[name]
    return RepoEntry(
        name=name, url=e["url"], branch=e["branch"], token=e["token"],
        added_at=int(e["added_at"]), updated_at=int(e["updated_at"]),
    )


def delete(name: str) -> bool:
    obj = _load_raw()
    repos = obj.setdefault("repos", {})
    if name in repos:
        del repos[name]
        _save_raw(obj)
        return True
    return False
