"""Scheduled product-owner review of a managed app.

Runs on a configurable weekly cadence per app. For each run the supervisor:

  1. Gathers cheap context from the app's workspace clone (README, CLAUDE.md,
     recent git log, the most-recent docs/product-owner files, TODO/FIXME
     comments in source).
  2. Optionally posts a Telegram question to allowlisted operators asking
     for pain points / wishlist items, then collects replies for a
     configurable window (default 6h) before continuing.
  3. Calls Claude with the gathered context AND the `web_search` server
     tool enabled, asking it to propose `max_specs` new feature specs
     grounded in both the codebase and external best-practice research.
  4. Writes each proposal as a markdown file under
     `docs/product-owner/<YYYY-MM-DD>-<slug>.md`, commits them to a
     branch named `product-owner/<YYYY-MM-DD>`, and pushes that branch
     to origin so the operator can open a PR by hand.

External dependencies — the agent host must have:
  - A valid Anthropic API key at
    `C:\\ProgramData\\ClaudeAgent\\secrets\\anthropic.token` (one line,
    SYSTEM-only ACL, never logged).
  - Network reachability to api.anthropic.com.
  - The same repo's PAT registered in repo_store (the existing one used
    for git_pull works for push too, since GitHub PATs are bidirectional).

The whole flow is best-effort: every external boundary (Anthropic API,
Telegram, git push) is wrapped so a transient failure records a result
string and bails out rather than wedging the supervisor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

from agent import repo_store

log = logging.getLogger("agent.product_owner")

ANTHROPIC_TOKEN_PATH = Path(r"C:\ProgramData\ClaudeAgent\secrets\anthropic.token")
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-opus-4-7"
# Maximum bytes from each readme/source file we forward to Claude. Keeping
# this bounded matters — the prompt also includes git log, TODOs, and any
# Telegram feedback, and we are paying per input token.
_README_MAX = 8000
_CLAUDE_MD_MAX = 4000
_SOURCE_TREE_FILE_CAP = 200
_TODO_MAX_ITEMS = 40
_GIT_LOG_LIMIT = 30
_EXISTING_SPECS_MAX = 12


def load_anthropic_token() -> Optional[str]:
    if not ANTHROPIC_TOKEN_PATH.exists():
        return None
    try:
        v = ANTHROPIC_TOKEN_PATH.read_text(encoding="utf-8").strip()
        return v or None
    except Exception:
        log.exception("could not read %s", ANTHROPIC_TOKEN_PATH)
        return None


# ----- context gathering -----

def _read_capped(path: Path, cap: int) -> str:
    try:
        with path.open("rb") as f:
            data = f.read(cap + 1)
        text = data.decode("utf-8", errors="replace")
        if len(data) > cap:
            return text[:cap] + "\n\n[...truncated...]"
        return text
    except Exception:
        return ""


def _scan_todos(repo_dir: Path, max_items: int = _TODO_MAX_ITEMS) -> list[str]:
    """Best-effort TODO/FIXME/XXX scan across the common source dirs. We
    DO NOT shell out to `grep` because Windows doesn't have it; this is a
    bounded recursive walk that stops once we've gathered enough hits.
    """
    out: list[str] = []
    pattern = re.compile(rb"\b(TODO|FIXME|XXX)[:\s]")
    skip_dirs = {".git", "node_modules", "target", "build", "dist",
                 ".idea", ".vscode", "venv", ".venv", "__pycache__"}
    skip_exts = {".jar", ".class", ".png", ".jpg", ".gif", ".pdf", ".zip",
                 ".bin", ".so", ".dll", ".exe", ".jpeg", ".webp", ".mp4"}
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            if Path(fname).suffix.lower() in skip_exts:
                continue
            fp = Path(root) / fname
            try:
                if fp.stat().st_size > 200_000:
                    continue
                with fp.open("rb") as f:
                    for i, line in enumerate(f, 1):
                        if pattern.search(line):
                            rel = fp.relative_to(repo_dir).as_posix()
                            try:
                                text = line.decode("utf-8", errors="replace").strip()
                            except Exception:
                                text = "?"
                            out.append(f"{rel}:{i}: {text}")
                            if len(out) >= max_items:
                                return out
            except Exception:
                continue
    return out


def _git_log(repo_dir: Path, limit: int = _GIT_LOG_LIMIT) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "log", f"-{limit}", "--oneline"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.stdout.splitlines() if proc.returncode == 0 else []
    except Exception:
        return []


def _existing_specs(repo_dir: Path) -> list[str]:
    """Names of files already in docs/product-owner/, newest first. We
    forward these to Claude so it knows what's already been proposed and
    can choose to either build on them OR steer clear."""
    d = repo_dir / "docs" / "product-owner"
    if not d.exists():
        return []
    try:
        files = sorted(
            (f for f in d.iterdir() if f.is_file() and f.suffix == ".md"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        return [f.name for f in files[:_EXISTING_SPECS_MAX]]
    except Exception:
        return []


def _top_level_listing(repo_dir: Path, max_entries: int = 80) -> list[str]:
    """One-line directory listing of the repo root and the main src dir,
    so Claude has a sense of the project shape without us shipping every
    file."""
    out: list[str] = []
    try:
        for p in sorted(repo_dir.iterdir()):
            if p.name.startswith(".") and p.name != ".github":
                continue
            out.append(p.name + ("/" if p.is_dir() else ""))
            if len(out) >= max_entries:
                break
    except Exception:
        pass
    return out


def gather_context(repo_dir: Path, app_name: str) -> dict:
    """Pull the cheap, bounded inputs the LLM needs. Stays under ~30 KB."""
    readme = ""
    for name in ("README.md", "Readme.md", "readme.md"):
        p = repo_dir / name
        if p.exists():
            readme = _read_capped(p, _README_MAX)
            break
    claude_md = ""
    for name in ("CLAUDE.md", "claude.md", "AGENTS.md"):
        p = repo_dir / name
        if p.exists():
            claude_md = _read_capped(p, _CLAUDE_MD_MAX)
            break
    return {
        "app_name": app_name,
        "readme": readme,
        "claude_md": claude_md,
        "git_log": _git_log(repo_dir),
        "todos": _scan_todos(repo_dir),
        "existing_specs": _existing_specs(repo_dir),
        "top_level": _top_level_listing(repo_dir),
    }


# ----- LLM call -----

def _build_user_prompt(ctx: dict, feedback: list[str], max_specs: int) -> str:
    parts = [
        f"You are acting as the product owner for the **{ctx['app_name']}** application.",
        "Use the web_search tool to research current best practices, recent",
        "developments, and competitor features relevant to this app's domain.",
        f"Then propose {max_specs} concrete feature/improvement specs.",
        "",
        "Output ONE fenced JSON code block (no other prose outside it). Schema:",
        "```json",
        '{"specs":[{',
        '  "title": "short imperative phrase",',
        '  "slug": "kebab-case-slug",',
        '  "complexity": "S" | "M" | "L",',
        '  "problem": "1-3 paragraphs",',
        '  "proposed_solution": "1-3 paragraphs with concrete touchpoints",',
        '  "why_now": "1 paragraph; cite operator feedback or research links",',
        '  "risks": "1 paragraph",',
        '  "success_criteria": "bulleted list as a single string with newlines",',
        '  "sources": ["https://...", "..."]',
        "}]}",
        "```",
        "",
        f"## App: {ctx['app_name']}",
        "",
        "### README (excerpt)",
        ctx["readme"] or "(missing)",
        "",
    ]
    if ctx["claude_md"]:
        parts += ["### CLAUDE.md / AGENTS.md", ctx["claude_md"], ""]
    if ctx["top_level"]:
        parts += [
            "### Top-level layout",
            "```",
            "\n".join(ctx["top_level"]),
            "```",
            "",
        ]
    if ctx["git_log"]:
        parts += [
            f"### Recent commits (last {len(ctx['git_log'])})",
            "```",
            "\n".join(ctx["git_log"]),
            "```",
            "",
        ]
    if ctx["todos"]:
        parts += [
            f"### TODO/FIXME items in source ({len(ctx['todos'])} found)",
            "```",
            "\n".join(ctx["todos"]),
            "```",
            "",
        ]
    if ctx["existing_specs"]:
        parts += [
            "### Existing spec proposals in docs/product-owner/",
            "(Don't duplicate these; build on them or steer clear.)",
            "```",
            "\n".join(ctx["existing_specs"]),
            "```",
            "",
        ]
    if feedback:
        parts += [
            "### Operator feedback (from Telegram in the last few hours)",
            "(Weight this HEAVILY — these are real pain points / asks.)",
            "```",
            "\n\n---\n\n".join(feedback),
            "```",
            "",
        ]
    parts += [
        "Now call web_search as needed to research the space, then output",
        f"the fenced JSON block with exactly {max_specs} specs (or fewer if",
        "you genuinely can't find that many worthwhile ones).",
    ]
    return "\n".join(parts)


async def request_claude_specs(
    api_key: str,
    ctx: dict,
    feedback: list[str],
    max_specs: int,
    model: str = DEFAULT_MODEL,
    web_search_max_uses: int = 5,
) -> tuple[list[dict], Optional[str]]:
    """Call Claude with web_search enabled. Returns (specs, error). On
    failure specs is [] and error is a one-line description."""
    body = {
        "model": model,
        "max_tokens": 16000,
        "messages": [
            {"role": "user", "content": _build_user_prompt(ctx, feedback, max_specs)}
        ],
        "tools": [{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": web_search_max_uses,
        }],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=600) as client:
            r = await client.post(ANTHROPIC_API_URL, json=body, headers=headers)
        if r.status_code != 200:
            return [], f"anthropic http {r.status_code}: {r.text[:300]}"
        data = r.json()
    except Exception as e:
        return [], f"anthropic call raised: {e!r}"

    # Collect any text blocks (a web_search-enabled response may have
    # interleaved tool_use / tool_result blocks; we want the model's final
    # natural-language output).
    text_parts: list[str] = []
    for block in data.get("content") or []:
        if block.get("type") == "text":
            text_parts.append(block.get("text") or "")
    text = "\n".join(text_parts).strip()
    if not text:
        return [], "anthropic returned no text content"

    # Find the first fenced JSON block — be liberal in what we accept
    # (```json, ``` json, or even a top-level JSON object).
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    raw = m.group(1) if m else text
    try:
        obj = json.loads(raw)
    except Exception as e:
        return [], f"could not parse JSON: {e!r}"

    specs = obj.get("specs") if isinstance(obj, dict) else None
    if not isinstance(specs, list):
        return [], "JSON had no 'specs' list"

    cleaned: list[dict] = []
    for s in specs:
        if not isinstance(s, dict):
            continue
        title = (s.get("title") or "").strip()
        if not title:
            continue
        slug = (s.get("slug") or "").strip() or _slugify(title)
        cleaned.append({
            "title": title,
            "slug": slug,
            "complexity": (s.get("complexity") or "M").strip().upper()[:1],
            "problem": (s.get("problem") or "").strip(),
            "proposed_solution": (s.get("proposed_solution") or "").strip(),
            "why_now": (s.get("why_now") or "").strip(),
            "risks": (s.get("risks") or "").strip(),
            "success_criteria": (s.get("success_criteria") or "").strip(),
            "sources": [str(x) for x in (s.get("sources") or []) if x],
        })
    return cleaned, None


_SLUG_NON = re.compile(r"[^a-z0-9-]+")
_SLUG_DASHES = re.compile(r"-{2,}")


def _slugify(text: str) -> str:
    s = text.lower().strip().replace(" ", "-")
    s = _SLUG_NON.sub("-", s)
    s = _SLUG_DASHES.sub("-", s).strip("-")
    return (s or "untitled")[:60]


# ----- spec file writing + git push -----

def _spec_markdown(spec: dict, app_name: str, date_str: str) -> str:
    return (
        "---\n"
        f"title: {spec['title']}\n"
        f"app: {app_name}\n"
        f"date: {date_str}\n"
        f"complexity: {spec['complexity'] or 'M'}\n"
        "status: proposed\n"
        f"slug: {spec['slug']}\n"
        "---\n\n"
        f"# {spec['title']}\n\n"
        "## Problem\n\n"
        f"{spec['problem'] or '_(none provided)_'}\n\n"
        "## Proposed solution\n\n"
        f"{spec['proposed_solution'] or '_(none provided)_'}\n\n"
        "## Why now\n\n"
        f"{spec['why_now'] or '_(none provided)_'}\n\n"
        "## Risks / unknowns\n\n"
        f"{spec['risks'] or '_(none provided)_'}\n\n"
        "## Success criteria\n\n"
        f"{spec['success_criteria'] or '_(none provided)_'}\n\n"
        "## Sources\n\n"
        + (
            "\n".join(f"- {url}" for url in spec["sources"])
            if spec["sources"] else "_(none)_"
        )
        + "\n"
    )


def _authed_url(url: str, token: Optional[str]) -> str:
    """Same shape as git_ops._authed_url — kept here to avoid importing
    the whole handlers package from product_owner (which would create an
    import cycle once handlers/product_owner_admin.py exists)."""
    if not token:
        return url
    p = urlparse(url)
    if not p.scheme.startswith("http"):
        return url
    netloc = f"x-access-token:{token}@{p.hostname}"
    if p.port:
        netloc += f":{p.port}"
    return p._replace(netloc=netloc).geturl()


def _git_env() -> dict:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"
    return env


def _run_git(args: list[str], cwd: Path, env: Optional[dict] = None,
             timeout: int = 60) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            env=env or _git_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout + proc.stderr)
    except Exception as e:
        return -1, repr(e)


def write_specs_to_branch(
    repo_dir: Path,
    repo_name: str,
    specs: list[dict],
    app_name: str,
    date_str: str,
) -> tuple[Optional[str], Optional[str]]:
    """Create a clean branch off the current HEAD, write each spec as a
    markdown file, commit them, push. Returns (branch_name, error)."""
    if not specs:
        return None, "no specs to write"
    branch_name = f"product-owner/{date_str}"

    # Capture the current branch so we can return to it.
    rc, out = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_dir)
    if rc != 0:
        return None, f"rev-parse failed: {out[:200]}"
    original_branch = out.strip() or "main"

    # If the branch already exists (a previous run today), append a numeric
    # suffix so we don't clobber unmerged work.
    branch = branch_name
    rc, _ = _run_git(["rev-parse", "--verify", branch], repo_dir)
    if rc == 0:
        for i in range(2, 10):
            candidate = f"{branch_name}-{i}"
            rc, _ = _run_git(["rev-parse", "--verify", candidate], repo_dir)
            if rc != 0:
                branch = candidate
                break
        else:
            return None, "too many product-owner branches today"

    rc, out = _run_git(["checkout", "-b", branch], repo_dir)
    if rc != 0:
        return None, f"branch create failed: {out[:200]}"

    spec_dir = repo_dir / "docs" / "product-owner"
    try:
        spec_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        _run_git(["checkout", original_branch], repo_dir)
        return None, f"could not create spec dir: {e!r}"

    written: list[str] = []
    try:
        for spec in specs:
            fname = f"{date_str}-{spec['slug']}.md"
            (spec_dir / fname).write_text(
                _spec_markdown(spec, app_name, date_str),
                encoding="utf-8",
            )
            written.append(f"docs/product-owner/{fname}")
    except Exception as e:
        _run_git(["checkout", "--", "."], repo_dir)
        _run_git(["checkout", original_branch], repo_dir)
        return None, f"writing spec file raised: {e!r}"

    rc, out = _run_git(["add", *written], repo_dir)
    if rc != 0:
        _run_git(["checkout", original_branch], repo_dir)
        return None, f"git add failed: {out[:200]}"

    titles = "; ".join(s["title"] for s in specs)[:140]
    msg = f"product-owner: {len(specs)} spec proposals\n\n{titles}\n"
    # Use environment-supplied author so the commit shows as the agent,
    # not whichever Windows account git is currently configured with.
    env = _git_env()
    env["GIT_AUTHOR_NAME"] = "claude-agent product-owner"
    env["GIT_AUTHOR_EMAIL"] = "claude-agent@pilothouse.ltd"
    env["GIT_COMMITTER_NAME"] = env["GIT_AUTHOR_NAME"]
    env["GIT_COMMITTER_EMAIL"] = env["GIT_AUTHOR_EMAIL"]
    rc, out = _run_git(["commit", "-m", msg], repo_dir, env=env)
    if rc != 0:
        _run_git(["checkout", original_branch], repo_dir)
        return None, f"commit failed: {out[:200]}"

    # Push using the registered token. We DO NOT modify origin's URL
    # in-place the way git_ops does (we restore afterwards) so a half-failed
    # push doesn't leak a credential into .git/config.
    entry = repo_store.get(repo_name)
    if entry is None or not entry.token:
        # Try anyway — works for public repos / SSH-configured remotes
        rc, out = _run_git(["push", "-u", "origin", branch], repo_dir, timeout=180)
    else:
        auth_url = _authed_url(entry.url, entry.token)
        rc, _ = _run_git(["remote", "set-url", "origin", auth_url], repo_dir)
        if rc != 0:
            _run_git(["checkout", original_branch], repo_dir)
            return None, "could not set authed remote"
        try:
            push_rc, push_out = _run_git(
                ["push", "-u", "origin", branch], repo_dir, timeout=180,
            )
            rc, out = push_rc, push_out
        finally:
            _run_git(["remote", "set-url", "origin", entry.url], repo_dir)
    if rc != 0:
        # Push failed; leave the branch in place locally so the operator
        # can investigate, but flip back to original branch so the next
        # supervisor cycle isn't on this work branch.
        _run_git(["checkout", original_branch], repo_dir)
        return None, f"git push failed: {out[:200]}"

    # All good — return to the original branch so the supervisor's normal
    # git_pull cycle isn't disturbed.
    _run_git(["checkout", original_branch], repo_dir)
    return branch, None


# ----- main entry -----

async def run_review(
    runner,
    rt,
    repo_dir: Path,
    cfg: dict,
    log_line,
) -> dict:
    """Top-level orchestration. Returns a dict matching what
    app_store.record_product_owner_run wants: {result, message,
    branch, spec_count}.
    """
    name = rt.record.name
    repo_name = rt.record.repo_name
    notify_telegram = bool(cfg.get("notify_telegram", True))
    feedback_window_hours = max(0, int(cfg.get("feedback_window_hours", 6)))
    max_specs = max(1, min(10, int(cfg.get("max_specs", 3))))

    api_key = load_anthropic_token()
    if not api_key:
        msg = (
            f"no Anthropic API key at {ANTHROPIC_TOKEN_PATH}; "
            "create the file with the key on a single line, "
            "ACL: SYSTEM:F + Administrators:F"
        )
        log_line(f"--- product-owner: {msg} ---")
        return {"result": "no_api_key", "message": msg,
                "branch": None, "spec_count": 0}

    log_line(f"--- product-owner: gathering context for {name} ---")
    ctx = gather_context(repo_dir, name)
    log_line(
        f"--- product-owner: README {len(ctx['readme'])}B, "
        f"{len(ctx['todos'])} TODOs, {len(ctx['git_log'])} commits, "
        f"{len(ctx['existing_specs'])} existing specs ---"
    )

    feedback: list[str] = []
    tg = getattr(runner, "telegram", None)
    if notify_telegram and feedback_window_hours > 0 and tg is not None:
        feedback = await _collect_feedback(
            tg, name, feedback_window_hours, log_line,
        )
    elif feedback_window_hours == 0:
        log_line("--- product-owner: feedback window is 0, skipping Telegram ---")
    else:
        log_line("--- product-owner: Telegram unavailable, skipping feedback ---")

    log_line(
        f"--- product-owner: calling Claude (max_specs={max_specs}, "
        f"feedback={len(feedback)} replies) ---"
    )
    specs, err = await request_claude_specs(api_key, ctx, feedback, max_specs)
    if err:
        log_line(f"--- product-owner: LLM call failed: {err} ---")
        return {"result": "llm_failed", "message": err,
                "branch": None, "spec_count": 0}
    if not specs:
        msg = "Claude returned no specs"
        log_line(f"--- product-owner: {msg} ---")
        return {"result": "no_specs", "message": msg,
                "branch": None, "spec_count": 0}

    date_str = time.strftime("%Y-%m-%d", time.gmtime())
    log_line(f"--- product-owner: writing {len(specs)} specs to branch ---")
    branch, err = write_specs_to_branch(
        repo_dir, repo_name, specs, name, date_str,
    )
    if err:
        log_line(f"--- product-owner: git step failed: {err} ---")
        return {"result": "git_failed", "message": err,
                "branch": branch, "spec_count": len(specs)}

    if notify_telegram and tg is not None:
        await _broadcast_summary(tg, name, specs, branch)
    return {"result": "ok", "message": f"branch {branch}",
            "branch": branch, "spec_count": len(specs)}


async def _collect_feedback(tg, app_name: str, window_hours: int, log_line) -> list[str]:
    """Post a question to allowlisted users, wait window_hours, return
    their replies. Closes early if any operator sends /po_done."""
    if not getattr(tg, "application", None):
        return []
    ids = list(getattr(tg, "allowlist", set()) or set())
    if not ids:
        log_line("--- product-owner: no Telegram allowlist; skipping feedback ---")
        return []

    question = (
        f"🎯 Product-owner review for *{app_name}* is starting.\n\n"
        f"Reply to this message in the next {window_hours}h with any pain "
        f"points, wishlist items, or bugs you want me to weight when "
        f"researching new specs. Send /po_done to close the window early."
    )

    # Track this collection on the bot. Most-recent question id per app.
    # The bot's reply handler is registered lazily below.
    if not hasattr(tg, "po_collections"):
        tg.po_collections = {}
    if not hasattr(tg, "po_done_event"):
        tg.po_done_event = {}
    if not hasattr(tg, "_po_handler_registered"):
        _install_telegram_po_handlers(tg)
        tg._po_handler_registered = True

    sent_ids: list[tuple[int, int]] = []  # (chat_id, message_id)
    for uid in ids:
        try:
            msg = await tg.application.bot.send_message(
                chat_id=uid, text=question, parse_mode="Markdown",
            )
            sent_ids.append((uid, msg.message_id))
            tg.po_collections[msg.message_id] = {
                "app": app_name,
                "replies": [],
                "ends_at": time.time() + window_hours * 3600,
            }
        except Exception:
            log.exception("po: send_message to %s failed", uid)

    if not sent_ids:
        return []

    done_evt = asyncio.Event()
    tg.po_done_event[app_name] = done_evt

    try:
        await asyncio.wait_for(done_evt.wait(), timeout=window_hours * 3600)
        log_line(f"--- product-owner: feedback closed early by /po_done ---")
    except asyncio.TimeoutError:
        log_line(f"--- product-owner: feedback window of {window_hours}h elapsed ---")

    replies: list[str] = []
    for _uid, mid in sent_ids:
        coll = tg.po_collections.pop(mid, None)
        if coll:
            replies.extend(coll.get("replies") or [])
    tg.po_done_event.pop(app_name, None)
    return replies


async def _broadcast_summary(tg, app_name: str, specs: list[dict], branch: str) -> None:
    if not getattr(tg, "application", None):
        return
    lines = [f"🎯 product-owner @ *{app_name}*: {len(specs)} new proposals"]
    for s in specs:
        lines.append(f"• {s['title']} ({s.get('complexity', 'M')})")
    lines.append(f"\nBranch: `{branch}`")
    text = "\n".join(lines)
    for uid in list(getattr(tg, "allowlist", set()) or set()):
        try:
            await tg.application.bot.send_message(
                chat_id=uid, text=text, parse_mode="Markdown",
            )
        except Exception:
            log.exception("po: broadcast to %s failed", uid)


def _install_telegram_po_handlers(tg) -> None:
    """Late-bind a message handler that captures replies-to-our-question
    and a /po_done command that closes any open collection.

    The bot uses python-telegram-bot's Application; we add to its handler
    list directly. Doing this lazily avoids a hard dependency from the
    bot module on the product-owner one (the bot module never has to
    know this module exists)."""
    try:
        from telegram.ext import CommandHandler, MessageHandler, filters
    except Exception:
        log.exception("po: telegram.ext import failed")
        return
    app = tg.application
    if app is None:
        return

    async def on_reply(update, _ctx):
        msg = update.effective_message
        if msg is None or msg.reply_to_message is None:
            return
        original_id = msg.reply_to_message.message_id
        coll = tg.po_collections.get(original_id) if hasattr(tg, "po_collections") else None
        if coll is None:
            return
        if time.time() > coll["ends_at"]:
            return
        coll["replies"].append((msg.text or msg.caption or "").strip())
        try:
            await msg.reply_text("noted ✓")
        except Exception:
            pass

    async def on_po_done(update, _ctx):
        # Close ALL open collections — operator is signalling "go think".
        events = getattr(tg, "po_done_event", {}) or {}
        if not events:
            try:
                await update.effective_message.reply_text(
                    "no active product-owner window"
                )
            except Exception:
                pass
            return
        for evt in events.values():
            evt.set()
        try:
            await update.effective_message.reply_text(
                f"closed {len(events)} active window(s)"
            )
        except Exception:
            pass

    try:
        app.add_handler(CommandHandler("po_done", on_po_done))
        app.add_handler(MessageHandler(
            filters.TEXT & filters.REPLY & ~filters.COMMAND, on_reply,
        ))
    except Exception:
        log.exception("po: add_handler failed")
