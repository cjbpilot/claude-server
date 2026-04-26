"""Command handlers. Each handler returns a Reply (optionally with job_id)."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from shared.protocol import Command, Reply

from . import git_ops, ollama, repo_admin, self_update, services, status

Handler = Callable[["HandlerCtx", Command], Awaitable[Reply]]


class HandlerCtx:
    """Carries shared state into handlers without a circular import."""

    def __init__(self, cfg, nc, runner):
        self.cfg = cfg
        self.nc = nc
        self.runner = runner


REGISTRY: dict[str, Handler] = {
    "status": status.handle_status,
    "list_repos": status.handle_list_repos,
    "list_deploys": status.handle_list_deploys,
    "list_services": status.handle_list_services,
    "git_pull": git_ops.handle_git_pull,
    "run_deploy": git_ops.handle_run_deploy,
    "register_repo": repo_admin.handle_register_repo,
    "unregister_repo": repo_admin.handle_unregister_repo,
    "update_repo_token": repo_admin.handle_update_repo_token,
    "service_restart": services.handle_service_restart,
    "ollama_list": ollama.handle_list,
    "ollama_pull": ollama.handle_pull,
    "ollama_ps": ollama.handle_ps,
    "ollama_stop": ollama.handle_stop,
    "self_update": self_update.handle_self_update,
}
