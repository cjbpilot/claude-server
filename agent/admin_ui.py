"""Embedded admin UI: a small aiohttp server on port 7979 that exposes the
agent's key functions via a single HTML page + JSON endpoints.

Security model: bound to 0.0.0.0 by default. We expect Windows Firewall to
restrict TCP/7979 to the Tailscale adapter, same as Ollama. There's no
in-app auth - if the port is reachable, it's trusted. The page does not
expose secrets (tokens, NATS creds, file ops); it's a thin viewer + a few
guarded actions over the existing REGISTRY handlers.

Endpoints:
  GET  /                     - HTML page
  GET  /api/status           - status (JSON)
  GET  /api/apps             - list_apps (JSON)
  POST /api/apps/<name>/start
  POST /api/apps/<name>/stop
  POST /api/apps/<name>/restart
  GET  /api/apps/<name>/logs?lines=100
  GET  /api/repos            - list_repos
  POST /api/repos/<name>/pull
  GET  /api/services
  POST /api/services/<name>/restart
  GET  /api/machine?minutes=5
  GET  /api/watchdog
  GET  /api/ollama
  GET  /api/ollama/ps
  POST /api/host/restart?delay_s=30
  POST /api/host/cancel
  POST /api/self_update
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from aiohttp import web

from shared.protocol import Command

log = logging.getLogger("agent.admin_ui")

DEFAULT_PORT = 7979
HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>claude-agent admin - %HOST%</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif;
         margin: 0; padding: 20px; max-width: 1100px;
         background: #0f1419; color: #e6edf3; }
  h1 { margin: 0 0 4px; font-size: 22px; }
  .sub { color: #7d8590; font-size: 13px; margin-bottom: 24px; }
  section { background: #161b22; border: 1px solid #30363d;
            border-radius: 6px; padding: 14px 16px; margin-bottom: 16px; }
  section h2 { margin: 0 0 10px; font-size: 15px; color: #58a6ff; font-weight: 600; }
  .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
  .pill { background: #21262d; border: 1px solid #30363d; padding: 4px 10px;
          border-radius: 12px; font-size: 12px; }
  .pill.ok    { background: #103a1a; border-color: #1f6e34; }
  .pill.bad   { background: #4d1a1f; border-color: #8b2233; }
  .pill.warn  { background: #4a3a0a; border-color: #8b6f1a; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #21262d; }
  th { color: #7d8590; font-weight: 500; }
  button { background: #21262d; color: #e6edf3; border: 1px solid #30363d;
           border-radius: 4px; padding: 4px 10px; cursor: pointer; font-size: 12px; }
  button:hover { background: #30363d; }
  button.danger { color: #ffa198; border-color: #6e2d34; }
  button.danger:hover { background: #5a1f25; }
  pre { background: #0d1117; border: 1px solid #21262d; border-radius: 4px;
        padding: 10px; overflow-x: auto; font-size: 11px; line-height: 1.45;
        max-height: 360px; margin: 8px 0 0; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  @media (max-width: 700px) { .grid { grid-template-columns: 1fr; } }
  small { color: #7d8590; }
  .actions { display: flex; gap: 6px; flex-wrap: wrap; }
  .flash { padding: 8px 12px; border-radius: 4px; margin-bottom: 10px;
           font-size: 13px; display: none; }
  .flash.ok { background: #103a1a; border: 1px solid #1f6e34; display: block; }
  .flash.bad { background: #4d1a1f; border: 1px solid #8b2233; display: block; }
</style>
</head>
<body>
<h1>claude-agent admin <small id="hostName">%HOST%</small></h1>
<div class="sub">auto-refresh every 10s &middot; <a href="#" onclick="refresh();return false" style="color:#58a6ff">refresh now</a></div>

<div id="flash" class="flash"></div>

<section>
  <h2>Status</h2>
  <div id="statusBox" class="row">loading...</div>
</section>

<div class="grid">
  <section>
    <h2>Apps</h2>
    <div id="appsBox">loading...</div>
  </section>
  <section>
    <h2>Repos</h2>
    <div id="reposBox">loading...</div>
  </section>
</div>

<div class="grid">
  <section>
    <h2>Machine (5m window)</h2>
    <div id="machineBox">loading...</div>
  </section>
  <section>
    <h2>Watchdog</h2>
    <div id="watchdogBox">loading...</div>
  </section>
</div>

<div class="grid">
  <section>
    <h2>Ollama</h2>
    <div id="ollamaBox">loading...</div>
  </section>
  <section>
    <h2>Services</h2>
    <div id="servicesBox">loading...</div>
  </section>
</div>

<section>
  <h2>Host control</h2>
  <div class="actions">
    <button onclick="doPost('/api/self_update')">self_update agent</button>
    <button class="danger" onclick="if(confirm('Reboot host in 30s? (host_cancel will abort)'))doPost('/api/host/restart?delay_s=30')">host restart (30s)</button>
    <button onclick="doPost('/api/host/cancel')">cancel pending reboot</button>
  </div>
</section>

<section>
  <h2>App logs</h2>
  <div class="row">
    <select id="logApp"></select>
    <input id="logLines" type="number" value="50" min="1" max="2000" style="width:80px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;padding:4px;border-radius:4px">
    <button onclick="loadLogs()">tail</button>
  </div>
  <pre id="logsBox"></pre>
</section>

<script>
async function getJSON(url) {
  const r = await fetch(url);
  return r.json();
}
async function doPost(url) {
  const r = await fetch(url, {method: 'POST'});
  const j = await r.json();
  flash(j.ok ? 'OK' : ('Error: ' + (j.error || 'failed')), j.ok);
  setTimeout(refresh, 500);
  return j;
}
function flash(msg, ok) {
  const f = document.getElementById('flash');
  f.textContent = msg;
  f.className = 'flash ' + (ok ? 'ok' : 'bad');
  setTimeout(() => { f.className = 'flash'; }, 4000);
}
function pill(text, kind) {
  return '<span class="pill ' + (kind || '') + '">' + text + '</span>';
}
function fmtDuration(s) {
  if (s == null) return '-';
  s = Math.floor(s);
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s/60) + 'm ' + (s%60) + 's';
  if (s < 86400) return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
  return Math.floor(s/86400) + 'd ' + Math.floor((s%86400)/3600) + 'h';
}
async function loadStatus() {
  const j = await getJSON('/api/status');
  if (!j.ok) { document.getElementById('statusBox').textContent = j.error; return; }
  const d = j.data;
  const m = d.machine || {};
  const tg = d.telegram || {};
  document.getElementById('statusBox').innerHTML =
    pill('agent ' + d.version) +
    pill('uptime ' + fmtDuration(d.uptime_s)) +
    pill('CPU ' + m.cpu_pct + '%') +
    pill('RAM ' + m.ram_pct + '% (' + m.ram_used_gb + '/' + m.ram_total_gb + ' GB)') +
    pill('disk ' + m.disk_pct + '% (' + m.disk_free_gb + ' GB free)') +
    pill('ollama ' + (d.ollama_up ? 'up' : 'DOWN'), d.ollama_up ? 'ok' : 'bad') +
    pill('telegram ' + (tg.running ? 'up' : 'down') + ' (' + tg.allowlisted + ')', tg.running ? 'ok' : 'bad');
}
async function loadApps() {
  const j = await getJSON('/api/apps');
  if (!j.ok) { document.getElementById('appsBox').textContent = j.error; return; }
  const sel = document.getElementById('logApp');
  const cur = sel.value;
  sel.innerHTML = '';
  let html = '<table><thead><tr><th>name</th><th>state</th><th>pid</th><th>uptime</th><th>health</th><th></th></tr></thead><tbody>';
  for (const a of j.data) {
    const stateKind = a.alive ? 'ok' : 'bad';
    const stateText = a.alive ? 'alive' : ('down (rc=' + a.last_exit_code + ')');
    const healthKind = a.last_health === 'ok' ? 'ok' : (a.last_health === 'fail' ? 'bad' : '');
    html += '<tr>' +
      '<td>' + a.name + '</td>' +
      '<td>' + pill(stateText, stateKind) + '</td>' +
      '<td>' + (a.pid || '-') + '</td>' +
      '<td>' + fmtDuration(a.uptime_s) + '</td>' +
      '<td>' + (a.last_health ? pill(a.last_health, healthKind) : '-') + '</td>' +
      '<td class="actions">' +
        '<button onclick="doPost(\\'/api/apps/' + a.name + '/start\\')">start</button>' +
        '<button onclick="doPost(\\'/api/apps/' + a.name + '/stop\\')">stop</button>' +
        '<button onclick="doPost(\\'/api/apps/' + a.name + '/restart\\')">restart</button>' +
      '</td></tr>';
    const o = document.createElement('option');
    o.value = a.name; o.textContent = a.name;
    sel.appendChild(o);
  }
  html += '</tbody></table>';
  document.getElementById('appsBox').innerHTML = html;
  if (cur) sel.value = cur;
}
async function loadRepos() {
  const j = await getJSON('/api/repos');
  if (!j.ok) { document.getElementById('reposBox').textContent = j.error; return; }
  let html = '<table><thead><tr><th>name</th><th>branch</th><th>token</th><th></th></tr></thead><tbody>';
  for (const r of j.data) {
    html += '<tr>' +
      '<td>' + r.name + '</td>' +
      '<td>' + r.branch + '</td>' +
      '<td>' + (r.has_token ? pill('auth', 'ok') : '-') + '</td>' +
      '<td><button onclick="doPost(\\'/api/repos/' + r.name + '/pull\\')">pull</button></td>' +
      '</tr>';
  }
  html += '</tbody></table>';
  document.getElementById('reposBox').innerHTML = html;
}
async function loadMachine() {
  const j = await getJSON('/api/machine?minutes=5');
  if (!j.ok) { document.getElementById('machineBox').textContent = j.error; return; }
  const w = (j.data.window || {});
  const c = w.cpu_pct || {};
  const r = w.ram_pct || {};
  let html = '<table><tr><th></th><th>avg</th><th>p95</th><th>max</th></tr>' +
    '<tr><td>CPU %</td><td>' + (c.avg||'-') + '</td><td>' + (c.p95||'-') + '</td><td>' + (c.max||'-') + '</td></tr>' +
    '<tr><td>RAM %</td><td>' + (r.avg||'-') + '</td><td>' + (r.p95||'-') + '</td><td>' + (r.max||'-') + '</td></tr></table>';
  const tp = (j.data.top_processes || {}).by_cpu || [];
  if (tp.length) {
    html += '<small>Top CPU:</small><table>';
    for (const p of tp.slice(0,5)) {
      html += '<tr><td>' + p.cpu_pct + '%</td><td>' + p.name + '</td><td>pid ' + p.pid + '</td><td>' + p.ram_mb + ' MB</td></tr>';
    }
    html += '</table>';
  }
  document.getElementById('machineBox').innerHTML = html;
}
async function loadWatchdog() {
  const j = await getJSON('/api/watchdog');
  if (!j.ok) { document.getElementById('watchdogBox').textContent = j.error; return; }
  const t = j.data.totals || {};
  let html = pill('1h: ' + (t.last_1h||0) + ' kills') +
             pill('24h: ' + (t.last_24h||0)) +
             pill('7d: ' + (t.last_7d||0)) +
             pill('all-time: ' + (t.all_time||0));
  const k = j.data.recent_kills || [];
  if (k.length) {
    html += '<table style="margin-top:8px"><tr><th>when</th><th>pid</th><th>reason</th></tr>';
    for (const e of k.slice(-6).reverse()) {
      const d = new Date(e.ts * 1000);
      html += '<tr><td>' + d.toLocaleString() + '</td><td>' + (e.pid_killed||'-') + '</td><td>' + e.reason + '</td></tr>';
    }
    html += '</table>';
  } else {
    html += '<small style="display:block;margin-top:8px">no kills recorded</small>';
  }
  document.getElementById('watchdogBox').innerHTML = html;
}
async function loadOllama() {
  const lj = await getJSON('/api/ollama');
  const pj = await getJSON('/api/ollama/ps');
  let html = '<small>Installed:</small><br>';
  for (const m of ((lj.data || {}).models || [])) {
    html += pill(m.name + ' (' + (m.size/1024**3).toFixed(1) + ' GB)') + ' ';
  }
  html += '<br><small>Loaded:</small><br>';
  const loaded = ((pj.data || {}).models || []);
  if (!loaded.length) html += '<small>none</small>';
  for (const m of loaded) {
    html += pill(m.name, 'ok') + ' ';
  }
  document.getElementById('ollamaBox').innerHTML = html;
}
async function loadServices() {
  const j = await getJSON('/api/services');
  if (!j.ok) { document.getElementById('servicesBox').textContent = j.error; return; }
  if (!j.data.length) { document.getElementById('servicesBox').innerHTML = '<small>none allowlisted</small>'; return; }
  let html = '<table>';
  for (const s of j.data) {
    html += '<tr><td>' + s + '</td><td><button onclick="doPost(\\'/api/services/' + s + '/restart\\')">restart</button></td></tr>';
  }
  html += '</table>';
  document.getElementById('servicesBox').innerHTML = html;
}
async function loadLogs() {
  const name = document.getElementById('logApp').value;
  const lines = document.getElementById('logLines').value;
  if (!name) return;
  const j = await getJSON('/api/apps/' + name + '/logs?lines=' + lines);
  if (!j.ok) { document.getElementById('logsBox').textContent = j.error; return; }
  document.getElementById('logsBox').textContent = (j.data.lines || []).join('\\n');
}
async function refresh() {
  loadStatus(); loadApps(); loadRepos(); loadMachine();
  loadWatchdog(); loadOllama(); loadServices();
}
refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""


class AdminServer:
    """aiohttp server wrapping the agent's handler registry as JSON endpoints
    plus a single HTML page."""

    def __init__(self, cfg, runner, port: int = DEFAULT_PORT):
        self.cfg = cfg
        self.runner = runner
        self.port = port
        self.app: Optional[web.Application] = None
        self.runner_obj: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None

    async def start(self) -> None:
        self.app = web.Application()
        r = self.app.router
        r.add_get("/", self._page)
        r.add_get("/api/status", self._wrap("status"))
        r.add_get("/api/apps", self._wrap("list_apps"))
        r.add_post("/api/apps/{name}/start", self._wrap("start_app", path_args={"name": "name"}))
        r.add_post("/api/apps/{name}/stop", self._wrap("stop_app", path_args={"name": "name"}))
        r.add_post("/api/apps/{name}/restart", self._wrap("restart_app", path_args={"name": "name"}))
        r.add_get("/api/apps/{name}/logs", self._app_logs)
        r.add_get("/api/repos", self._wrap("list_repos"))
        r.add_post("/api/repos/{name}/pull", self._wrap("git_pull", path_args={"name": "repo"}))
        r.add_get("/api/services", self._wrap("list_services"))
        r.add_post("/api/services/{name}/restart", self._wrap("service_restart", path_args={"name": "name"}))
        r.add_get("/api/machine", self._machine)
        r.add_get("/api/watchdog", self._wrap("watchdog_stats"))
        r.add_get("/api/ollama", self._wrap("ollama_list"))
        r.add_get("/api/ollama/ps", self._wrap("ollama_ps"))
        r.add_post("/api/host/restart", self._host_restart)
        r.add_post("/api/host/cancel", self._wrap("host_cancel_restart"))
        r.add_post("/api/self_update", self._wrap("self_update"))

        self.runner_obj = web.AppRunner(self.app, access_log=None)
        await self.runner_obj.setup()
        self.site = web.TCPSite(self.runner_obj, "0.0.0.0", self.port)
        try:
            await self.site.start()
            log.info("admin UI listening on http://0.0.0.0:%d", self.port)
        except Exception:
            log.exception("admin UI failed to bind port %d", self.port)
            self.site = None

    async def stop(self) -> None:
        try:
            if self.site is not None:
                await self.site.stop()
        except Exception:
            pass
        try:
            if self.runner_obj is not None:
                await self.runner_obj.cleanup()
        except Exception:
            pass

    # ---------- handlers ----------

    async def _page(self, request: web.Request) -> web.Response:
        body = HTML_PAGE.replace("%HOST%", self.cfg.host_id)
        return web.Response(text=body, content_type="text/html")

    def _wrap(self, tool: str, path_args: Optional[dict] = None):
        async def _h(request: web.Request) -> web.Response:
            args: dict = {}
            if path_args:
                for path_key, arg_key in path_args.items():
                    args[arg_key] = request.match_info.get(path_key, "")
            return await self._call_and_respond(tool, args)
        return _h

    async def _app_logs(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        try:
            lines = int(request.query.get("lines", 100))
        except ValueError:
            lines = 100
        return await self._call_and_respond("app_logs", {"name": name, "lines": lines})

    async def _machine(self, request: web.Request) -> web.Response:
        try:
            minutes = int(request.query.get("minutes", 5))
        except ValueError:
            minutes = 5
        return await self._call_and_respond(
            "machine_stats",
            {"window_minutes": minutes, "top_processes": 6},
        )

    async def _host_restart(self, request: web.Request) -> web.Response:
        try:
            delay = int(request.query.get("delay_s", 30))
        except ValueError:
            delay = 30
        return await self._call_and_respond("host_restart", {"delay_s": delay})

    async def _call_and_respond(self, tool: str, args: dict) -> web.Response:
        from agent.handlers import REGISTRY, HandlerCtx  # lazy: avoid cycle
        handler = REGISTRY.get(tool)
        if handler is None:
            return web.json_response({"ok": False, "error": f"unknown tool: {tool}"})
        cmd = Command(tool=tool, args=args)
        hctx = HandlerCtx(self.cfg, self.runner.nc, self.runner)
        try:
            reply = await handler(hctx, cmd)
        except Exception as e:
            log.exception("admin_ui tool %s raised", tool)
            return web.json_response({"ok": False, "error": repr(e)})
        return web.json_response({
            "ok": reply.ok,
            "data": reply.data,
            "error": reply.error,
            "job_id": reply.job_id,
        })
