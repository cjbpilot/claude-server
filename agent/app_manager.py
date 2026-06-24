"""Process supervisor for apps registered via app_store.

Lifecycle (high-level):

  - On agent boot, load the registry. Every app whose desired_state is
    'running' is launched.
  - A supervisor task ticks every 5s:
      * For each managed app expected to be running, if its child process
        has exited and the restart budget allows, relaunch it.
      * If the manifest has [health], probe the URL on its configured
        interval and record the result.
  - On agent shutdown, all child processes are terminated.

Per-app stdout/stderr go to C:\\ProgramData\\ClaudeAgent\\logs\\app-<name>.log
in append mode, with a naive 10 MB rotation to .1.

Token redaction: app start commands and env values may contain secrets the
user supplied in their manifest. We do not log env values, and we do not
log the start command after launch — only the spawn event ("started <name>
pid=N").
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from agent import app_manifest, app_store

log = logging.getLogger("agent.apps")

LOG_DIR = Path(r"C:\ProgramData\ClaudeAgent\logs")
LOG_ROTATE_BYTES = 10 * 1024 * 1024
SUPERVISOR_INTERVAL_S = 5
# How long (s) to wait for the app's /health endpoint to come back green
# after an auto-update cycle before considering it a failure and rolling
# back. Spring Boot + Vaadin can take ~25-40s to start; 90s gives headroom.
_AUTO_UPDATE_HEALTH_WAIT_S = 90
# Where rollback artifacts live. MUST be outside the repo's build-output
# tree because the build command (`mvn clean`, `dotnet clean`, etc.)
# wipes that whole tree as its first step — any in-tree stash would be
# deleted before the build even fails, leaving rollback with no JAR to
# restore. Each app gets its own subdirectory: <STASH>/<name>/<artifact>.
_AUTO_UPDATE_STASH_DIR = Path(r"C:\ProgramData\ClaudeAgent\stash")

# After we kill the previous app process on Windows, the OS may need a beat
# to flush file handles. Until then, build commands that touch the same
# files (mvn clean over target/*.jar, dotnet build over bin/*.dll, etc.)
# will fail with "Failed to delete ...". We poll for write-openability on
# these artifact paths instead of guessing how long to sleep.
_LOCK_PROBE_EXTS = (".jar", ".dll", ".exe", ".pyd", ".so")
_LOCK_PROBE_DIRS = ("target", "bin", "build", "dist", "out")
_LOCK_WAIT_MAX_S = 30.0


class MaintenanceModeError(Exception):
    """Raised when an operator-only action is attempted on an app the
    supervisor is currently leaving alone."""


def _is_file_locked(path: Path) -> bool:
    """Return True iff something else has `path` open with sharing that
    blocks a write-open. On POSIX this is effectively always False (open
    files are deletable), which matches the production behaviour we care
    about — the race only bites on Windows. The function is still safe to
    call cross-platform so callers don't need a guard.
    """
    if sys.platform != "win32":
        return False
    try:
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        fd = os.open(str(path), flags)
    except FileNotFoundError:
        return False
    except (PermissionError, OSError):
        return True
    os.close(fd)
    return False


def _collect_probe_artifacts(cwd: Path) -> list[Path]:
    """Find candidate build artifacts under standard output dirs of `cwd`.

    Bounded scan: only the top-level files in each known dir, no recursion,
    no symlink follow. We're looking for the file mvn/dotnet/etc. is about
    to delete, not for an exhaustive inventory.
    """
    out: list[Path] = []
    for sub in _LOCK_PROBE_DIRS:
        d = cwd / sub
        if not d.is_dir():
            continue
        try:
            for entry in d.iterdir():
                if entry.is_file() and entry.suffix.lower() in _LOCK_PROBE_EXTS:
                    out.append(entry)
        except OSError:
            continue
    return out


def _artifact_mtimes(cwd: Path) -> dict[str, float]:
    """Snapshot {path: mtime} for candidate build artifacts under cwd."""
    out: dict[str, float] = {}
    for p in _collect_probe_artifacts(cwd):
        try:
            out[str(p)] = p.stat().st_mtime
        except OSError:
            continue
    return out


def _any_artifact_advanced(cwd: Path, baseline: dict[str, float]) -> bool:
    """True if any current artifact is newer than its baseline, or if a
    new artifact appeared that wasn't in the baseline. Used to catch the
    case where install rc=0 but the build produced nothing fresh — meaning
    a relaunch would still pick up the previous commit's binary.
    """
    after = _artifact_mtimes(cwd)
    if not baseline and not after:
        # Nothing to compare and nothing produced. Build commands that
        # don't emit jar/dll/exe (pure pip install, npm install) hit this
        # branch — defer to install rc as the source of truth.
        return True
    for path, mtime in after.items():
        prev = baseline.get(path)
        if prev is None or mtime > prev + 0.001:
            return True
    return False


async def _wait_for_artifacts_unlocked(
    cwd: Path,
    timeout_s: float = _LOCK_WAIT_MAX_S,
    *,
    sleeper=None,
    now=None,
) -> tuple[bool, list[str]]:
    """Poll the standard build-output dirs until no artifact is held open.

    Returns ``(ok, still_locked)``. ``ok`` is True if every probed file
    became write-openable (or no probes existed); False on timeout. The
    `sleeper`/`now` parameters exist so tests can drive the loop without
    real wall time — production calls leave them as None.
    """
    if sys.platform != "win32":
        return True, []
    sleeper = sleeper or asyncio.sleep
    now = now or time.monotonic
    deadline = now() + timeout_s
    delay = 0.2
    while True:
        artifacts = _collect_probe_artifacts(cwd)
        still_locked = [str(p) for p in artifacts if _is_file_locked(p)]
        if not still_locked:
            return True, []
        if now() >= deadline:
            return False, still_locked
        await sleeper(delay)
        delay = min(delay * 1.5, 2.0)


@dataclass
class _Runtime:
    """In-process state for a managed app. Not persisted."""
    record: app_store.AppRecord
    manifest: app_manifest.AppManifest
    proc: Optional[asyncio.subprocess.Process] = None
    log_fh: Optional[object] = None
    started_at: Optional[float] = None
    last_exit_code: Optional[int] = None
    last_health: Optional[str] = None  # "ok" | "fail" | None
    last_health_at: Optional[float] = None
    next_health_at: float = 0.0
    restart_history: deque = field(default_factory=lambda: deque(maxlen=64))
    next_restart_at: float = 0.0
    # "ok", "failed", "rolled_back", "skipped", or None if no install has
    # run yet. "rolled_back" means a build failed (or produced no fresh
    # artifact) and the supervisor chose NOT to relaunch the stale binary.
    install_status: Optional[str] = None
    install_status_at: Optional[float] = None
    install_message: Optional[str] = None

    @property
    def mode(self) -> str:
        return self.record.mode

    @property
    def in_maintenance(self) -> bool:
        return self.record.mode == "maintenance"


class AppManager:
    def __init__(self, cfg, runner):
        self.cfg = cfg
        self.runner = runner
        self._apps: dict[str, _Runtime] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._stop_evt = asyncio.Event()
        self._http: Optional[httpx.AsyncClient] = None

    # ---------- public API ----------

    async def start(self) -> None:
        """Boot the supervisor: load registry, launch desired-running apps."""
        if self._running:
            return
        self._running = True
        self._http = httpx.AsyncClient(timeout=10)
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        for rec in app_store.list_records():
            try:
                manifest = app_manifest.manifest_from_dict(rec.manifest, rec.name)
            except Exception:
                log.exception("invalid stored manifest for %s; skipping", rec.name)
                continue
            self._apps[rec.name] = _Runtime(record=rec, manifest=manifest)
            if rec.mode == "maintenance":
                # Parked by the operator. Don't relaunch on boot (e.g. after
                # self_update); they'll flip mode back to 'active' when ready.
                log.info("app %s loaded in maintenance mode; not launching", rec.name)
                continue
            if rec.desired_state == "running":
                await self._launch(rec.name)

        # Run the supervisor loop as a background task tied to the runner.
        self.runner.spawn(self._supervisor_loop())

    async def stop(self) -> None:
        """Stop the supervisor and kill every managed child process."""
        self._stop_evt.set()
        self._running = False
        for name in list(self._apps.keys()):
            await self._terminate(name)
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass

    async def register(self, repo_name: str, name: Optional[str] = None) -> dict:
        """Read the manifest from the repo dir, store it, install, and start.

        Returns a status dict describing what happened. Raises ValueError on
        manifest problems so the handler can surface them.
        """
        repo_dir = self._repo_dir(repo_name)
        if not repo_dir.exists() or not (repo_dir / ".git").exists():
            raise ValueError(
                f"repo '{repo_name}' is not checked out at {repo_dir} - "
                f"git_pull it first"
            )

        manifest = app_manifest.parse_manifest(repo_dir, default_name=name or repo_name)
        if name and manifest.name != name:
            # User supplied a name override.
            manifest.name = name

        rec = app_store.upsert(
            name=manifest.name,
            repo_name=repo_name,
            manifest=manifest.to_dict(),
            desired_state="running",
        )
        rt = _Runtime(record=rec, manifest=manifest)
        async with self._lock:
            existing = self._apps.get(manifest.name)
            if existing and existing.proc is not None:
                await self._terminate_locked(manifest.name)
            self._apps[manifest.name] = rt

        install_result = "skipped"
        if manifest.install is not None:
            rc = await self._run_install(rt)
            install_result = (
                rt.install_status if rc == 0 else f"{rt.install_status} rc={rc}"
            )
            if rc != 0:
                # Don't auto-start a broken install.
                app_store.set_desired(manifest.name, "stopped")
                rt.record = app_store.get(manifest.name) or rec
                return {
                    "name": manifest.name, "repo": repo_name,
                    "install": install_result, "started": False,
                }
        else:
            self._set_install_status(rt, "skipped")

        ollama_result = "skipped"
        if manifest.ollama is not None:
            ok, ollama_result = await self._ensure_ollama(rt)
            if not ok:
                app_store.set_desired(manifest.name, "stopped")
                rt.record = app_store.get(manifest.name) or rec
                return {
                    "name": manifest.name, "repo": repo_name,
                    "install": install_result, "ollama": ollama_result,
                    "started": False,
                }

        await self._launch(manifest.name)
        return {
            "name": manifest.name, "repo": repo_name,
            "install": install_result, "ollama": ollama_result,
            "started": True,
        }

    async def unregister(self, name: str) -> bool:
        async with self._lock:
            await self._terminate_locked(name)
            self._apps.pop(name, None)
        return app_store.delete(name)

    async def start_app(self, name: str) -> bool:
        rt = self._apps.get(name)
        if rt is None:
            return False
        if rt.in_maintenance:
            raise MaintenanceModeError(
                f"app {name!r} is in maintenance mode — "
                f"call set_app_mode active first"
            )
        app_store.set_desired(name, "running")
        rt.record = app_store.get(name) or rt.record
        await self._launch(name)
        return True

    async def stop_app(self, name: str) -> bool:
        rt = self._apps.get(name)
        if rt is None:
            return False
        if rt.in_maintenance:
            raise MaintenanceModeError(
                f"app {name!r} is in maintenance mode — "
                f"call set_app_mode active first"
            )
        app_store.set_desired(name, "stopped")
        rt.record = app_store.get(name) or rt.record
        await self._terminate(name)
        return True

    async def restart_app(self, name: str) -> bool:
        rt = self._apps.get(name)
        if rt is None:
            return False
        if rt.in_maintenance:
            raise MaintenanceModeError(
                f"app {name!r} is in maintenance mode — "
                f"call set_app_mode active first"
            )
        await self._terminate(name)
        await asyncio.sleep(0.5)
        await self._launch(name)
        return True

    async def set_mode(self, name: str, mode: str) -> Optional[app_store.AppRecord]:
        """Flip an app between 'active' and 'maintenance' and persist it.

        Does not start, stop, or touch the live process — flipping back to
        'active' means the supervisor resumes its normal duties (health
        probes, crash restarts honoring desired_state) on the next tick.
        Returns None if the app is unknown; raises ValueError for a bad mode.
        """
        if mode not in app_store.VALID_MODES:
            raise ValueError(
                f"invalid mode {mode!r}; expected one of {app_store.VALID_MODES}"
            )
        rt = self._apps.get(name)
        if rt is None:
            return None
        rec = app_store.set_mode(name, mode)
        if rec is not None:
            rt.record = rec
            # A fresh return to 'active' should not be penalized by stale
            # crash backoff. The supervisor will re-evaluate everything
            # from scratch on the next tick.
            if mode == "active":
                rt.next_restart_at = 0.0
                rt.next_health_at = time.time()
        log.info("app %s mode -> %s", name, mode)
        return rec

    def list_apps(self) -> list[dict]:
        out = []
        now = time.time()
        for name, rt in self._apps.items():
            alive = rt.proc is not None and rt.proc.returncode is None
            uptime = (time.time() - rt.started_at) if (alive and rt.started_at) else None
            au = dict(rt.record.auto_update or {})
            au["next_run_at"] = self._auto_update_next_run_ts(rt, now)
            po_field = dict(rt.record.product_owner or {})
            po_field["next_run_at"] = self._product_owner_next_run_ts(rt, now)
            out.append({
                "name": name,
                "repo": rt.record.repo_name,
                "desired": rt.record.desired_state,
                "mode": rt.record.mode,
                "alive": alive,
                "pid": rt.proc.pid if rt.proc is not None else None,
                "uptime_s": round(uptime, 1) if uptime is not None else None,
                "last_exit_code": rt.last_exit_code,
                "last_health": rt.last_health,
                "last_health_at": rt.last_health_at,
                "restart_count_recent": len(self._recent_restarts(rt)),
                "last_known_sha": rt.record.last_known_sha,
                "log_path": str(self._log_path(name)),
                "install_status": rt.install_status,
                "install_status_at": rt.install_status_at,
                "install_message": rt.install_message,
                "auto_update": au,
                "product_owner": po_field,
            })
        return out

    def _product_owner_next_run_ts(self, rt: _Runtime, now: float) -> Optional[int]:
        """Compute the next product-owner run time. Honors day_of_week:
        -1 means daily; 0-6 means a specific weekday. Returns today's
        slot if not yet past, otherwise advances to the next eligible
        weekday."""
        cfg = rt.record.product_owner or {}
        if not cfg.get("enabled"):
            return None
        today_ts = self._today_scheduled_ts(cfg.get("at_utc", "10:00"), now)
        if today_ts is None:
            return None
        dow = int(cfg.get("day_of_week", 0))
        last_run = int(cfg.get("last_run_at") or 0)
        window_s = 60 * 60

        def _next_eligible(start_ts: int) -> int:
            ts = start_ts
            for _ in range(8):
                wd = time.gmtime(ts).tm_wday
                if dow < 0 or wd == dow:
                    return ts
                ts += 86400
            return ts

        if last_run >= today_ts or now > today_ts + window_s:
            return _next_eligible(today_ts + 86400)
        return _next_eligible(today_ts)

    def _auto_update_next_run_ts(self, rt: _Runtime, now: float) -> Optional[int]:
        """When will the next auto-update fire? None if disabled or
        misconfigured. Returns today's slot if not yet past, otherwise
        tomorrow's slot."""
        cfg = rt.record.auto_update or {}
        if not cfg.get("enabled"):
            return None
        today_ts = self._today_scheduled_ts(cfg.get("at_utc", "05:00"), now)
        if today_ts is None:
            return None
        window_s = max(1, int(cfg.get("window_minutes", 60))) * 60
        last_run = int(cfg.get("last_run_at") or 0)
        # If today's slot has already fired (or we're past the window),
        # next-eligible is tomorrow's slot.
        if last_run >= today_ts or now > today_ts + window_s:
            return today_ts + 86400
        return today_ts

    async def notify_pull(self, repo_name: str, new_sha: str) -> list[str]:
        """Called by git_ops after a successful pull. Restarts apps that
        live in this repo iff their SHA changed and on_update is true.

        Returns the list of app names that were cycled.
        """
        def _looks_like_sha(s) -> bool:
            return (
                isinstance(s, str)
                and 7 <= len(s) <= 64
                and all(c in "0123456789abcdef" for c in s.lower())
            )

        cycled = []
        for rt in list(self._apps.values()):
            if rt.record.repo_name != repo_name:
                continue
            known = rt.record.last_known_sha
            # Only skip if we have a real SHA on record AND it matches.
            # Anything else (None, "HEAD", empty) means we don't trust the
            # record - always cycle.
            if _looks_like_sha(known) and known == new_sha:
                continue
            # Record the new workspace SHA even if we won't cycle: the files
            # on disk really are at this SHA now, so list_apps should reflect
            # that. Maintenance mode just prevents the process cycle.
            app_store.set_last_sha(rt.record.name, new_sha)
            rt.record = app_store.get(rt.record.name) or rt.record
            if rt.in_maintenance:
                log.info(
                    "app %s in maintenance mode; pull recorded but not cycled",
                    rt.record.name,
                )
                continue
            if not (rt.manifest.restart.on_update and rt.record.desired_state == "running"):
                continue
            await self._cycle_after_pull(rt, new_sha)
            cycled.append(rt.record.name)
        return cycled

    async def _cycle_after_pull(self, rt: _Runtime, new_sha: str) -> None:
        """Restart a managed app after its repo pulled to a new SHA.

        Re-reads the manifest from the workspace repo so env, install, and
        ollama changes in the new commit take effect this cycle (the cached
        manifest from register_app would otherwise inject the previous
        commit's values). Holds self._lock through the whole sequence so the
        supervisor can't slip in and relaunch the old binary in the gap
        between terminate and the fresh launch.
        """
        name = rt.record.name
        repo_dir = self._repo_dir(rt.record.repo_name)
        log_path = self._log_path(name)

        def _log(line: str) -> None:
            try:
                with log_path.open("ab") as f:
                    f.write((line + "\n").encode("utf-8"))
            except Exception:
                pass

        async with self._lock:
            try:
                new_manifest = app_manifest.parse_manifest(repo_dir, default_name=name)
                # Keep the registered name; a manifest rename in a later
                # commit shouldn't silently rebrand an app under us.
                new_manifest.name = name
                rt.manifest = new_manifest
                app_store.upsert(
                    name=name,
                    repo_name=rt.record.repo_name,
                    manifest=new_manifest.to_dict(),
                    desired_state=rt.record.desired_state,
                )
                rt.record = app_store.get(name) or rt.record
                _log(f"--- manifest re-loaded after pull sha={new_sha[:12]} ---")
            except Exception as e:
                _log(f"--- manifest re-load failed, keeping cached: {e!r} ---")
                log.exception("manifest re-load failed for %s", name)

            if rt.manifest.install is not None:
                # _run_install terminates the live proc and waits for OS
                # handle release before building, so no terminate needed here.
                rc = await self._run_install(rt)
                if rc != 0:
                    # The build failed (or produced no fresh artifact). If we
                    # relaunched now, we'd spawn whatever's already on disk —
                    # i.e. the previous commit's binary — and the operator
                    # would believe the pull deployed. Keep the app down so
                    # the failure is visible in list_apps (install_status)
                    # and tail_logs.
                    _log(
                        f"--- install rc={rc} status={rt.install_status}; "
                        f"NOT launching, app stays down at sha "
                        f"{new_sha[:12]} ---"
                    )
                    log.error(
                        "app %s: install failed (status=%s rc=%s); refusing "
                        "to relaunch stale binary",
                        name, rt.install_status, rc,
                    )
                    return
            else:
                await self._terminate_locked(name)
                # No install step → no build artifact to verify. Mark as
                # skipped so list_apps shows we honored the cycle.
                self._set_install_status(rt, "skipped")

            if rt.manifest.ollama is not None:
                try:
                    await self._ensure_ollama(rt)
                except Exception:
                    log.exception("ollama re-warm failed for %s", name)

            await self._launch_locked(name)

    def tail_log(self, name: str, lines: int = 100) -> list[str]:
        path = self._log_path(name)
        if not path.exists():
            return []
        try:
            with path.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                read = min(size, max(lines * 200, 8192))
                f.seek(size - read)
                tail = f.read().decode("utf-8", errors="replace")
        except Exception:
            return []
        return tail.splitlines()[-lines:]

    # ---------- internals ----------

    def _repo_dir(self, repo_name: str) -> Path:
        return self.cfg.workspace_dir / repo_name

    def _log_path(self, name: str) -> Path:
        return LOG_DIR / f"app-{name}.log"

    def _recent_restarts(self, rt: _Runtime) -> list[float]:
        cutoff = time.time() - 3600
        rt.restart_history = deque(
            (t for t in rt.restart_history if t >= cutoff),
            maxlen=rt.restart_history.maxlen,
        )
        return list(rt.restart_history)

    def _rotate_log_if_needed(self, name: str) -> None:
        path = self._log_path(name)
        try:
            if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
                rotated = path.with_suffix(path.suffix + ".1")
                if rotated.exists():
                    rotated.unlink()
                path.rename(rotated)
        except Exception:
            pass

    async def _launch(self, name: str) -> None:
        async with self._lock:
            await self._launch_locked(name)

    async def _launch_locked(self, name: str) -> None:
        rt = self._apps.get(name)
        if rt is None:
            return
        if rt.proc is not None and rt.proc.returncode is None:
            return  # already running

        manifest = rt.manifest
        repo_dir = self._repo_dir(rt.record.repo_name)
        wd = manifest.start.working_dir
        cwd = Path(wd) if Path(wd).is_absolute() else (repo_dir / wd)

        if not cwd.exists():
            log.error("app %s working_dir does not exist: %s", name, cwd)
            return

        env = os.environ.copy()
        env.update(manifest.env)
        if manifest.ollama is not None:
            env[manifest.ollama.env_var] = manifest.ollama.host
            if manifest.ollama.default_model:
                env[manifest.ollama.model_env_var] = manifest.ollama.default_model

        self._rotate_log_if_needed(name)
        log_fh = open(self._log_path(name), "ab", buffering=0)
        log_fh.write(f"\n--- spawn {name} at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} cwd={cwd} ---\n".encode("utf-8"))

        try:
            proc = await asyncio.create_subprocess_shell(
                manifest.start.command,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
            )
        except Exception as e:
            log.exception("failed to spawn %s: %s", name, e)
            try:
                log_fh.write(f"--- spawn failed: {e!r} ---\n".encode("utf-8"))
            finally:
                log_fh.close()
            return

        rt.proc = proc
        rt.log_fh = log_fh
        rt.started_at = time.time()
        rt.last_exit_code = None
        rt.next_health_at = time.time() + (manifest.health.interval_s if manifest.health else 0)
        rt.restart_history.append(rt.started_at)
        log.info("started app %s pid=%s", name, proc.pid)

    async def _terminate(self, name: str) -> None:
        async with self._lock:
            await self._terminate_locked(name)

    async def _terminate_locked(self, name: str) -> None:
        rt = self._apps.get(name)
        if rt is None or rt.proc is None:
            return
        proc = rt.proc
        if proc.returncode is None:
            # Many app start commands shell out (mvnw.cmd, npm, dotnet, ...) and
            # spawn child java/node processes. proc.terminate kills only the
            # shell wrapper, leaving the actual app as an orphan that holds
            # file locks (e.g., H2 .mv.db) and breaks the next launch. On
            # Windows, taskkill /T walks the process tree and kills children.
            tree_killed = False
            if sys.platform == "win32":
                try:
                    tk = await asyncio.create_subprocess_exec(
                        "taskkill", "/T", "/F", "/PID", str(proc.pid),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(tk.wait(), timeout=10)
                    tree_killed = True
                except Exception:
                    log.exception("taskkill /T failed for %s pid=%s", name, proc.pid)
            if not tree_killed:
                try:
                    proc.terminate()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
        rt.last_exit_code = proc.returncode
        rt.proc = None
        if rt.log_fh is not None:
            try:
                rt.log_fh.close()
            except Exception:
                pass
            rt.log_fh = None
        rt.started_at = None
        log.info("terminated app %s exit=%s", name, rt.last_exit_code)

    def _set_install_status(
        self, rt: _Runtime, status: str, message: Optional[str] = None,
    ) -> None:
        rt.install_status = status
        rt.install_status_at = time.time()
        rt.install_message = message

    async def _run_install(self, rt: _Runtime) -> int:
        """Run the manifest's install command. Returns 0 only when the
        install command succeeded AND produced a fresh build artifact
        (when the build emits artifacts at all). Non-zero return means the
        caller MUST NOT spawn the app — the binary on disk is stale.

        Side effect: sets rt.install_status / install_status_at / install_message
        to one of ``ok``, ``failed``, ``rolled_back``, ``timeout`` (or leaves
        them untouched if no install spec is configured).
        """
        spec = rt.manifest.install
        if spec is None:
            return 0
        repo_dir = self._repo_dir(rt.record.repo_name)
        log_path = self._log_path(rt.record.name)

        def _log_line(line: str) -> None:
            try:
                with log_path.open("ab") as f:
                    f.write((line + "\n").encode("utf-8"))
            except Exception:
                pass

        # The previous process can hold the very files the build wants to
        # overwrite (target/*.jar on Maven, bin/*.dll on dotnet, etc.) and
        # on Windows the file handle survives the process exit by a beat.
        # mvn clean then fails with "Failed to delete ...", returns rc=1,
        # and we'd silently relaunch the stale jar. Stop the proc and
        # actively poll the candidate artifacts until the OS releases them.
        just_terminated = False
        if rt.proc is not None and rt.proc.returncode is None:
            await self._terminate_locked(rt.record.name)
            just_terminated = True
        if just_terminated:
            ok, still_locked = await _wait_for_artifacts_unlocked(repo_dir)
            if not ok:
                _log_line(
                    f"--- install: gave up waiting for file handles after "
                    f"{int(_LOCK_WAIT_MAX_S)}s; still locked: "
                    f"{', '.join(still_locked)} ---"
                )
                log.error(
                    "app %s: artifacts still locked after %.0fs: %s",
                    rt.record.name, _LOCK_WAIT_MAX_S, still_locked,
                )
                # Proceed anyway — the build will fail loudly with a real
                # error, which is still better than silently rolling back.
                # The caller checks rc and refuses to spawn the stale jar.

        # Snapshot the artifact mtimes BEFORE we run the build. If install
        # returns rc=0 but no artifact advanced, the build effectively
        # no-op'd and any "successful" relaunch would still be the old jar
        # — treat it as failure too.
        pre_install_mtimes = _artifact_mtimes(repo_dir)

        with log_path.open("ab") as f:
            f.write(
                f"\n--- install at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
                f"cwd={repo_dir} ---\n".encode("utf-8")
            )
            f.write(f"$ {spec.command}\n".encode("utf-8"))
        env = os.environ.copy()
        try:
            proc = await asyncio.create_subprocess_shell(
                spec.command,
                cwd=str(repo_dir),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=spec.timeout_s,
                )
            except asyncio.TimeoutError:
                proc.kill()
                with log_path.open("ab") as f:
                    f.write(f"--- install timeout after {spec.timeout_s}s ---\n".encode("utf-8"))
                self._set_install_status(
                    rt, "timeout", f"timed out after {spec.timeout_s}s",
                )
                return 124
            rc = proc.returncode if proc.returncode is not None else 0
            with log_path.open("ab") as f:
                if stdout:
                    f.write(stdout)
                if stderr:
                    f.write(b"--- install stderr ---\n")
                    f.write(stderr)
                f.write(
                    f"--- install rc={rc} stdout_bytes={len(stdout or b'')} "
                    f"stderr_bytes={len(stderr or b'')} ---\n".encode("utf-8")
                )
            if rc != 0:
                self._set_install_status(rt, "failed", f"install rc={rc}")
                return rc
            if not _any_artifact_advanced(repo_dir, pre_install_mtimes):
                # Build said rc=0 but nothing under target/bin/etc. got newer.
                # Almost certainly mvn clean failed silently (the file-lock
                # symptom this whole guard exists to catch). Refuse to spawn:
                # otherwise we'd launch the previous commit's binary and the
                # operator would believe the pull deployed.
                _log_line(
                    "--- install rc=0 but no build artifact advanced; "
                    "refusing to relaunch stale binary ---"
                )
                self._set_install_status(
                    rt, "rolled_back",
                    "install rc=0 but no build artifact advanced",
                )
                return 1
            self._set_install_status(rt, "ok")
            return 0
        except Exception as e:
            with log_path.open("ab") as f:
                f.write(f"--- install error: {e!r} ---\n".encode("utf-8"))
            self._set_install_status(rt, "failed", repr(e))
            return 1

    async def _ensure_ollama(self, rt: _Runtime) -> tuple[bool, str]:
        """Verify Ollama is reachable, pull missing models, optionally warm them.

        Returns (ok, summary_string). Progress is appended to the per-app log.
        """
        spec = rt.manifest.ollama
        if spec is None:
            return True, "skipped"

        log_path = self._log_path(rt.record.name)

        def _log(line: str) -> None:
            try:
                with log_path.open("ab") as f:
                    f.write((line + "\n").encode("utf-8"))
            except Exception:
                pass

        _log(f"--- ollama: probing {spec.host} ---")
        try:
            r = await self._http.get(f"{spec.host}/api/tags", timeout=10)
            r.raise_for_status()
            tags = r.json().get("models") or []
            installed_full = {m.get("name", "") for m in tags}
        except Exception as e:
            _log(f"ollama unreachable at {spec.host}: {e!r}")
            return False, f"unreachable: {e!r}"

        missing = [m for m in spec.models if m not in installed_full]
        for model in missing:
            _log(f"--- ollama pull {model} ---")
            try:
                async with self._http.stream(
                    "POST", f"{spec.host}/api/pull",
                    json={"name": model, "stream": True},
                    timeout=None,
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if line:
                            _log(f"  {line}")
            except Exception as e:
                _log(f"ollama pull {model} failed: {e!r}")
                return False, f"pull {model} failed: {e!r}"

        if spec.warm:
            for model in spec.models:
                try:
                    await self._http.post(
                        f"{spec.host}/api/generate",
                        json={"model": model, "prompt": "", "keep_alive": spec.keep_alive},
                        timeout=120,
                    )
                    _log(f"warmed {model} (keep_alive={spec.keep_alive})")
                except Exception as e:
                    _log(f"warm {model} failed (non-fatal): {e!r}")

        models_summary = ", ".join(spec.models) if spec.models else "(none)"
        return True, f"ready: {models_summary}"

    async def _supervisor_loop(self) -> None:
        log.info("app supervisor loop started")
        try:
            while not self._stop_evt.is_set():
                try:
                    await self._tick()
                except Exception:
                    log.exception("supervisor tick failed")
                try:
                    await asyncio.wait_for(self._stop_evt.wait(), timeout=SUPERVISOR_INTERVAL_S)
                except asyncio.TimeoutError:
                    pass
        finally:
            log.info("app supervisor loop exiting")

    async def _tick(self) -> None:
        now = time.time()
        for name, rt in list(self._apps.items()):
            if rt.in_maintenance:
                # Operator is hand-driving this app; do not spawn, terminate,
                # health-probe, or count restart budget. State (alive/pid)
                # remains accurate via the cheap `proc.returncode is None`
                # check in list_apps().
                #
                # Auto-update *also* skips maintenance apps (default), but
                # the check happens inside _auto_update_due_now() so the
                # operator can opt back in if they ever want to.
                await self._maybe_auto_update(rt, now)
                await self._maybe_product_owner(rt, now)
                continue
            if rt.record.desired_state != "running":
                # Even a stopped app should be auto-updateable so a
                # scheduled cycle can bring a manually-stopped service back
                # up on the fresh code without operator intervention.
                await self._maybe_auto_update(rt, now)
                await self._maybe_product_owner(rt, now)
                continue
            # Crash detection / restart.
            if rt.proc is not None and rt.proc.returncode is not None:
                rt.last_exit_code = rt.proc.returncode
                rt.proc = None
                if rt.log_fh is not None:
                    try:
                        rt.log_fh.close()
                    except Exception:
                        pass
                    rt.log_fh = None
                log.warning("app %s exited rc=%s", name, rt.last_exit_code)
                if rt.manifest.restart.on_crash:
                    rt.next_restart_at = now + rt.manifest.restart.backoff_s

            if rt.proc is None and rt.manifest.restart.on_crash:
                if now < rt.next_restart_at:
                    continue
                if len(self._recent_restarts(rt)) >= rt.manifest.restart.max_per_hour:
                    log.error("app %s restart budget exhausted; staying down", name)
                    continue
                await self._launch(name)

            # Health probe.
            if rt.proc is not None and rt.manifest.health is not None:
                if now >= rt.next_health_at:
                    await self._probe_health(rt)
                    rt.next_health_at = now + rt.manifest.health.interval_s

            await self._maybe_auto_update(rt, now)
            await self._maybe_product_owner(rt, now)

    async def _probe_health(self, rt: _Runtime) -> None:
        spec = rt.manifest.health
        if spec is None or self._http is None:
            return
        try:
            r = await self._http.get(spec.url, timeout=spec.timeout_s)
            ok = (r.status_code == spec.expect_status)
        except Exception:
            ok = False
        rt.last_health = "ok" if ok else "fail"
        rt.last_health_at = time.time()

    # ---------- auto-update ----------

    def _auto_update_cfg(self, rt: _Runtime) -> Optional[dict]:
        """Return the auto_update sub-document iff it's enabled. None means
        the operator hasn't opted this app in, so the tick does nothing."""
        cfg = rt.record.auto_update or {}
        if not cfg.get("enabled"):
            return None
        return cfg

    def _today_scheduled_ts(self, at_utc: str, now: float) -> Optional[int]:
        """Convert an 'HH:MM' UTC schedule string into today's unix ts."""
        try:
            h_str, m_str = at_utc.split(":")
            h, m = int(h_str), int(m_str)
            if not (0 <= h < 24 and 0 <= m < 60):
                return None
        except (ValueError, AttributeError):
            return None
        today = time.gmtime(now)
        return calendar.timegm((
            today.tm_year, today.tm_mon, today.tm_mday,
            h, m, 0, 0, 0, 0,
        ))

    def _auto_update_due_now(self, rt: _Runtime, now: float) -> bool:
        """True iff this app's auto-update schedule fires inside its window
        right now and we haven't already run it today."""
        cfg = self._auto_update_cfg(rt)
        if cfg is None:
            return False
        sched = self._today_scheduled_ts(cfg.get("at_utc", "05:00"), now)
        if sched is None:
            return False
        window_s = max(1, int(cfg.get("window_minutes", 60))) * 60
        if now < sched or now > sched + window_s:
            return False
        last_run = int(cfg.get("last_run_at") or 0)
        if last_run >= sched:
            return False
        if rt.in_maintenance and cfg.get("skip_if_maintenance", True):
            # Burn the slot so we don't keep evaluating it every tick today.
            app_store.record_auto_update_run(
                rt.record.name, "skipped_maintenance",
                message="app in maintenance mode at scheduled time",
                ran_at=int(now),
            )
            rt.record = app_store.get(rt.record.name) or rt.record
            return False
        return True

    async def _maybe_auto_update(self, rt: _Runtime, now: float) -> None:
        """Cheap per-tick gate. Fires _run_auto_update only when due."""
        if not self._auto_update_due_now(rt, now):
            return
        try:
            await self._run_auto_update(rt)
        except Exception:
            log.exception("auto-update for %s raised", rt.record.name)

    def _build_artifacts(self, rt: _Runtime) -> list[Path]:
        repo_dir = self._repo_dir(rt.record.repo_name)
        return _collect_probe_artifacts(repo_dir)

    def _stash_artifacts(self, rt: _Runtime) -> dict[str, str]:
        """Copy current build artifacts to a per-app stash directory under
        C:\\ProgramData\\ClaudeAgent\\stash\\<name>\\ so the rollback path
        can restore them after `mvn clean` (or equivalent) has nuked the
        in-tree target/ output. Each artifact is keyed by its absolute
        original path so _restore_artifacts can put it back exactly where
        it was. Returns {} when there's nothing to stash."""
        stash: dict[str, str] = {}
        stash_dir = _AUTO_UPDATE_STASH_DIR / rt.record.name
        try:
            stash_dir.mkdir(parents=True, exist_ok=True)
            # Wipe any leftovers from a prior aborted run so we don't
            # restore the WRONG generation's artifact on the next rollback.
            for old in stash_dir.iterdir():
                if old.is_file():
                    try:
                        old.unlink()
                    except Exception:
                        pass
        except Exception:
            log.exception(
                "auto-update: cannot prepare stash dir %s "
                "(rollback will be impossible)", stash_dir,
            )
            return {}
        for p in self._build_artifacts(rt):
            backup = stash_dir / p.name
            try:
                shutil.copy2(str(p), str(backup))
                stash[str(p)] = str(backup)
            except Exception:
                log.exception(
                    "auto-update: failed to stash %s (rollback will be partial)", p,
                )
        return stash

    def _restore_artifacts(self, stash: dict[str, str]) -> bool:
        """Restore stashed artifacts back into the build output dirs.
        Creates the original parent directory if missing — Maven's clean
        removes the whole target/ tree, so the directory itself may be
        gone by the time we try to restore.
        Returns True only if every entry restored cleanly."""
        ok = True
        for orig, backup in stash.items():
            try:
                Path(orig).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, orig)
            except Exception:
                log.exception("auto-update: restore %s from %s failed", orig, backup)
                ok = False
        return ok

    def _clear_stash(self, stash: dict[str, str]) -> None:
        """Delete the stashed copies (not the originals)."""
        for backup in stash.values():
            try:
                Path(backup).unlink()
            except Exception:
                pass

    async def _wait_for_health(self, rt: _Runtime, timeout_s: float) -> bool:
        """Poll the app's /health endpoint until it returns the expected
        status, or until `timeout_s` has elapsed. Returns True on green.

        Apps without a [health] block in their manifest can't be probed —
        we treat "alive process" as a proxy and report green if the
        subprocess survives the timeout.
        """
        spec = rt.manifest.health
        deadline = time.monotonic() + timeout_s
        if spec is None or self._http is None:
            while time.monotonic() < deadline:
                if rt.proc is None or rt.proc.returncode is not None:
                    return False
                await asyncio.sleep(2)
            return rt.proc is not None and rt.proc.returncode is None
        while time.monotonic() < deadline:
            try:
                r = await self._http.get(spec.url, timeout=spec.timeout_s)
                if r.status_code == spec.expect_status:
                    rt.last_health = "ok"
                    rt.last_health_at = time.time()
                    return True
            except Exception:
                pass
            await asyncio.sleep(3)
        rt.last_health = "fail"
        rt.last_health_at = time.time()
        return False

    async def _notify_telegram(self, text: str) -> None:
        """Broadcast a one-line message to every allowlisted Telegram user.
        Silent no-op if the bot isn't running or there's no allowlist."""
        tg = getattr(self.runner, "telegram", None)
        if tg is None:
            return
        app = getattr(tg, "application", None)
        if app is None:
            return
        ids = getattr(tg, "allowlist", set()) or set()
        if not ids:
            return
        for uid in ids:
            try:
                await app.bot.send_message(chat_id=uid, text=text)
            except Exception:
                log.exception("auto-update telegram notify to %s failed", uid)

    async def _run_auto_update(self, rt: _Runtime) -> None:
        """Pull the app's repo, rebuild + relaunch if the SHA advanced, and
        roll back on a build or health failure.

        Outcomes (also persisted as `last_result` for inspection via
        list_apps and the admin UI):

          - ``no_change``           — already up to date; no Telegram.
          - ``pull_failed``         — git refused; app left as-is.
          - ``ok``                  — new SHA, build green, health green.
          - ``build_failed``        — mvn rc!=0 or stale-artifact guard;
                                     rolled back to prev JAR and prev SHA.
          - ``health_failed``       — build green but app didn't go green;
                                     rolled back same as build_failed.
          - ``rolled_back``         — build_failed/health_failed where the
                                     restore-prev-binary step itself failed
                                     (app is in an unknown state).
        """
        from agent.handlers import git_ops  # lazy: handlers import this module

        name = rt.record.name
        cfg = self._auto_update_cfg(rt) or {}
        notify = bool(cfg.get("notify_telegram", True))
        rollback_on_health = bool(cfg.get("rollback_on_health_fail", True))
        repo_name = rt.record.repo_name
        log_path = self._log_path(name)

        def _log(line: str) -> None:
            try:
                with log_path.open("ab") as f:
                    f.write((line + "\n").encode("utf-8"))
            except Exception:
                pass

        _log(f"--- auto-update {name} starting at "
             f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ---")

        pull = await git_ops.pull_repo_quietly(self.cfg, repo_name)
        prev_sha = pull.get("prev_sha") or rt.record.last_known_sha or ""
        new_sha = pull.get("new_sha") or prev_sha
        if not pull["ok"]:
            err = pull.get("error") or "pull failed"
            _log(f"auto-update pull failed: {err}")
            app_store.record_auto_update_run(
                name, "pull_failed", message=err,
                prev_sha=prev_sha, new_sha=new_sha,
            )
            rt.record = app_store.get(name) or rt.record
            if notify:
                await self._notify_telegram(
                    f"🔴 auto-update {name}: pull FAILED ({err[:120]})"
                )
            return

        if not pull.get("changed"):
            _log(f"auto-update: no new commits since {prev_sha[:12] or '-'}")
            app_store.record_auto_update_run(
                name, "no_change", message="repo already at HEAD",
                prev_sha=prev_sha, new_sha=new_sha,
            )
            rt.record = app_store.get(name) or rt.record
            return  # silent — daily "no change" pings would be noise

        # SHA moved. Stash current artifacts before the cycle so we can
        # roll back if either the build or the health probe fails.
        stash = self._stash_artifacts(rt) if rollback_on_health else {}
        cycle_started = time.time()
        await self._cycle_after_pull(rt, new_sha)
        downtime = time.time() - cycle_started

        if rt.install_status != "ok":
            # Build failed (or produced no fresh artifact). The supervisor
            # has already kept the app down; restore prev binary + prev SHA
            # and relaunch the previous good build.
            if not rollback_on_health:
                _log(f"auto-update build failed at {new_sha[:12]} "
                     f"(status={rt.install_status}); rollback disabled, "
                     f"app stays down")
                app_store.record_auto_update_run(
                    name, "build_failed",
                    message=rt.install_message or rt.install_status,
                    prev_sha=prev_sha, new_sha=new_sha,
                )
                rt.record = app_store.get(name) or rt.record
                if notify:
                    await self._notify_telegram(
                        f"🔴 auto-update {name} {prev_sha[:7]} → {new_sha[:7]} "
                        f"build FAILED ({rt.install_status}); app is DOWN"
                    )
                return
            restored = await self._rollback_to(rt, prev_sha, stash, _log,
                                                reason="build_failed")
            terminal = "build_failed" if restored else "rolled_back"
            app_store.record_auto_update_run(
                name, terminal,
                message=(rt.install_message or rt.install_status or "build failed"),
                prev_sha=prev_sha, new_sha=new_sha,
            )
            rt.record = app_store.get(name) or rt.record
            if notify:
                if restored:
                    await self._notify_telegram(
                        f"🟠 auto-update {name} build FAILED at {new_sha[:7]}; "
                        f"rolled back to {prev_sha[:7]}"
                    )
                else:
                    await self._notify_telegram(
                        f"🔴 auto-update {name}: build failed AND rollback "
                        f"failed; app may be in an inconsistent state"
                    )
            return

        # Build green; verify the app reports healthy on the new code.
        healthy = await self._wait_for_health(rt, _AUTO_UPDATE_HEALTH_WAIT_S)
        if healthy:
            self._clear_stash(stash)
            app_store.record_auto_update_run(
                name, "ok",
                message=f"downtime {downtime:.0f}s",
                prev_sha=prev_sha, new_sha=new_sha,
            )
            rt.record = app_store.get(name) or rt.record
            if notify:
                await self._notify_telegram(
                    f"🟢 auto-update {name} {prev_sha[:7]} → {new_sha[:7]} "
                    f"({downtime:.0f}s downtime, health ok)"
                )
            return

        # Built but didn't come up green within the window.
        if not rollback_on_health:
            _log(f"auto-update {name}: health failed at {new_sha[:12]}, "
                 f"rollback disabled; leaving on new code")
            app_store.record_auto_update_run(
                name, "health_failed",
                message=f"health probe didn't go green within "
                        f"{_AUTO_UPDATE_HEALTH_WAIT_S}s",
                prev_sha=prev_sha, new_sha=new_sha,
            )
            rt.record = app_store.get(name) or rt.record
            if notify:
                await self._notify_telegram(
                    f"🟠 auto-update {name} {prev_sha[:7]} → {new_sha[:7]}: "
                    f"built ok but HEALTH FAILED; left on new code"
                )
            return
        restored = await self._rollback_to(rt, prev_sha, stash, _log,
                                            reason="health_failed")
        terminal = "health_failed" if restored else "rolled_back"
        app_store.record_auto_update_run(
            name, terminal,
            message=f"health probe didn't go green within "
                    f"{_AUTO_UPDATE_HEALTH_WAIT_S}s",
            prev_sha=prev_sha, new_sha=new_sha,
        )
        rt.record = app_store.get(name) or rt.record
        if notify:
            if restored:
                await self._notify_telegram(
                    f"🟠 auto-update {name}: built ok but HEALTH FAILED at "
                    f"{new_sha[:7]}; rolled back to {prev_sha[:7]}"
                )
            else:
                await self._notify_telegram(
                    f"🔴 auto-update {name}: health failed AND rollback failed; "
                    f"app may be in an inconsistent state"
                )

    async def _rollback_to(
        self, rt: _Runtime, prev_sha: str, stash: dict[str, str],
        log_line, reason: str,
    ) -> bool:
        """Restore the previous build artifacts, hard-reset the repo to
        `prev_sha`, and relaunch the app from that prev binary. Returns
        True if the restored process is alive at the end.

        Fast path: restore the stashed JAR + git reset + relaunch. No
        rebuild, ~5s rollback.

        Fallback: if the stash is empty or restore failed (Maven cleaned
        the dir, disk full, etc.), and prev_sha is known, run the install
        from the rolled-back source. Slower (~90s on jeeves), but rescues
        the case where the prev JAR isn't on disk anymore.
        """
        from agent.handlers import git_ops

        name = rt.record.name
        log_line(f"--- auto-update rollback ({reason}) target sha={prev_sha[:12]} ---")

        restored_ok = False
        if not stash:
            log_line("--- auto-update rollback: no stashed artifacts to restore ---")
        else:
            restored_ok = self._restore_artifacts(stash)
            if not restored_ok:
                log_line("--- auto-update rollback: artifact restore had failures ---")

        if prev_sha:
            ok, msg = await git_ops.git_reset_hard(self.cfg, rt.record.repo_name, prev_sha)
            if not ok:
                log_line(f"--- auto-update rollback: git reset failed: {msg} ---")
        else:
            log_line("--- auto-update rollback: no prev_sha known, skipping git reset ---")

        # If the stash didn't deliver a usable binary, rebuild from the
        # rolled-back source so we have something to launch. _run_install
        # already terminates the live proc and waits for file-handle
        # release; calling it here is safe even though we may have just
        # restored artifacts (success means the same JAR comes out the
        # other end).
        need_rebuild = (not restored_ok) and bool(prev_sha)
        if need_rebuild and rt.manifest.install is not None:
            log_line("--- auto-update rollback: rebuilding from prev sha ---")
            rc = await self._run_install(rt)
            if rc != 0:
                log_line(
                    f"--- auto-update rollback: rebuild failed (rc={rc} "
                    f"status={rt.install_status}); app stays down ---"
                )
                return False

        async with self._lock:
            await self._terminate_locked(name)
            await self._launch_locked(name)
        # Best-effort: did we come back up?
        alive_deadline = time.monotonic() + 30
        while time.monotonic() < alive_deadline:
            if rt.proc is not None and rt.proc.returncode is None:
                if prev_sha:
                    app_store.set_last_sha(name, prev_sha)
                    rt.record = app_store.get(name) or rt.record
                self._clear_stash(stash)
                return True
            await asyncio.sleep(2)
        return False

    async def auto_update_now(self, name: str) -> dict:
        """Operator-triggered manual run. Ignores the scheduled-time gate
        but otherwise behaves exactly like the daily tick (so the same
        rollback paths get exercised). Used by smoke tests and by
        emergency 'pull the new fix right now' operations.
        """
        rt = self._apps.get(name)
        if rt is None:
            return {"ok": False, "error": f"unknown app: {name}"}
        # Don't honor `enabled=False` for a manual trigger — operator
        # explicitly asked for it. But DO honor maintenance mode unless
        # they flipped that off.
        cfg = rt.record.auto_update or {}
        if rt.in_maintenance and cfg.get("skip_if_maintenance", True):
            return {
                "ok": False,
                "error": f"app {name} is in maintenance mode; flip to active first",
            }
        await self._run_auto_update(rt)
        # Re-read the record so callers see the persisted last_* fields.
        rec = app_store.get(name)
        if rec is None:
            return {"ok": True, "result": None}
        return {
            "ok": True,
            "result": (rec.auto_update or {}).get("last_result"),
            "message": (rec.auto_update or {}).get("last_message"),
            "prev_sha": (rec.auto_update or {}).get("last_prev_sha"),
            "new_sha": (rec.auto_update or {}).get("last_new_sha"),
            "ran_at": (rec.auto_update or {}).get("last_run_at"),
        }

    async def set_auto_update(self, name: str, partial: dict) -> Optional[app_store.AppRecord]:
        """Merge operator-supplied auto-update config into the persisted
        record and refresh the live runtime."""
        rt = self._apps.get(name)
        if rt is None:
            return None
        rec = app_store.set_auto_update(name, partial)
        if rec is not None:
            rt.record = rec
        return rec

    # ---------- product owner ----------

    def _product_owner_cfg(self, rt: _Runtime) -> Optional[dict]:
        cfg = rt.record.product_owner or {}
        if not cfg.get("enabled"):
            return None
        return cfg

    def _product_owner_due_now(self, rt: _Runtime, now: float) -> bool:
        cfg = self._product_owner_cfg(rt)
        if cfg is None:
            return False
        sched = self._today_scheduled_ts(cfg.get("at_utc", "10:00"), now)
        if sched is None:
            return False
        # day-of-week gate (-1 means daily)
        dow = int(cfg.get("day_of_week", 0))
        if dow >= 0:
            if time.gmtime(now).tm_wday != dow:
                return False
        # 60-minute window from the scheduled time; longer than auto-update's
        # default because a product-owner run can legitimately take hours
        # (the Telegram feedback window alone is typically 6h), but the
        # WINDOW we use here is only "may we start"; once started the run
        # has its own internal timing.
        window_s = 60 * 60
        if now < sched or now > sched + window_s:
            return False
        last_run = int(cfg.get("last_run_at") or 0)
        if last_run >= sched:
            return False
        if rt.in_maintenance and cfg.get("skip_if_maintenance", True):
            app_store.record_product_owner_run(
                rt.record.name, "skipped_maintenance",
                message="app in maintenance mode at scheduled time",
                ran_at=int(now),
            )
            rt.record = app_store.get(rt.record.name) or rt.record
            return False
        return True

    async def _maybe_product_owner(self, rt: _Runtime, now: float) -> None:
        if not self._product_owner_due_now(rt, now):
            return
        # Reserve the slot BEFORE we await — a multi-hour run must not be
        # eligible for re-trigger by the next tick while it's still going.
        app_store.record_product_owner_run(
            rt.record.name, "ok",  # provisional; corrected below
            message="(in progress)",
            ran_at=int(now),
        )
        rt.record = app_store.get(rt.record.name) or rt.record
        # Run on a background task so the supervisor loop isn't blocked.
        self.runner.spawn(self._run_product_owner(rt))

    async def _run_product_owner(self, rt: _Runtime) -> dict:
        from agent import product_owner as po  # lazy: keep import cost off boot

        name = rt.record.name
        log_path = self._log_path(name)

        def _log(line: str) -> None:
            try:
                with log_path.open("ab") as f:
                    f.write((line + "\n").encode("utf-8"))
            except Exception:
                pass

        cfg = dict(rt.record.product_owner or {})
        repo_dir = self._repo_dir(rt.record.repo_name)
        if not (repo_dir / ".git").exists():
            msg = f"repo dir {repo_dir} is not a git checkout"
            _log(f"--- product-owner: {msg} ---")
            app_store.record_product_owner_run(name, "git_failed", message=msg)
            rt.record = app_store.get(name) or rt.record
            return {"result": "git_failed", "message": msg}

        try:
            result = await po.run_review(self.runner, rt, repo_dir, cfg, _log)
        except Exception as e:
            _log(f"--- product-owner: raised: {e!r} ---")
            log.exception("product-owner for %s raised", name)
            app_store.record_product_owner_run(
                name, "llm_failed", message=f"raised: {e!r}",
            )
            rt.record = app_store.get(name) or rt.record
            return {"result": "llm_failed", "message": repr(e)}

        app_store.record_product_owner_run(
            name,
            result["result"],
            message=result.get("message"),
            branch=result.get("branch"),
            spec_count=int(result.get("spec_count") or 0),
        )
        rt.record = app_store.get(name) or rt.record
        return result

    async def product_owner_now(self, name: str) -> dict:
        """Operator-triggered manual run. Fire-and-forget — a full review
        can take many hours (Telegram feedback window + Claude API +
        git push), so we spawn it and return immediately. Poll list_apps
        and inspect the per-app `product_owner` field for completion
        state (`last_run_at` advances when the run finishes; `last_result`
        records the outcome).
        """
        rt = self._apps.get(name)
        if rt is None:
            return {"ok": False, "error": f"unknown app: {name}"}
        cfg = rt.record.product_owner or {}
        if rt.in_maintenance and cfg.get("skip_if_maintenance", True):
            return {
                "ok": False,
                "error": f"app {name} is in maintenance mode; flip to active first",
            }
        # Mark as in-progress before we spawn so a second concurrent
        # invocation can detect it and back off.
        app_store.record_product_owner_run(
            name, "ok", message="(in progress)", ran_at=int(time.time()),
        )
        rt.record = app_store.get(name) or rt.record
        self.runner.spawn(self._run_product_owner(rt))
        return {
            "ok": True,
            "result": "started",
            "message": "review running in background; check list_apps for completion",
        }

    async def set_product_owner(
        self, name: str, partial: dict,
    ) -> Optional[app_store.AppRecord]:
        rt = self._apps.get(name)
        if rt is None:
            return None
        rec = app_store.set_product_owner(name, partial)
        if rec is not None:
            rt.record = rec
        return rec
