# Claude Agent — Setup

Step-by-step setup for a Windows desktop controlled by Claude Code via NATS
(Synadia Cloud). Two sides to configure:

- **A. Synadia Cloud** — the message broker both sides dial into.
- **B. Windows desktop** — installs the agent as a service.
- **C. Claude Code machine** — registers the MCP plugin.

Work top to bottom the first time.

---

## A. Synadia Cloud (one-time)

1. Sign up at <https://cloud.synadia.com> (free tier is fine).
2. Create an **Account** (any name — e.g. `home`).
3. Create a **User** inside that account — e.g. `claude-agent`. Both the
   Windows agent and the Claude Code plugin will use this **same user**, so it
   needs publish/subscribe on `agent.>`.
4. Download the user's **creds file**. It looks like `claude-agent.creds` and
   contains a JWT + NKey seed. **Treat it like a password.**
5. Note the NATS URL. For Synadia NGS this is:

       wss://connect.ngs.global:443

   (If you're using a different NATS host, use its URL.)

You now have two things to carry to the other steps:

- `claude-agent.creds`
- `wss://connect.ngs.global:443`

---

## B. Windows desktop (the server)

### B1. Prerequisites

Install these once, as an admin:

- **Python 3.11+** — <https://www.python.org/downloads/windows/>. Tick "Add
  python.exe to PATH" during install.
- **Git for Windows** — <https://git-scm.com/download/win>. Required for
  `git_pull` and `self_update`.
- **Ollama** — <https://ollama.com/download>. After install, open
  PowerShell and run `ollama --version` to confirm.

Optional: pull a model so you can test. From any shell:

    ollama pull llama3.1:8b

### B2. Clone the agent repo

As an **Administrator** PowerShell:

    git clone https://github.com/<you>/claude-server.git C:\ClaudeAgent\src
    cd C:\ClaudeAgent\src

> The install script copies the repo to `C:\ClaudeAgent\app`. Keeping the
> clone at `C:\ClaudeAgent\src` is optional — the `app` directory is what
> becomes the live install and what `self_update` pulls into.

### B3. Copy the creds file

Copy the creds file from step A4 to somewhere you can point the installer at.
A temp location is fine:

    Copy-Item C:\Users\<you>\Downloads\claude-agent.creds C:\Temp\nats.creds

### B4. Run the installer

Still in an elevated PowerShell, from the repo root:

    .\install\install-agent.ps1 `
        -CredsFile C:\Temp\nats.creds `
        -HostId    my-desktop `
        -NatsUrl   wss://connect.ngs.global:443

What this does:

- Copies the repo to `C:\ClaudeAgent\app`.
- Creates a Python venv at `C:\ClaudeAgent\venv`.
- Installs dependencies and the agent package (editable).
- Seeds `C:\ProgramData\ClaudeAgent\agent.toml` from the example and locks
  down `nats.creds` so only SYSTEM/Admins can read it.
- Registers the `ClaudeAgent` Scheduled Task: runs at boot as SYSTEM with
  highest privileges, restarts automatically on crash, no user login needed.
- Starts the task.

Check it's running:

    Get-ScheduledTask -TaskName ClaudeAgent
    Get-Content C:\ProgramData\ClaudeAgent\logs\agent.log -Tail 20

You should see a line like:

    connecting to wss://connect.ngs.global:443 as host=my-desktop version=0.1.0

### B5. Configure what the agent is allowed to touch

Edit `C:\ProgramData\ClaudeAgent\agent.toml`. By default, no repos, deploys,
or services are registered — the agent will run but every command will be
rejected as "unknown".

Minimal example:

```toml
[repos.my-app]
path = "my-app"
remote = "https://github.com/you/my-app.git"
branch = "main"

[deploys.my-app-deploy]
cwd = "my-app"
cmd = "pwsh -NoProfile -ExecutionPolicy Bypass -File ./deploy.ps1"
timeout_s = 600

[[allowed_services]]
name = "Ollama"
```

After editing, restart the service:

    Restart-Service ClaudeAgent

### B6. Enable `self_update` (important)

`self_update` only works if `C:\ClaudeAgent\app` is a **git checkout** pointed
at your remote. From an elevated PowerShell:

    cd C:\ClaudeAgent\app
    git init
    git remote add origin https://github.com/<you>/claude-server.git
    git fetch origin
    git reset --hard origin/main

After that, any time you push new agent code to `main`, invoke `self_update`
from Claude Code and the Windows machine will pull, reinstall deps, and
restart the service on its own.

### B7. Registering private repos (preferred path)

The cleanest way to give the agent access to a private repo is to register
it dynamically from Claude Code with a per-repo PAT — no static config
needed. The agent stores the URL, branch, and token in
`C:\ProgramData\ClaudeAgent\repos.json` (ACL: SYSTEM:F + Administrators:F).
The token is injected into `git` via a per-process env var so it never
appears in argv, never lands in `.git/config`, and never reaches any log
or NATS reply.

In Claude Code:

    > use the claude-agent MCP, register_repo with:
    >   name = my-app
    >   url = https://github.com/you/my-app.git
    >   token = ghp_xxxxxxxxxxxxxxxx
    >   branch = main

To rotate or clear the token:

    > update_repo_token my-app ghp_yyyyyyyyyyyyyyyy
    > update_repo_token my-app null    # clears (e.g. repo became public)

To remove a repo from the registry (does NOT delete the on-disk checkout):

    > unregister_repo my-app

Generate a fine-grained PAT at <https://github.com/settings/tokens?type=beta>
with **Contents: Read** on the specific repo(s). Set an expiry. Treat the
token like a password.

Public repos don't need a token — register with `token = null` (or omit it).

### B7-alt. Static config (legacy)

You can still pre-declare repos in `agent.toml` (see B5). Static entries
work for public repos but have no place to store a token. If a repo with
the same name exists in both places, the dynamic registry wins.
- Use a deploy key / HTTPS token baked into the remote URL in `agent.toml`.

---

## C. Claude Code machine (the client)

### C1. Prerequisites

- **Python 3.11+**.
- Claude Code installed (`claude --version`).

### C2. Clone the same repo (or a subset)

You only need `mcp_plugin/` and `shared/` on the client side, but cloning
the whole thing is easiest:

    git clone https://github.com/<you>/claude-server.git ~/code/claude-server
    cd ~/code/claude-server

### C3. Install the plugin's deps

    python3 -m venv .venv
    source .venv/bin/activate       # Windows: .venv\Scripts\activate
    pip install -r mcp_plugin/requirements.txt

### C4. Put the creds file somewhere

Copy `claude-agent.creds` from step A4 to a local path. Keep it outside the
repo so you don't accidentally commit it.

    mkdir -p ~/.config/claude-agent
    cp /path/to/claude-agent.creds ~/.config/claude-agent/nats.creds
    chmod 600 ~/.config/claude-agent/nats.creds

### C5. Register the MCP server with Claude Code

From the `claude-server` directory:

    claude mcp add claude-agent \
        -e CLAUDE_AGENT_HOST_ID=my-desktop \
        -e CLAUDE_AGENT_NATS_URL=wss://connect.ngs.global:443 \
        -e CLAUDE_AGENT_CREDS=$HOME/.config/claude-agent/nats.creds \
        -- $PWD/.venv/bin/python -m mcp_plugin.server

> On Windows client machines, swap `$PWD/.venv/bin/python` for
> `%CD%\.venv\Scripts\python.exe`.

Verify:

    claude mcp list

You should see `claude-agent` listed.

### C6. Try it

In Claude Code:

    > use the claude-agent mcp: run the status tool

Expected JSON includes your host id, version, CPU/RAM/disk, Ollama health,
and the lists of registered repos/deploys/services.

Then try a real flow:

    > git_pull my-app
    > run_deploy my-app-deploy
    > ollama_list

---

## D. Updating the agent remotely

From Claude Code:

    > self_update

What happens:

1. Agent receives the command, replies immediately with a `job_id`.
2. Agent spawns `updater.py` detached and exits.
3. Windows Service Manager restarts the service.
4. `updater.py` waits for the old PID to die, runs `git pull`, `pip install`,
   then `sc start ClaudeAgent`.
5. Fresh agent reconnects, publishes an `updated` event on
   `agent.my-desktop.events`, and includes the new version in its next
   heartbeat.

Expect a 10–30s gap where `status` times out. Run it again and you'll see
the new version number.

If an update ever fails:

- The updater rolls back to the pre-update commit if `pip install` fails.
- Check `C:\ProgramData\ClaudeAgent\last_update.json` on the desktop for the
  full step-by-step report.
- Fall back to the manual path: `install\update-agent.ps1` from an elevated
  PowerShell on the desktop.

---

## E. Troubleshooting

**Agent won't start.** Check `C:\ProgramData\ClaudeAgent\logs\agent.log`. The
most common causes:

- `agent.toml` not found → installer didn't finish; rerun it.
- `nats.creds` bad path or wrong permissions → SYSTEM needs read.
- NATS connect failure → confirm the creds file is for the right account and
  the URL matches.

**`status` times out from Claude.** Two layers to check:

- `Get-Service ClaudeAgent` on the desktop. If it's not running, start it and
  watch `agent.log`.
- If it's running, either the plugin can't reach NATS, or `HOST_ID` doesn't
  match `agent.host_id`. Subjects are scoped per host, so a mismatch looks
  exactly like "agent is offline".

**`self_update` hangs.** The plugin returns fast with the spawn message. If
the agent never comes back:

- Log in to the desktop and check the Service status (`Get-Service
  ClaudeAgent`).
- `C:\ProgramData\ClaudeAgent\last_update.json` has the step log.
- Worst case: run `install\update-agent.ps1` manually.

**`run_deploy` fails with permission errors.** The service runs as
LocalSystem by default. Either change its Log On user (services.msc) to an
account that has the permissions your deploy needs, or make the deploy
script use `runas` / scheduled tasks for the elevated pieces.

**Ollama unreachable.** The default URL is `http://127.0.0.1:11434`. Make
sure Ollama is running (`Get-Service Ollama` if installed as a service, or
just look in the taskbar).
