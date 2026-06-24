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
import logging
import os
import shlex
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
        for name, rt in self._apps.items():
            alive = rt.proc is not None and rt.proc.returncode is None
            uptime = (time.time() - rt.started_at) if (alive and rt.started_at) else None
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
            })
        return out

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
                continue
            if rt.record.desired_state != "running":
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

