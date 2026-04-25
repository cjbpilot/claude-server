"""Out-of-process updater. Lives inside the install dir but runs detached
from the agent process so it can restart the service that's hosting it.

It writes a JSON report to UPDATE_REPORT_PATH when done. The fresh agent
reads that file on startup and publishes an 'updated' event.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPORT_PATH = Path(r"C:\ProgramData\ClaudeAgent\last_update.json")


def log(line: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{ts}] {line}", flush=True)


def wait_for_pid_exit(pid: int, timeout_s: int = 60) -> bool:
    import psutil

    try:
        p = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    try:
        p.wait(timeout=timeout_s)
        return True
    except psutil.TimeoutExpired:
        return False


def run(cmd: list[str], cwd: str, timeout: int = 600) -> tuple[int, str]:
    log(f"$ {' '.join(cmd)}  (cwd={cwd})")
    try:
        out = subprocess.run(
            cmd, cwd=cwd, timeout=timeout, capture_output=True, text=True
        )
        log(out.stdout.strip())
        if out.stderr:
            log(f"stderr: {out.stderr.strip()}")
        return out.returncode, (out.stdout + out.stderr)
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def write_report(report: dict) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--install-dir", required=True)
    ap.add_argument("--service", required=True)
    ap.add_argument("--python", required=True)
    ap.add_argument("--job-id", required=True)
    args = ap.parse_args()

    install_dir = args.install_dir
    started = time.time()
    report: dict = {
        "job_id": args.job_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ok": False,
        "steps": [],
    }

    def step(name: str, rc: int, output: str) -> None:
        report["steps"].append({"name": name, "rc": rc, "output": output[-4000:]})

    task = args.service

    def end_task():
        return run(["schtasks", "/End", "/TN", task], cwd=install_dir, timeout=60)

    def run_task():
        return run(["schtasks", "/Run", "/TN", task], cwd=install_dir, timeout=60)

    log(f"waiting for agent pid {args.pid} to exit ...")
    if not wait_for_pid_exit(args.pid, timeout_s=60):
        log("agent did not exit; stopping scheduled task")
        rc, out = end_task()
        step("task_end", rc, out)
        time.sleep(3)

    rc_rev, pre_rev = run(["git", "rev-parse", "HEAD"], cwd=install_dir)
    pre_rev = pre_rev.strip()
    step("pre_rev", rc_rev, pre_rev)

    rc, out = run(["git", "fetch", "--all", "--prune"], cwd=install_dir)
    step("git_fetch", rc, out)
    if rc != 0:
        report["error"] = "git fetch failed"
        write_report(report)
        run_task()
        return 1

    rc_branch, branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=install_dir)
    branch = branch.strip() or "main"
    step("branch", rc_branch, branch)

    rc, out = run(["git", "reset", "--hard", f"origin/{branch}"], cwd=install_dir)
    step("git_reset", rc, out)
    if rc != 0:
        report["error"] = f"git reset --hard origin/{branch} failed"
        write_report(report)
        run_task()
        return 1

    rc, out = run(["git", "clean", "-fd"], cwd=install_dir)
    step("git_clean", rc, out)

    rc_post, post_rev = run(["git", "rev-parse", "HEAD"], cwd=install_dir)
    post_rev = post_rev.strip()
    step("post_rev", rc_post, post_rev)
    report["pre_rev"] = pre_rev
    report["post_rev"] = post_rev

    req = str(Path(install_dir) / "agent" / "requirements.txt")
    rc, out = run([args.python, "-m", "pip", "install", "-r", req], cwd=install_dir, timeout=600)
    step("pip_install", rc, out)
    if rc != 0:
        log("pip install failed; rolling back")
        run(["git", "reset", "--hard", pre_rev], cwd=install_dir)
        report["error"] = "pip install failed, rolled back"
        write_report(report)
        run_task()
        return 1

    rc, out = run_task()
    step("task_run", rc, out)

    report["ok"] = rc == 0
    report["duration_s"] = round(time.time() - started, 1)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_report(report)
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
