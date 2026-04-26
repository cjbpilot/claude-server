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
            install_result = "ok" if rc == 0 else f"failed rc={rc}"
            if rc != 0:
                # Don't auto-start a broken install.
                app_store.set_desired(manifest.name, "stopped")
                rt.record = app_store.get(manifest.name) or rec
                return {
                    "name": manifest.name, "repo": repo_name,
                    "install": install_result, "started": False,
                }

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
        app_store.set_desired(name, "running")
        rt.record = app_store.get(name) or rt.record
        await self._launch(name)
        return True

    async def stop_app(self, name: str) -> bool:
        rt = self._apps.get(name)
        if rt is None:
            return False
        app_store.set_desired(name, "stopped")
        rt.record = app_store.get(name) or rt.record
        await self._terminate(name)
        return True

    async def restart_app(self, name: str) -> bool:
        if name not in self._apps:
            return False
        await self._terminate(name)
        await asyncio.sleep(0.5)
        await self._launch(name)
        return True

    def list_apps(self) -> list[dict]:
        out = []
        for name, rt in self._apps.items():
            alive = rt.proc is not None and rt.proc.returncode is None
            uptime = (time.time() - rt.started_at) if (alive and rt.started_at) else None
            out.append({
                "name": name,
                "repo": rt.record.repo_name,
                "desired": rt.record.desired_state,
                "alive": alive,
                "pid": rt.proc.pid if rt.proc is not None else None,
                "uptime_s": round(uptime, 1) if uptime is not None else None,
                "last_exit_code": rt.last_exit_code,
                "last_health": rt.last_health,
                "last_health_at": rt.last_health_at,
                "restart_count_recent": len(self._recent_restarts(rt)),
                "last_known_sha": rt.record.last_known_sha,
                "log_path": str(self._log_path(name)),
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
            app_store.set_last_sha(rt.record.name, new_sha)
            rt.record = app_store.get(rt.record.name) or rt.record
            if rt.manifest.restart.on_update and rt.record.desired_state == "running":
                if rt.manifest.install is not None:
                    await self._run_install(rt)
                await self.restart_app(rt.record.name)
                cycled.append(rt.record.name)
        return cycled

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

    async def _run_install(self, rt: _Runtime) -> int:
        spec = rt.manifest.install
        if spec is None:
            return 0
        repo_dir = self._repo_dir(rt.record.repo_name)
        log_path = self._log_path(rt.record.name)
        with log_path.open("ab") as f:
            f.write(f"\n--- install at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ---\n".encode("utf-8"))
            f.write(f"$ {spec.command}\n".encode("utf-8"))
        env = os.environ.copy()
        try:
            proc = await asyncio.create_subprocess_shell(
                spec.command,
                cwd=str(repo_dir),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=spec.timeout_s)
            except asyncio.TimeoutError:
                proc.kill()
                with log_path.open("ab") as f:
                    f.write(f"--- install timeout after {spec.timeout_s}s ---\n".encode("utf-8"))
                return 124
            with log_path.open("ab") as f:
                f.write(stdout or b"")
                f.write(f"--- install rc={proc.returncode} ---\n".encode("utf-8"))
            return proc.returncode or 0
        except Exception as e:
            with log_path.open("ab") as f:
                f.write(f"--- install error: {e!r} ---\n".encode("utf-8"))
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
