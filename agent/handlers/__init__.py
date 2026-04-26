"""Command handlers. Each handler returns a Reply (optionally with job_id)."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from shared.protocol import Command, Reply

from . import (
    app_admin, files, git_ops, host, machine, ollama, repo_admin, self_update,
    services, status, telegram_admin,
)

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
    "register_app": app_admin.handle_register_app,
    "unregister_app": app_admin.handle_unregister_app,
    "start_app": app_admin.handle_start_app,
    "stop_app": app_admin.handle_stop_app,
    "restart_app": app_admin.handle_restart_app,
    "list_apps": app_admin.handle_list_apps,
    "app_logs": app_admin.handle_app_logs,
    "host_restart": host.handle_host_restart,
    "host_cancel_restart": host.handle_host_cancel_restart,
    "write_file": files.handle_write_file,
    "read_file": files.handle_read_file,
    "delete_file": files.handle_delete_file,
    "list_dir": files.handle_list_dir,
    "tail_logs": files.handle_tail_logs,
    "machine_stats": machine.handle_machine_stats,
    "telegram_allow": telegram_admin.handle_telegram_allow,
    "telegram_revoke": telegram_admin.handle_telegram_revoke,
    "telegram_reload": telegram_admin.handle_telegram_reload,
    "telegram_status": telegram_admin.handle_telegram_status,
    "service_restart": services.handle_service_restart,
    "ollama_list": ollama.handle_list,
    "ollama_pull": ollama.handle_pull,
    "ollama_ps": ollama.handle_ps,
    "ollama_stop": ollama.handle_stop,
    "self_update": self_update.handle_self_update,
}
