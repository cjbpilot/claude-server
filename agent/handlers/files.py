"""File-system access constrained to a small set of allowed roots.

Designed primarily for transmitting credentials onto the host machine
without committing them to a repo. The allowlist confines writes to:

  - C:\\ProgramData\\ClaudeAgent\\secrets\\         (general secret store)
  - C:\\ProgramData\\ClaudeAgent\\app-secrets\\<n>\\ (per-app secret store)
  - the configured workspace_dir (so per-repo .env files etc. are allowed)

Symlinks are resolved before the allowlist check, so an attacker can't
escape via a symlink under one of the allowed roots.

When `secret=true` the destination file is ACL'd to SYSTEM:F +
Administrators:F (matching how nats.creds and repos.json are stored).
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys
from pathlib import Path

from shared.protocol import Command, Reply

log = logging.getLogger("agent.files")

_DEFAULT_SECRETS_ROOT = Path(r"C:\ProgramData\ClaudeAgent\secrets")
_APP_SECRETS_ROOT = Path(r"C:\ProgramData\ClaudeAgent\app-secrets")
_MAX_BYTES = 1024 * 1024  # 1 MB cap per call


def _allowed_roots(cfg) -> list[Path]:
    roots: list[Path] = []
    for r in (_DEFAULT_SECRETS_ROOT, _APP_SECRETS_ROOT):
        try:
            r.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        try:
            roots.append(r.resolve())
        except Exception:
            roots.append(r)
    try:
        roots.append(cfg.workspace_dir.resolve())
    except Exception:
        pass
    return roots


def _resolve_safe(cfg, path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        raise ValueError("path must be absolute")
    # Resolve symlinks and '..' segments so we can't escape via either.
    resolved = p.resolve()
    for root in _allowed_roots(cfg):
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise ValueError(
        "path not in allowlist; allowed roots: "
        + ", ".join(str(r) for r in _allowed_roots(cfg))
    )


def _lock_secret_acl(path: Path) -> None:
    if sys.platform != "win32":
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r",
             "/grant", "SYSTEM:F", "Administrators:F"],
            check=False, capture_output=True,
        )
    except Exception:
        log.exception("icacls lock failed for %s", path)


async def handle_write_file(hctx, cmd: Command) -> Reply:
    """Write content to an allowlisted path. Args:

        path: absolute path under one of the allowed roots
        content: the bytes to write (text by default)
        encoding: text encoding when binary_b64 is false (default utf-8)
        binary_b64: if true, content is base64-decoded before writing
        secret: if true, lock ACL to SYSTEM:F + Administrators:F
    """
    path_arg = (cmd.args.get("path") or "").strip()
    content = cmd.args.get("content")
    encoding = (cmd.args.get("encoding") or "utf-8").strip()
    secret = bool(cmd.args.get("secret", False))
    binary_b64 = bool(cmd.args.get("binary_b64", False))

    if not path_arg:
        return Reply(id=cmd.id, ok=False, error="missing 'path'")
    if content is None:
        return Reply(id=cmd.id, ok=False, error="missing 'content'")

    try:
        target = _resolve_safe(hctx.cfg, path_arg)
    except ValueError as e:
        return Reply(id=cmd.id, ok=False, error=str(e))

    if binary_b64:
        try:
            data = base64.b64decode(str(content))
        except Exception as e:
            return Reply(id=cmd.id, ok=False, error=f"bad base64: {e!r}")
    else:
        data = str(content).encode(encoding)

    if len(data) > _MAX_BYTES:
        return Reply(id=cmd.id, ok=False,
                     error=f"content too large ({len(data)} > {_MAX_BYTES})")

    target.parent.mkdir(parents=True, exist_ok=True)
    # If file exists with locked ACL, re-grant so we can overwrite.
    if target.exists() and sys.platform == "win32":
        try:
            subprocess.run(
                ["icacls", str(target), "/grant", "Administrators:F"],
                check=False, capture_output=True,
            )
        except Exception:
            pass

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)

    if secret:
        _lock_secret_acl(target)

    log.info("write_file %s bytes=%d secret=%s", target, len(data), secret)
    return Reply(id=cmd.id, ok=True, data={
        "path": str(target),
        "bytes": len(data),
        "secret": secret,
        "binary_b64": binary_b64,
    })


async def handle_read_file(hctx, cmd: Command) -> Reply:
    """Read content from an allowlisted path. Args:

        path: absolute path under one of the allowed roots
        encoding: text encoding when binary_b64 is false
        binary_b64: if true, return base64-encoded raw bytes
    """
    path_arg = (cmd.args.get("path") or "").strip()
    encoding = (cmd.args.get("encoding") or "utf-8").strip()
    binary_b64 = bool(cmd.args.get("binary_b64", False))

    if not path_arg:
        return Reply(id=cmd.id, ok=False, error="missing 'path'")

    try:
        target = _resolve_safe(hctx.cfg, path_arg)
    except ValueError as e:
        return Reply(id=cmd.id, ok=False, error=str(e))

    if not target.exists():
        return Reply(id=cmd.id, ok=False, error=f"not found: {target}")
    if not target.is_file():
        return Reply(id=cmd.id, ok=False, error=f"not a file: {target}")

    size = target.stat().st_size
    if size > _MAX_BYTES:
        return Reply(id=cmd.id, ok=False,
                     error=f"file too large ({size} > {_MAX_BYTES})")

    raw = target.read_bytes()
    if binary_b64:
        content = base64.b64encode(raw).decode("ascii")
    else:
        try:
            content = raw.decode(encoding)
        except Exception as e:
            return Reply(id=cmd.id, ok=False,
                         error=f"decode failed ({e!r}); retry with binary_b64=true")

    log.info("read_file %s bytes=%d", target, size)
    return Reply(id=cmd.id, ok=True, data={
        "path": str(target),
        "bytes": size,
        "content": content,
        "binary_b64": binary_b64,
    })


async def handle_delete_file(hctx, cmd: Command) -> Reply:
    path_arg = (cmd.args.get("path") or "").strip()
    if not path_arg:
        return Reply(id=cmd.id, ok=False, error="missing 'path'")

    try:
        target = _resolve_safe(hctx.cfg, path_arg)
    except ValueError as e:
        return Reply(id=cmd.id, ok=False, error=str(e))

    if not target.exists():
        return Reply(id=cmd.id, ok=True, data={
            "path": str(target), "removed": False, "note": "did not exist",
        })

    if target.is_dir():
        return Reply(id=cmd.id, ok=False, error="path is a directory; refusing to delete")

    if sys.platform == "win32":
        try:
            subprocess.run(
                ["icacls", str(target), "/grant", "Administrators:F"],
                check=False, capture_output=True,
            )
        except Exception:
            pass
    target.unlink()
    log.info("delete_file %s", target)
    return Reply(id=cmd.id, ok=True, data={"path": str(target), "removed": True})


_LOG_DIR = Path(r"C:\ProgramData\ClaudeAgent\logs")
_LOG_FILES = {
    "agent": _LOG_DIR / "agent.log",
    "stderr": _LOG_DIR / "stderr.log",
    "stdout": _LOG_DIR / "stdout.log",
    "updater": _LOG_DIR / "updater.log",
}


async def handle_tail_logs(hctx, cmd: Command) -> Reply:
    """Tail one of the agent's own log files. Args:

      file: 'agent' (default) | 'stderr' | 'stdout' | 'updater'
            OR an absolute path under C:\\ProgramData\\ClaudeAgent\\logs\\
            OR an app log name -> we look up app-<name>.log
      lines: number of trailing lines (default 100, max 2000)
    """
    name = (cmd.args.get("file") or "agent").strip()
    lines = int(cmd.args.get("lines") or 100)
    lines = max(1, min(2000, lines))

    target: Path | None = None
    if name in _LOG_FILES:
        target = _LOG_FILES[name]
    else:
        candidate = Path(name)
        if candidate.is_absolute():
            try:
                resolved = candidate.resolve()
                resolved.relative_to(_LOG_DIR.resolve())
                target = resolved
            except Exception:
                return Reply(id=cmd.id, ok=False,
                             error=f"path must be inside {_LOG_DIR}")
        else:
            # Treat as an app log shorthand.
            target = _LOG_DIR / f"app-{name}.log"

    if not target.exists():
        return Reply(id=cmd.id, ok=False, error=f"not found: {target}")

    try:
        with target.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            read = min(size, max(lines * 200, 16384))
            f.seek(size - read)
            blob = f.read().decode("utf-8", errors="replace")
    except Exception as e:
        return Reply(id=cmd.id, ok=False, error=repr(e))

    rows = blob.splitlines()[-lines:]
    return Reply(id=cmd.id, ok=True, data={
        "path": str(target),
        "lines": rows,
        "total_size": size,
    })


async def handle_list_dir(hctx, cmd: Command) -> Reply:
    path_arg = (cmd.args.get("path") or "").strip()
    if not path_arg:
        return Reply(id=cmd.id, ok=False, error="missing 'path'")

    try:
        target = _resolve_safe(hctx.cfg, path_arg)
    except ValueError as e:
        return Reply(id=cmd.id, ok=False, error=str(e))

    if not target.exists() or not target.is_dir():
        return Reply(id=cmd.id, ok=False, error=f"not a directory: {target}")

    entries = []
    try:
        for child in target.iterdir():
            try:
                st = child.stat()
                entries.append({
                    "name": child.name,
                    "is_dir": child.is_dir(),
                    "size": st.st_size if child.is_file() else None,
                    "mtime": int(st.st_mtime),
                })
            except Exception:
                continue
    except Exception as e:
        return Reply(id=cmd.id, ok=False, error=repr(e))

    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return Reply(id=cmd.id, ok=True, data={"path": str(target), "entries": entries})
