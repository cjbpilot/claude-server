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
          "mode": "active" | "maintenance",
          "manifest": {...},          # AppManifest.to_dict()
          "registered_at": 1714123456,
          "updated_at": 1714123456,
          "last_known_sha": "bbe66f3...",  # set by git_pull
          "auto_update": {            # optional; absent = disabled
            "enabled": false,
            "at_utc": "05:00",        # HH:MM in UTC
            "window_minutes": 60,     # don't fire if we wake later than this
            "skip_if_maintenance": true,
            "rollback_on_health_fail": true,
            "notify_telegram": true,
            "last_run_at": 0,         # unix; reset to 0 on each new schedule
            "last_result": null,      # see VALID_AUTO_UPDATE_RESULTS
            "last_message": null,
            "last_prev_sha": null,
            "last_new_sha": null
          }
        }
      }
    }

`mode` is orthogonal to `desired_state`:
- "active" (default): supervisor enforces desired_state, restarts on crash,
   health-probes, cycles on git_pull.
- "maintenance": supervisor leaves the app entirely alone — no spawn, no
   terminate, no health probes, no pull-driven cycling. Used while the
   operator is hand-building / hand-restarting the app.
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


VALID_MODES = ("active", "maintenance")

VALID_AUTO_UPDATE_RESULTS = (
    "ok",
    "no_change",
    "pull_failed",
    "build_failed",
    "health_failed",
    "rolled_back",
    "skipped_maintenance",
)

VALID_PRODUCT_OWNER_RESULTS = (
    "ok",
    "no_specs",
    "no_api_key",
    "llm_failed",
    "git_failed",
    "telegram_failed",
    "skipped_maintenance",
)


def default_auto_update_config() -> dict:
    """Sensible defaults applied when an operator first turns auto-update on
    without supplying every field."""
    return {
        "enabled": False,
        "at_utc": "05:00",
        "window_minutes": 60,
        "skip_if_maintenance": True,
        "rollback_on_health_fail": True,
        "notify_telegram": True,
        "last_run_at": 0,
        "last_result": None,
        "last_message": None,
        "last_prev_sha": None,
        "last_new_sha": None,
    }


def default_product_owner_config() -> dict:
    """Defaults for the product-owner review cadence. day_of_week is 0=Mon
    through 6=Sun, matching Python's time.gmtime().tm_wday. -1 means daily.
    feedback_window_hours is how long the agent waits for Telegram replies
    after posting its 'anything you want added?' question; 0 skips Telegram
    entirely and goes straight to web+code synthesis."""
    return {
        "enabled": False,
        "day_of_week": 0,           # Monday
        "at_utc": "10:00",          # 11:00 BST / 06:00 ET / 03:00 PT
        "feedback_window_hours": 6,
        "max_specs": 3,
        "skip_if_maintenance": True,
        "notify_telegram": True,
        "last_run_at": 0,
        "last_result": None,
        "last_message": None,
        "last_branch": None,
        "last_spec_count": 0,
    }


@dataclass
class AppRecord:
    name: str
    repo_name: str
    desired_state: str  # "running" | "stopped"
    mode: str = "active"  # "active" | "maintenance"
    manifest: dict = field(default_factory=dict)
    registered_at: int = 0
    updated_at: int = 0
    last_known_sha: Optional[str] = None
    auto_update: dict = field(default_factory=dict)
    product_owner: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "repo_name": self.repo_name,
            "desired_state": self.desired_state,
            "mode": self.mode,
            "manifest": self.manifest,
            "registered_at": self.registered_at,
            "updated_at": self.updated_at,
            "last_known_sha": self.last_known_sha,
            "auto_update": dict(self.auto_update),
            "product_owner": dict(self.product_owner),
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


def _coerce_mode(value) -> str:
    return value if value in VALID_MODES else "active"


def list_records() -> list[AppRecord]:
    obj = _load_raw()
    out = []
    for name, e in obj.get("apps", {}).items():
        out.append(AppRecord(
            name=name,
            repo_name=str(e.get("repo_name", "")),
            desired_state=str(e.get("desired_state", "stopped")),
            mode=_coerce_mode(e.get("mode")),
            manifest=dict(e.get("manifest", {})),
            registered_at=int(e.get("registered_at", 0)),
            updated_at=int(e.get("updated_at", 0)),
            last_known_sha=e.get("last_known_sha"),
            auto_update=dict(e.get("auto_update") or {}),
            product_owner=dict(e.get("product_owner") or {}),
        ))
    return out


def get(name: str) -> Optional[AppRecord]:
    for r in list_records():
        if r.name == name:
            return r
    return None


def upsert(name: str, repo_name: str, manifest: dict,
           desired_state: str = "running",
           mode: Optional[str] = None) -> AppRecord:
    """Upsert a record. `mode` defaults to the existing value (or "active"
    for new records); pass an explicit string to override.

    Preserves the auto_update sub-document across re-registers (otherwise an
    operator who registered an app then turned on auto-update would lose the
    setting on every git_pull's manifest re-load — register/upsert is called
    by both code paths).
    """
    obj = _load_raw()
    apps = obj.setdefault("apps", {})
    now = int(time.time())
    existing = apps.get(name, {})
    effective_mode = _coerce_mode(mode if mode is not None else existing.get("mode"))
    rec = {
        "repo_name": repo_name,
        "desired_state": desired_state,
        "mode": effective_mode,
        "manifest": manifest,
        "registered_at": int(existing.get("registered_at", now)),
        "updated_at": now,
        "last_known_sha": existing.get("last_known_sha"),
        "auto_update": dict(existing.get("auto_update") or {}),
        "product_owner": dict(existing.get("product_owner") or {}),
    }
    apps[name] = rec
    _save_raw(obj)
    return AppRecord(
        name=name, repo_name=rec["repo_name"], desired_state=rec["desired_state"],
        mode=rec["mode"], manifest=rec["manifest"],
        registered_at=rec["registered_at"], updated_at=rec["updated_at"],
        last_known_sha=rec["last_known_sha"],
        auto_update=rec["auto_update"],
        product_owner=rec["product_owner"],
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


def set_mode(name: str, mode: str) -> Optional[AppRecord]:
    if mode not in VALID_MODES:
        raise ValueError(f"invalid mode {mode!r}; expected one of {VALID_MODES}")
    obj = _load_raw()
    apps = obj.setdefault("apps", {})
    if name not in apps:
        return None
    apps[name]["mode"] = mode
    apps[name]["updated_at"] = int(time.time())
    _save_raw(obj)
    return get(name)


_AUTO_UPDATE_ALLOWED_KEYS = {
    "enabled", "at_utc", "window_minutes",
    "skip_if_maintenance", "rollback_on_health_fail", "notify_telegram",
}


def set_auto_update(name: str, partial: dict) -> Optional[AppRecord]:
    """Merge `partial` into the app's auto_update sub-document and persist.

    Only the operator-controlled fields can be set this way (last_run_at /
    last_result / last_*_sha are written by record_auto_update_run as the
    supervisor runs cycles). Unknown keys are silently dropped. Returns the
    updated record, or None if the app is unknown.
    """
    obj = _load_raw()
    apps = obj.setdefault("apps", {})
    if name not in apps:
        return None
    existing = dict(apps[name].get("auto_update") or {})
    if not existing:
        existing = default_auto_update_config()
    for k, v in partial.items():
        if k in _AUTO_UPDATE_ALLOWED_KEYS:
            existing[k] = v
    # If the operator changed the time, clear last_run_at so today's new slot
    # is eligible to fire (otherwise yesterday's run blocks a freshly-edited
    # earlier-in-the-day schedule).
    if "at_utc" in partial:
        existing["last_run_at"] = 0
    apps[name]["auto_update"] = existing
    apps[name]["updated_at"] = int(time.time())
    _save_raw(obj)
    return get(name)


def record_auto_update_run(
    name: str,
    result: str,
    *,
    message: Optional[str] = None,
    prev_sha: Optional[str] = None,
    new_sha: Optional[str] = None,
    ran_at: Optional[int] = None,
) -> Optional[AppRecord]:
    """Record the outcome of an auto-update attempt. The supervisor calls
    this every time the tick fires an update, regardless of outcome — that
    way the rest of the day's ticks correctly see "already ran today"."""
    if result not in VALID_AUTO_UPDATE_RESULTS:
        raise ValueError(
            f"invalid auto-update result {result!r}; "
            f"expected one of {VALID_AUTO_UPDATE_RESULTS}"
        )
    obj = _load_raw()
    apps = obj.setdefault("apps", {})
    if name not in apps:
        return None
    existing = dict(apps[name].get("auto_update") or {})
    if not existing:
        existing = default_auto_update_config()
    existing["last_run_at"] = int(ran_at if ran_at is not None else time.time())
    existing["last_result"] = result
    existing["last_message"] = message
    existing["last_prev_sha"] = prev_sha
    existing["last_new_sha"] = new_sha
    apps[name]["auto_update"] = existing
    apps[name]["updated_at"] = int(time.time())
    _save_raw(obj)
    return get(name)


_PRODUCT_OWNER_ALLOWED_KEYS = {
    "enabled", "day_of_week", "at_utc", "feedback_window_hours",
    "max_specs", "skip_if_maintenance", "notify_telegram",
}


def set_product_owner(name: str, partial: dict) -> Optional[AppRecord]:
    """Merge operator-supplied product-owner config into the persisted
    sub-document. Same shape as set_auto_update."""
    obj = _load_raw()
    apps = obj.setdefault("apps", {})
    if name not in apps:
        return None
    existing = dict(apps[name].get("product_owner") or {})
    if not existing:
        existing = default_product_owner_config()
    for k, v in partial.items():
        if k in _PRODUCT_OWNER_ALLOWED_KEYS:
            existing[k] = v
    if "at_utc" in partial or "day_of_week" in partial:
        # Reset last-run so the new slot is eligible the same day if it has
        # not already passed — same logic as auto-update.
        existing["last_run_at"] = 0
    apps[name]["product_owner"] = existing
    apps[name]["updated_at"] = int(time.time())
    _save_raw(obj)
    return get(name)


def record_product_owner_run(
    name: str,
    result: str,
    *,
    message: Optional[str] = None,
    branch: Optional[str] = None,
    spec_count: int = 0,
    ran_at: Optional[int] = None,
) -> Optional[AppRecord]:
    if result not in VALID_PRODUCT_OWNER_RESULTS:
        raise ValueError(
            f"invalid product-owner result {result!r}; "
            f"expected one of {VALID_PRODUCT_OWNER_RESULTS}"
        )
    obj = _load_raw()
    apps = obj.setdefault("apps", {})
    if name not in apps:
        return None
    existing = dict(apps[name].get("product_owner") or {})
    if not existing:
        existing = default_product_owner_config()
    existing["last_run_at"] = int(ran_at if ran_at is not None else time.time())
    existing["last_result"] = result
    existing["last_message"] = message
    existing["last_branch"] = branch
    existing["last_spec_count"] = int(spec_count)
    apps[name]["product_owner"] = existing
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
