"""Persistent registry of apps the agent supervises.

Same security model as repo_store: SYSTEM:F + Administrators:F at
C:\\ProgramData\\ClaudeAgent\\apps.json.

Schema:

    {
      "version": 1,
      "apps": {
        "<app_name>": {
          "repo_name": "stronghold",
          "desired_state": "running" | "stopped",
          "manifest": {...},          # AppManifest.to_dict()
          "registered_at": 1714123456,
          "updated_at": 1714123456,
          "last_known_sha": "bbe66f3..."  # set by git_pull
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

STORE_PATH = Path(r"C:\ProgramData\ClaudeAgent\apps.json")


@dataclass
class AppRecord:
    name: str
    repo_name: str
    desired_state: str  # "running" | "stopped"
    manifest: dict = field(default_factory=dict)
    registered_at: int = 0
    updated_at: int = 0
    last_known_sha: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "repo_name": self.repo_name,
            "desired_state": self.desired_state,
            "manifest": self.manifest,
            "registered_at": self.registered_at,
            "updated_at": self.updated_at,
            "last_known_sha": self.last_known_sha,
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
        return {"version": 1, "apps": {}}
    try:
        data = STORE_PATH.read_bytes()
        if data.startswith(b"\xef\xbb\xbf"):
            data = data[3:]
        obj = json.loads(data.decode("utf-8") or "{}")
        obj.setdefault("version", 1)
        obj.setdefault("apps", {})
        return obj
    except Exception:
        return {"version": 1, "apps": {}}


def _save_raw(obj: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    os.replace(tmp, STORE_PATH)
    _lock_acl(STORE_PATH)


def list_records() -> list[AppRecord]:
    obj = _load_raw()
    out = []
    for name, e in obj.get("apps", {}).items():
        out.append(AppRecord(
            name=name,
            repo_name=str(e.get("repo_name", "")),
            desired_state=str(e.get("desired_state", "stopped")),
            manifest=dict(e.get("manifest", {})),
            registered_at=int(e.get("registered_at", 0)),
            updated_at=int(e.get("updated_at", 0)),
            last_known_sha=e.get("last_known_sha"),
        ))
    return out


def get(name: str) -> Optional[AppRecord]:
    for r in list_records():
        if r.name == name:
            return r
    return None


def upsert(name: str, repo_name: str, manifest: dict,
           desired_state: str = "running") -> AppRecord:
    obj = _load_raw()
    apps = obj.setdefault("apps", {})
    now = int(time.time())
    existing = apps.get(name, {})
    rec = {
        "repo_name": repo_name,
        "desired_state": desired_state,
        "manifest": manifest,
        "registered_at": int(existing.get("registered_at", now)),
        "updated_at": now,
        "last_known_sha": existing.get("last_known_sha"),
    }
    apps[name] = rec
    _save_raw(obj)
    return AppRecord(
        name=name, repo_name=rec["repo_name"], desired_state=rec["desired_state"],
        manifest=rec["manifest"], registered_at=rec["registered_at"],
        updated_at=rec["updated_at"], last_known_sha=rec["last_known_sha"],
    )


def set_desired(name: str, desired_state: str) -> Optional[AppRecord]:
    obj = _load_raw()
    apps = obj.setdefault("apps", {})
    if name not in apps:
        return None
    apps[name]["desired_state"] = desired_state
    apps[name]["updated_at"] = int(time.time())
    _save_raw(obj)
    return get(name)


def set_last_sha(name: str, sha: str) -> None:
    obj = _load_raw()
    apps = obj.setdefault("apps", {})
    if name in apps:
        apps[name]["last_known_sha"] = sha
        apps[name]["updated_at"] = int(time.time())
        _save_raw(obj)


def delete(name: str) -> bool:
    obj = _load_raw()
    apps = obj.setdefault("apps", {})
    if name in apps:
        del apps[name]
        _save_raw(obj)
        return True
    return False


def for_repo(repo_name: str) -> list[AppRecord]:
    return [r for r in list_records() if r.repo_name == repo_name]
