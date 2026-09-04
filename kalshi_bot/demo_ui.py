"""Local dashboard for the demo loop.

A single-page site served from the standard library, bound to localhost. It
polls the loop's JSON state file every two seconds, so it works whether or
not the loop is running, and it can stop the loop by creating the same stop
file the loop watches. No third-party dependencies, nothing leaves the
machine.

    kalshi-bot demo-ui            # then open http://127.0.0.1:8765
"""

from __future__ import annotations

import json
import logging
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ALIVE_WITHIN_S = 30.0

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalshi demo loop</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --ink:#1b1f24; --muted:#6b7280; --line:#e5e7eb;
          --good:#15803d; --bad:#b91c1c; --warn:#b45309; --accent:#2563eb; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0f1115; --card:#181b22; --ink:#e6e8ec; --muted:#9aa3b2; --line:#2a2f3a;
            --good:#4ade80; --bad:#f87171; --warn:#fbbf24; --accent:#60a5fa; }
  }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:15px/1.45 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
  main { max-width: 960px; margin: 0 auto; padding: 24px 16px 48px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: var(--muted); margin-bottom: 20px; }
  .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap:12px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
  .label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
  .big { font-size: 28px; font-weight: 600; margin-top: 2px; }
  .good { color: var(--good); } .bad { color: var(--bad); } .warn { color: var(--warn); }
  .bar { height: 8px; background: var(--line); border-radius: 4px; overflow: hidden; margin-top: 8px; }
  .bar > div { height: 100%; }
  .row { display:flex; gap:12px; flex-wrap:wrap; align-items:center; margin: 16px 0; }
  button { font: inherit; padding: 8px 14px; border-radius: 8px; border: 1px solid var(--line);
           background: var(--card); color: var(--ink); cursor: pointer; }
  button.stop { background: var(--bad); color: #fff; border-color: transparent; }
  table { width:100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align:left; padding: 6px 8px; border-bottom: 1px solid var(--line); }
  th { color: var(--muted); font-weight: 500; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
  #err { color: var(--bad); }
</style>
</head>
<body>
<main>
  <h1>Kalshi demo loop</h1>
  <div class="sub" id="cfg">loading…</div>
  <div class="grid">
    <div class="card"><div class="label">Loop</div><div class="big" id="status">–</div>
      <div id="statusnote" class="sub" style="margin:0"></div></div>
    <div class="card"><div class="label">Realised P&amp;L (after fees)</div><div class="big" id="pnl">–</div>
      <div class="label" style="margin-top:8px">toward profit target</div><div class="bar"><div id="pbar" style="background:var(--good);width:0"></div></div>
      <div class="label" style="margin-top:8px">toward loss cap</div><div class="bar"><div id="lbar" style="background:var(--bad);width:0"></div></div></div>
    <div class="card"><div class="label">Trades</div><div class="big" id="trades">–</div><div id="wl" class="sub" style="margin:0"></div></div>
    <div class="card"><div class="label">Open position</div><div class="big" id="open">none</div><div id="opennote" class="sub" style="margin:0"></div></div>
  </div>
  <div class="row">
    <button class="stop" onclick="post('/api/stop')">Stop loop</button>
    <button onclick="post('/api/clear-stop')">Clear stop file</button>
    <span id="stopnote" class="sub" style="margin:0"></span>
    <span id="err"></span>
  </div>
  <div class="card">
    <div class="label">Settled trades (latest first)</div>
    <table><thead><tr><th>Time</th><th>Market</th><th>Side</th><th class="num">Qty</th>
      <th class="num">Price</th><th>Result</th><th class="num">Net</th></tr></thead>
      <tbody id="hist"></tbody></table>
  </div>
</main>
<script>
function fmt(x, d) { return (x === null || x === undefined) ? '–' : Number(x).toFixed(d); }
function money(x) { const v = Number(x || 0); return (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2); }
function ts(t) { return t ? new Date(t * 1000).toLocaleTimeString() : ''; }
async function post(path) {
  try { await fetch(path, {method: 'POST'}); await refresh(); }
  catch (e) { document.getElementById('err').textContent = String(e); }
}
async function refresh() {
  let d;
  try { d = await (await fetch('/api/state')).json(); document.getElementById('err').textContent = ''; }
  catch (e) { document.getElementById('err').textContent = 'dashboard server unreachable'; return; }
  const s = d.state || {}; const c = s.config || {};
  const series = Array.isArray(c.series) ? c.series.join(', ') : (c.series || '');
  const size = c.dollars ? `$${fmt(c.dollars, 2)} per trade` : `${c.contracts || '?'} contract(s) per trade`;
  document.getElementById('cfg').textContent = d.state
    ? `${c.env || ''} · ${series} · ${size} · max price ${fmt(c.max_price, 2)} · loss cap $${fmt(c.loss_cap, 2)} · ${c.profit_target ? 'profit target $' + fmt(c.profit_target, 2) : 'no profit cap'} · reading ${d.state_file}`
    : `no state file yet at ${d.state_file}; start the loop with: kalshi-bot demo-trade`;
  const st = document.getElementById('status'), note = document.getElementById('statusnote');
  st.className = 'big';
  document.title = (c.env === 'LIVE' ? 'LIVE · ' : '') + 'Kalshi loop';
  document.querySelector('h1').textContent = c.env === 'LIVE' ? 'Kalshi loop · REAL MONEY' : 'Kalshi demo loop';
  if (s.halted) { st.textContent = 'halted'; st.classList.add('warn'); note.textContent = s.halted; }
  else if (d.alive) { st.innerHTML = '<span class="dot" style="background:var(--good)"></span>running'; st.classList.add('good'); note.textContent = 'last tick ' + ts(s.last_tick_ts); }
  else { st.textContent = 'not running'; st.classList.add('bad'); note.textContent = s.stopped ? ('stopped: ' + s.stopped) : (s.last_tick_ts ? 'last tick ' + ts(s.last_tick_ts) : ''); }
  const pnl = document.getElementById('pnl'); pnl.textContent = money(s.realized_pnl);
  pnl.className = 'big ' + ((s.realized_pnl || 0) >= 0 ? 'good' : 'bad');
  const p = Number(s.realized_pnl || 0);
  document.getElementById('pbar').style.width = (c.profit_target ? Math.min(100, Math.max(0, p / c.profit_target * 100)) : 0) + '%';
  document.getElementById('lbar').style.width = (c.loss_cap ? Math.min(100, Math.max(0, -p / c.loss_cap * 100)) : 0) + '%';
  document.getElementById('trades').textContent = s.trades ?? '–';
  document.getElementById('wl').textContent = `${s.wins || 0} won · ${s.losses || 0} lost · fees $${fmt(s.fees_paid, 2)}` + (c.max_trades ? ` · max ${c.max_trades}` : '');
  const opens = Object.entries(s.series || {}).filter(([, v]) => v && v.open).map(([name, v]) => [name, v.open]);
  const oe = document.getElementById('open'), on = document.getElementById('opennote');
  if (opens.length) {
    oe.textContent = opens.map(([, o]) => `${o.side.toUpperCase()} x${o.count}`).join(' · ');
    on.innerHTML = opens.map(([, o]) => {
      const left = Math.max(0, Math.round(o.close_ts - d.now));
      return `<span class="mono">${o.ticker}</span> ` + (o.filled_count > 0 ? `filled at ${fmt(o.fill_price, 3)}` : `resting at ${fmt(o.limit_price, 3)}, unfilled`) + ` · closes in ${Math.floor(left/60)}m ${left%60}s`;
    }).join('<br>');
  } else { oe.textContent = 'none'; on.textContent = ''; }
  document.getElementById('stopnote').textContent = d.stop_file_present ? `stop file present (${d.stop_file}); the loop will exit and refuse to start until cleared` : '';
  const rows = (s.history || []).slice().reverse().slice(0, 30).map(h =>
    `<tr><td>${ts(h.settled_ts)}</td><td class="mono">${h.ticker}</td><td>${h.side}</td><td class="num">${fmt(h.count, 0)}</td><td class="num">${fmt(h.price, 3)}</td><td>${h.result}</td><td class="num ${h.net >= 0 ? 'good' : 'bad'}">${money(h.net)}</td></tr>`);
  document.getElementById('hist').innerHTML = rows.join('') || '<tr><td colspan="7" class="sub">nothing settled yet</td></tr>';
}
refresh(); setInterval(refresh, 2000);
</script>
</body>
</html>
"""


class Dashboard:
    """State that the HTTP handler reads; one per server.

    ``state_files`` may list several candidates (the live and the demo loop
    write different files); each poll shows the most recently modified one.
    """

    def __init__(self, state_file: Path | list[Path], stop_file: Path) -> None:
        files = state_file if isinstance(state_file, list) else [state_file]
        self.state_files = [Path(f) for f in files]
        self.stop_file = Path(stop_file)

    @property
    def state_file(self) -> Path:
        existing = [f for f in self.state_files if f.exists()]
        if not existing:
            return self.state_files[0]
        return max(existing, key=lambda f: f.stat().st_mtime)

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        state: dict[str, Any] | None = None
        state_file = self.state_file
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
            except ValueError:
                state = None  # mid-write; the next poll will get it
        last = (state or {}).get("last_tick_ts")
        alive = bool(state) and last is not None and now - float(last) <= ALIVE_WITHIN_S
        return {
            "now": now,
            "state": state,
            "state_file": str(state_file),
            "stop_file": str(self.stop_file),
            "stop_file_present": self.stop_file.exists(),
            "alive": alive and not (state or {}).get("halted"),
        }

    def stop(self) -> None:
        self.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.stop_file.write_text(
            f"stopped from dashboard at {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )

    def clear_stop(self) -> None:
        if self.stop_file.exists():
            self.stop_file.unlink()


def make_handler(dash: Dashboard) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default
            log.debug(fmt, *args)

        def _send(self, status: HTTPStatus, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                body = json.dumps(dash.snapshot()).encode()
                self._send(HTTPStatus.OK, body, "application/json")
            else:
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            if self.path == "/api/stop":
                dash.stop()
            elif self.path == "/api/clear-stop":
                dash.clear_stop()
            else:
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                return
            self._send(HTTPStatus.OK, json.dumps(dash.snapshot()).encode(), "application/json")

    return Handler


class _Server(ThreadingHTTPServer):
    # http.server sets allow_reuse_address, which on Windows lets a second
    # dashboard bind a port an older one still serves; the browser then keeps
    # talking to the stale process. Fail loudly instead.
    allow_reuse_address = False
    daemon_threads = True


def serve(
    state_file: Path | list[Path], stop_file: Path, host: str = "127.0.0.1", port: int = 8765
) -> ThreadingHTTPServer:
    """Bind and return the server; call ``serve_forever`` on it."""
    try:
        return _Server((host, port), make_handler(Dashboard(state_file, stop_file)))
    except OSError as exc:
        raise OSError(
            f"port {port} is already in use, probably by an earlier dashboard window; "
            f"close it or pass --port with a different number ({exc})"
        ) from exc
