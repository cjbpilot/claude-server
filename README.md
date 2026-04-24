# claude-server

A remote-control agent for a Windows desktop driven by Claude Code / Cowork.
No inbound ports, no VPN — both sides dial out to a shared NATS account
(Synadia Cloud free tier works) and exchange JSON messages.

## What you can do from Claude

- `status` — CPU/RAM/disk/Ollama health + agent version
- `list_repos`, `list_deploys`, `list_services`
- `git_pull <repo>` — update a registered checkout (streams log)
- `run_deploy <name>` — run a registered deploy script (streams log)
- `service_restart <name>` — restart an allowlisted Windows service
- `ollama_list`, `ollama_ps`, `ollama_pull <model>`, `ollama_stop <model>`
- `self_update` — the agent pulls its own latest code, reinstalls deps, and
  restarts itself

Everything touched is allowlisted in `agent.toml`; there is no arbitrary
shell tool exposed.

## Architecture

```
┌─────────────────────────┐           ┌───────────────────────────┐
│   Claude Code / Cowork  │           │  Windows desktop          │
│   mcp_plugin.server     │           │  ClaudeAgent Windows svc  │
│   (stdio MCP)           │           │  (pywin32 + asyncio)      │
└────────────┬────────────┘           └────────────┬──────────────┘
             │                                     │
             │        outbound TLS (4222)          │
             └──────────────┬──────────────────────┘
                            ▼
                ┌─────────────────────────┐
                │   Synadia Cloud (NATS)  │
                │   JWT+NKey auth         │
                └─────────────────────────┘
```

Subjects:

    agent.<host>.cmd.<tool>         request/reply
    agent.<host>.jobs.<id>.log      streamed stdout/stderr
    agent.<host>.jobs.<id>.done     job terminal status
    agent.<host>.status             periodic heartbeat
    agent.<host>.events             lifecycle (starting, updated, ...)

## Repo layout

    agent/              Windows service, runner, handlers, updater
    mcp_plugin/         Claude Code MCP server
    shared/             Wire protocol + subject helpers (used by both)
    install/            PowerShell install/uninstall/manual-update scripts
    SETUP.md            Step-by-step, read this first

## Setup

See [SETUP.md](SETUP.md).
