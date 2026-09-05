"""The dashboard page served by ``demo_ui``: a single HTML document, no external assets.

Design notes: one hero figure (realised P&L), a row of stat tiles, an equity
curve as the only chart (single series, crosshair tooltip, hairline grid),
then the settled-trades table with tabular figures. Dark and light are both
first-class; the dark palette is stepped for the dark surface, not inverted.
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalshi 15m desk</title>
<style>
  :root {
    color-scheme: light;
    --page: #f9f9f7; --surface: #fcfcfb; --surface-2: #f3f3f0;
    --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
    --hair: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
    --accent: #2a78d6; --accent-soft: #cde2fb;
    --good: #006300; --good-mark: #0ca30c; --bad: #d03b3b; --warn: #b26f00; --warn-mark: #fab219;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --page: #0d0d0d; --surface: #1a1a19; --surface-2: #232322;
      --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
      --hair: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
      --accent: #3987e5; --accent-soft: #184f95;
      --good: #0ca30c; --good-mark: #0ca30c; --bad: #e66767; --warn: #fab219; --warn-mark: #fab219;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--page); color: var(--ink); }
  body { font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         -webkit-font-smoothing: antialiased; }
  main { max-width: 1120px; margin: 0 auto; padding: 28px 24px 56px; }

  header { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px;
           flex-wrap: wrap; margin-bottom: 20px; }
  .brand { display: flex; flex-direction: column; gap: 4px; }
  .brand h1 { margin: 0; font-size: 20px; font-weight: 600; letter-spacing: -0.01em; }
  .brand .sub { color: var(--ink-2); font-size: 13px; }
  .pill { display: inline-flex; align-items: center; gap: 8px; padding: 6px 12px; border-radius: 999px;
          border: 1px solid var(--ring); background: var(--surface); font-size: 12px; font-weight: 600;
          letter-spacing: 0.04em; text-transform: uppercase; color: var(--ink-2); }
  .pill .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  .pill.live { color: var(--bad); border-color: color-mix(in oklab, var(--bad) 40%, transparent); }
  .pill.on .dot { background: var(--good-mark); box-shadow: 0 0 0 4px color-mix(in oklab, var(--good-mark) 25%, transparent); }
  .pill.halt .dot { background: var(--warn-mark); }

  .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px; }
  .card { background: var(--surface); border: 1px solid var(--ring); border-radius: 14px; padding: 18px 20px; }
  .hero { grid-column: span 5; display: flex; flex-direction: column; justify-content: space-between; min-height: 176px; }
  .tile { grid-column: span 7; display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
  .tile .card { padding: 16px 18px; }
  .label { color: var(--muted); font-size: 11px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; }
  .value { font-size: 26px; font-weight: 600; margin-top: 6px; letter-spacing: -0.01em; }
  .value.hero-fig { font-size: 52px; line-height: 1.05; margin-top: 10px; }
  .delta { color: var(--ink-2); font-size: 13px; margin-top: 4px; }
  .up { color: var(--good); } .down { color: var(--bad); } .flat { color: var(--ink-2); }

  .meter { margin-top: 12px; }
  .meter .track { height: 6px; border-radius: 3px; background: var(--accent-soft); overflow: hidden; }
  .meter .fill { height: 100%; background: var(--accent); border-radius: 3px; transition: width .4s ease; }
  .meter .fill.warn { background: var(--warn-mark); } .meter .fill.crit { background: var(--bad); }
  .meter .cap { display: flex; justify-content: space-between; color: var(--muted); font-size: 12px; margin-top: 6px; }

  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin: 18px 0; }
  button { font: inherit; font-weight: 600; padding: 9px 16px; border-radius: 10px; border: 1px solid var(--ring);
           background: var(--surface); color: var(--ink); cursor: pointer; }
  button:hover { background: var(--surface-2); }
  button.stop { background: var(--bad); border-color: transparent; color: #fff; }
  .note { color: var(--ink-2); font-size: 13px; }
  #err { color: var(--bad); font-size: 13px; }

  .chart { grid-column: span 12; }
  .chart-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
  .chart-head .label { margin: 0; }
  .chart-head .value { font-size: 15px; margin: 0; font-weight: 600; color: var(--ink-2); }
  .plot { position: relative; height: 220px; }
  .plot svg { width: 100%; height: 100%; display: block; }
  .plot .grid-line { stroke: var(--hair); stroke-width: 1; }
  .plot .base { stroke: var(--axis); stroke-width: 1; }
  .plot .line { fill: none; stroke: var(--accent); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
  .plot .area { fill: var(--accent); opacity: 0.08; }
  .plot .xhair { stroke: var(--axis); stroke-width: 1; display: none; }
  .plot .pt { fill: var(--accent); stroke: var(--surface); stroke-width: 2; display: none; }
  .plot .end { fill: var(--accent); stroke: var(--surface); stroke-width: 2; }
  .plot text { fill: var(--muted); font-size: 11px; }
  .plot .endlab { fill: var(--ink); font-size: 12px; font-weight: 600; }
  .tip { position: absolute; pointer-events: none; display: none; background: var(--surface);
         border: 1px solid var(--ring); border-radius: 10px; padding: 8px 10px; font-size: 12px;
         box-shadow: 0 6px 20px rgba(0,0,0,0.12); white-space: nowrap; }
  .tip b { font-weight: 600; }
  .tip .k { display: inline-block; width: 14px; height: 2px; background: var(--accent); vertical-align: middle; margin-right: 6px; }
  .empty { color: var(--muted); font-size: 13px; padding: 60px 0; text-align: center; }

  .split { grid-column: span 12; display: grid; grid-template-columns: 2fr 1fr; gap: 14px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--hair); white-space: nowrap; }
  th { color: var(--muted); font-weight: 600; font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; }
  tr:last-child td { border-bottom: none; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; color: var(--ink-2); }
  .side { display: inline-block; min-width: 34px; text-align: center; padding: 2px 6px; border-radius: 6px;
          font-size: 11px; font-weight: 700; letter-spacing: 0.04em; border: 1px solid var(--ring); color: var(--ink-2); }
  .open-list { display: flex; flex-direction: column; gap: 10px; margin-top: 10px; }
  .open-item { display: flex; justify-content: space-between; gap: 10px; padding: 10px 12px; border-radius: 10px;
               background: var(--surface-2); font-size: 13px; }
  .open-item .t { color: var(--muted); font-variant-numeric: tabular-nums; }
  .series-row { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid var(--hair); font-size: 13px; }
  .series-row:last-child { border-bottom: none; }
  .series-row .n { color: var(--ink-2); font-variant-numeric: tabular-nums; }

  @media (max-width: 860px) {
    .hero, .tile, .split { grid-column: span 12; }
    .split { grid-template-columns: 1fr; }
    .tile { grid-template-columns: 1fr 1fr; }
    .value.hero-fig { font-size: 42px; }
  }
</style>
</head>
<body>
<main>
  <header>
    <div class="brand">
      <h1 id="title">Kalshi 15-minute desk</h1>
      <div class="sub" id="cfg">connecting…</div>
    </div>
    <div class="row" style="margin:0">
      <div class="pill" id="pill"><span class="dot"></span><span id="pilltext">–</span></div>
      <button class="stop" onclick="post('/api/stop')">Stop loop</button>
      <button onclick="post('/api/clear-stop')">Clear stop file</button>
    </div>
  </header>
  <div class="row" style="margin:-8px 0 14px"><span class="note" id="stopnote"></span><span id="err"></span></div>

  <div class="grid">
    <section class="card hero">
      <div>
        <div class="label">Realised P&amp;L, after fees</div>
        <div class="value hero-fig" id="pnl">–</div>
        <div class="delta" id="pnlnote"></div>
      </div>
      <div class="meter">
        <div class="track"><div class="fill" id="lossfill" style="width:0"></div></div>
        <div class="cap"><span id="lossleft">loss cap</span><span id="losscap"></span></div>
      </div>
    </section>

    <section class="tile">
      <div class="card"><div class="label">Trades settled</div><div class="value" id="trades">–</div><div class="delta" id="record"></div></div>
      <div class="card"><div class="label">Win rate</div><div class="value" id="winrate">–</div><div class="delta" id="avgnet"></div></div>
      <div class="card"><div class="label">Fees paid</div><div class="value" id="fees">–</div><div class="delta" id="feenote"></div></div>
      <div class="card"><div class="label">Per trade</div><div class="value" id="size">–</div><div class="delta" id="sizenote"></div></div>
      <div class="card"><div class="label">Strategy</div><div class="value" id="strategy">–</div><div class="delta" id="stratnote"></div></div>
      <div class="card"><div class="label">Last tick</div><div class="value" id="tick">–</div><div class="delta" id="ticknote"></div></div>
    </section>

    <section class="card chart">
      <div class="chart-head"><div class="label">Equity curve, cumulative net by settlement</div><div class="value" id="curvenote"></div></div>
      <div class="plot" id="plot"><div class="tip" id="tip"></div></div>
    </section>

    <section class="split">
      <div class="card">
        <div class="label">Settled trades, latest first</div>
        <div style="overflow-x:auto;margin-top:10px"><table><thead><tr>
          <th>Time</th><th>Market</th><th>Side</th><th class="num">Qty</th><th class="num">Price</th><th>Result</th><th class="num">Net</th>
        </tr></thead><tbody id="hist"></tbody></table></div>
      </div>
      <div>
        <div class="card" style="margin-bottom:14px">
          <div class="label">Open positions</div>
          <div class="open-list" id="open"><div class="note">none</div></div>
        </div>
        <div class="card">
          <div class="label">By series</div>
          <div id="series" style="margin-top:6px"><div class="note">nothing settled yet</div></div>
        </div>
      </div>
    </section>
  </div>

</main>
<script>
const $ = id => document.getElementById(id);
const fmt = (x, d) => (x === null || x === undefined || Number.isNaN(Number(x))) ? '–' : Number(x).toFixed(d);
const money = x => { const v = Number(x || 0); return (v < 0 ? '−$' : '$') + Math.abs(v).toFixed(2); };
const signed = x => { const v = Number(x || 0); return (v > 0 ? '+' : v < 0 ? '−' : '') + '$' + Math.abs(v).toFixed(2); };
const tm = t => t ? new Date(t * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}) : '';
const cls = v => v > 0 ? 'up' : v < 0 ? 'down' : 'flat';
async function post(path) {
  try { await fetch(path, {method: 'POST'}); await refresh(); }
  catch (e) { $('err').textContent = String(e); }
}
function meter(p, cap) {
  const used = cap ? Math.min(100, Math.max(0, -p / cap * 100)) : 0;
  const f = $('lossfill'); f.style.width = used + '%';
  f.className = 'fill' + (used >= 80 ? ' crit' : used >= 50 ? ' warn' : '');
  $('lossleft').textContent = cap ? `${money(Math.max(0, cap + p))} of losses until the cap` : 'no loss cap';
  $('losscap').textContent = cap ? `cap −$${fmt(cap, 2)} realised` : '';
}
function curve(hist) {
  const plot = $('plot'); const tip = $('tip');
  plot.querySelectorAll('svg, .empty').forEach(n => n.remove());
  if (!hist.length) { const e = document.createElement('div'); e.className = 'empty'; e.textContent = 'The curve starts at the first settlement.'; plot.appendChild(e); $('curvenote').textContent = ''; return; }
  const W = plot.clientWidth || 800, H = plot.clientHeight || 220, L = 44, R = 64, T = 14, B = 26;
  const pts = [{i: 0, y: 0, t: null}]; let acc = 0;
  hist.forEach((h, i) => { acc += Number(h.net || 0); pts.push({i: i + 1, y: acc, t: h.settled_ts, h}); });
  const ys = pts.map(p => p.y); let lo = Math.min(0, ...ys), hi = Math.max(0, ...ys);
  if (hi - lo < 1) { hi += 0.5; lo -= 0.5; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const x = i => L + (W - L - R) * (pts.length > 1 ? i / (pts.length - 1) : 0.5);
  const y = v => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg'); svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const mk = (tag, attrs, text) => { const el = document.createElementNS(ns, tag); for (const k in attrs) el.setAttribute(k, attrs[k]); if (text !== undefined) el.textContent = text; svg.appendChild(el); return el; };
  const ticks = 4;
  for (let k = 0; k <= ticks; k++) {
    const v = lo + (hi - lo) * k / ticks; const yy = y(v);
    mk('line', {x1: L, x2: W - R, y1: yy, y2: yy, class: 'grid-line'});
    mk('text', {x: L - 8, y: yy + 4, 'text-anchor': 'end'}, signed(v).replace('.00', ''));
  }
  mk('line', {x1: L, x2: W - R, y1: y(0), y2: y(0), class: 'base'});
  const d = pts.map((p, k) => (k ? 'L' : 'M') + x(p.i).toFixed(1) + ' ' + y(p.y).toFixed(1)).join(' ');
  mk('path', {d: d + ` L${x(pts[pts.length - 1].i).toFixed(1)} ${y(0)} L${x(0).toFixed(1)} ${y(0)} Z`, class: 'area'});
  mk('path', {d, class: 'line'});
  const last = pts[pts.length - 1];
  mk('circle', {cx: x(last.i), cy: y(last.y), r: 4, class: 'end'});
  mk('text', {x: x(last.i) + 10, y: y(last.y) + 4, class: 'endlab'}, signed(last.y));
  mk('text', {x: L, y: H - 6}, hist[0] ? tm(hist[0].settled_ts) : '');
  mk('text', {x: W - R, y: H - 6, 'text-anchor': 'end'}, tm(last.t));
  const xh = mk('line', {y1: T, y2: H - B, class: 'xhair'});
  const pt = mk('circle', {r: 5, class: 'pt'});
  svg.addEventListener('mousemove', ev => {
    const r = svg.getBoundingClientRect(); const mx = (ev.clientX - r.left) * W / r.width;
    let best = pts[0]; for (const p of pts) if (Math.abs(x(p.i) - mx) < Math.abs(x(best.i) - mx)) best = p;
    xh.setAttribute('x1', x(best.i)); xh.setAttribute('x2', x(best.i)); xh.style.display = 'block';
    pt.setAttribute('cx', x(best.i)); pt.setAttribute('cy', y(best.y)); pt.style.display = 'block';
    tip.style.display = 'block';
    tip.innerHTML = best.h
      ? `<div><span class="k"></span><b>${signed(best.y)}</b> cumulative</div><div class="note">${tm(best.t)} · ${best.h.ticker}</div><div class="note">${best.h.side.toUpperCase()} ×${fmt(best.h.count, 0)} @ ${fmt(best.h.price, 2)} → ${best.h.result.toUpperCase()} · <span class="${cls(best.h.net)}">${signed(best.h.net)}</span></div>`
      : `<div><span class="k"></span><b>$0.00</b> start</div>`;
    const px = x(best.i) * r.width / W; const left = px + 14 + tip.offsetWidth > r.width ? px - tip.offsetWidth - 14 : px + 14;
    tip.style.left = left + 'px'; tip.style.top = Math.max(0, y(best.y) * r.height / H - 40) + 'px';
  });
  svg.addEventListener('mouseleave', () => { xh.style.display = 'none'; pt.style.display = 'none'; tip.style.display = 'none'; });
  plot.appendChild(svg);
  const best = Math.max(...ys), worst = Math.min(...ys);
  $('curvenote').textContent = `peak ${signed(best)} · trough ${signed(worst)}`;
}
async function refresh() {
  let d;
  try { d = await (await fetch('/api/state')).json(); $('err').textContent = ''; }
  catch (e) { $('err').textContent = 'dashboard server unreachable'; return; }
  const s = d.state || {}; const c = s.config || {};
  const live = c.env === 'LIVE';
  document.title = (live ? 'LIVE · ' : '') + 'Kalshi 15m desk';
  $('title').textContent = live ? 'Kalshi 15-minute desk · real money' : 'Kalshi 15-minute desk · ' + (c.env || 'idle');
  const series = Array.isArray(c.series) ? c.series.join(' · ') : (c.series || '');
  $('cfg').textContent = d.state ? `${series} · reading ${d.state_file}` : `no state file yet at ${d.state_file}`;
  const pill = $('pill'); pill.className = 'pill' + (live ? ' live' : '') + (s.halted ? ' halt' : d.alive ? ' on' : '');
  $('pilltext').textContent = s.halted ? 'halted' : d.alive ? (live ? 'live · running' : 'running') : (s.stopped ? 'stopped' : 'not running');
  const p = Number(s.realized_pnl || 0);
  const pnl = $('pnl'); pnl.textContent = signed(p); pnl.className = 'value hero-fig ' + cls(p);
  const hist = s.history || [];
  $('pnlnote').textContent = s.halted ? s.halted : (s.stopped ? 'stopped: ' + s.stopped : (hist.length ? `last settlement ${tm(hist[hist.length - 1].settled_ts)}` : 'no settlements yet'));
  meter(p, c.loss_cap);
  $('trades').textContent = s.trades ?? '–';
  $('record').textContent = `${s.wins || 0} won · ${s.losses || 0} lost` + (c.max_trades ? ` · max ${c.max_trades}` : '');
  const settled = (s.wins || 0) + (s.losses || 0);
  $('winrate').textContent = settled ? Math.round(100 * (s.wins || 0) / settled) + '%' : '–';
  $('avgnet').textContent = settled ? `${signed(p / settled)} per trade` : '';
  $('fees').textContent = money(s.fees_paid || 0);
  $('feenote').textContent = settled ? `${money((s.fees_paid || 0) / settled)} per trade` : '';
  $('size').textContent = c.dollars ? '$' + fmt(c.dollars, 2) : (c.contracts ? c.contracts + ' ct' : '–');
  $('sizenote').textContent = `max price ${fmt(c.max_price, 2)}` + (c.profit_target ? ` · target +$${fmt(c.profit_target, 2)}` : ' · no profit cap');
  $('strategy').textContent = c.strategy === 'fairvalue' ? 'Fair value' : c.strategy === 'alternate' ? 'Alternate' : (c.strategy || '–');
  $('stratnote').textContent = c.strategy === 'fairvalue' ? `margin ${fmt(c.margin, 2)} · vol ${Math.round((c.vol_window || 0) / 60)} min` : (c.strategy === 'alternate' ? 'YES / NO in turn' : '');
  $('tick').textContent = s.last_tick_ts ? tm(s.last_tick_ts) : '–';
  $('ticknote').textContent = s.last_tick_ts ? `${Math.max(0, Math.round(d.now - s.last_tick_ts))}s ago` : '';
  curve(hist);
  const opens = Object.entries(s.series || {}).filter(([, v]) => v && v.open).map(([n, v]) => [n, v.open]);
  $('open').innerHTML = opens.length ? opens.map(([n, o]) => {
    const left = Math.max(0, Math.round(o.close_ts - d.now));
    return `<div class="open-item"><div><span class="side">${o.side.toUpperCase()}</span> ×${o.count} <span class="mono">${o.ticker}</span><br><span class="note">${o.filled_count > 0 ? 'filled at ' + fmt(o.fill_price, 3) : 'resting at ' + fmt(o.limit_price, 3)}</span></div><div class="t">${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}</div></div>`;
  }).join('') : '<div class="note">none</div>';
  const bySeries = {};
  hist.forEach(h => { const k = h.series || (h.ticker || '').split('-')[0]; const b = bySeries[k] = bySeries[k] || {n: 0, w: 0, net: 0}; b.n++; b.w += h.won ? 1 : 0; b.net += Number(h.net || 0); });
  const keys = Object.keys(bySeries);
  $('series').innerHTML = keys.length ? keys.map(k => `<div class="series-row"><span>${k}</span><span class="n">${bySeries[k].n} trades · ${Math.round(100 * bySeries[k].w / bySeries[k].n)}% · <span class="${cls(bySeries[k].net)}">${signed(bySeries[k].net)}</span></span></div>`).join('') : '<div class="note">nothing settled yet</div>';
  $('stopnote').textContent = d.stop_file_present ? `stop file present (${d.stop_file}); the loop exits and will not start until it is cleared` : '';
  $('hist').innerHTML = hist.slice().reverse().slice(0, 40).map(h =>
    `<tr><td>${tm(h.settled_ts)}</td><td class="mono">${h.ticker}</td><td><span class="side">${h.side.toUpperCase()}</span></td><td class="num">${fmt(h.count, 0)}</td><td class="num">${fmt(h.price, 3)}</td><td>${h.result.toUpperCase()}</td><td class="num ${cls(h.net)}">${signed(h.net)}</td></tr>`
  ).join('') || '<tr><td colspan="7" class="note">nothing settled yet</td></tr>';
}
refresh(); setInterval(refresh, 2000); window.addEventListener('resize', refresh);
</script>
</body>
</html>
"""
