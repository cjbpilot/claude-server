"""Dynamic repo registry handlers: register, unregister, update token."""

from __future__ import annotations

import re

from agent import repo_store
from shared.protocol import Command, Reply


_TOKEN_PATTERNS = (
    re.compile(r"^ghp_[A-Za-z0-9]{20,}$"),
    re.compile(r"^gho_[A-Za-z0-9]{20,}$"),
    re.compile(r"^github_pat_[A-Za-z0-9_]{20,}$"),
    re.compile(r"^ghs_[A-Za-z0-9]{20,}$"),
)


def _looks_like_token(s: str) -> bool:
    return any(p.match(s) for p in _TOKEN_PATTERNS)


def _validate_url(url: str) -> str | None:
    if not url or not url.startswith(("https://", "http://", "ssh://", "git@")):
        return "url must start with https:// http:// ssh:// or git@"
    if "@" in url.split("://", 1)[-1].split("/", 1)[0]:
        return "url contains an embedded credential; pass the token separately"
    return None


async def handle_register_repo(hctx, cmd: Command) -> Reply:
    name = (cmd.args.get("name") or "").strip()
    url = (cmd.args.get("url") or "").strip()
    branch = (cmd.args.get("branch") or "main").strip()
    token = cmd.args.get("token")

    if not name or not name.replace("-", "").replace("_", "").isalnum():
        return Reply(id=cmd.id, ok=False,
                     error="name must be alphanumeric (dashes/underscores allowed)")
    err = _validate_url(url)
    if err:
        return Reply(id=cmd.id, ok=False, error=err)
    if token is not None:
        token = str(token).strip() or None
        if token and not _looks_like_token(token):
            return Reply(id=cmd.id, ok=False,
                         error="token does not look like a GitHub PAT (ghp_/gho_/github_pat_/ghs_)")

    entry = repo_store.upsert(name=name, url=url, branch=branch, token=token)
    return Reply(id=cmd.id, ok=True, data=entry.to_safe())


async def handle_unregister_repo(hctx, cmd: Command) -> Reply:
    name = (cmd.args.get("name") or "").strip()
    if not name:
        return Reply(id=cmd.id, ok=False, error="missing 'name'")
    removed = repo_store.delete(name)
    return Reply(id=cmd.id, ok=True, data={"name": name, "removed": removed})


async def handle_update_repo_token(hctx, cmd: Command) -> Reply:
    name = (cmd.args.get("name") or "").strip()
    token = cmd.args.get("token")
    if not name:
        return Reply(id=cmd.id, ok=False, error="missing 'name'")
    if token is not None:
        token = str(token).strip() or None
        if token and not _looks_like_token(token):
            return Reply(id=cmd.id, ok=False,
                         error="token does not look like a GitHub PAT")
    entry = repo_store.set_token(name, token)
    if entry is None:
        return Reply(id=cmd.id, ok=False, error=f"unknown repo: {name}")
    return Reply(id=cmd.id, ok=True, data=entry.to_safe())
