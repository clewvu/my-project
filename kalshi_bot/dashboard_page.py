"""The dashboard page served by ``demo_ui``: one HTML document, no external assets.

Lewis Wealth Global desk. Navy and gold in dark and light, a serif display
face for the masthead and figures, tabular numerals everywhere money is
shown. Four views: Overview (realised P&L against the cap, the equity
curve, open positions), Trades (every booked result, sortable, filterable,
each row opening the decision-log record behind it), Analysis (the review's
cuts as tables whose rows filter the trades, the confidence tiers that
gate sizing, and what the numbers support), Activity (the event feed).
Controls for pause, resume, stop and clear stay in the masthead.
"""

FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<circle cx="32" cy="32" r="30" fill="#14213d" stroke="#c9a53a" stroke-width="3"/>'
    '<path d="M21 20v22h11" fill="none" stroke="#e2c56b" stroke-width="5" stroke-linecap="square"/>'
    '<path d="M31 24l4 15 4-11 4 11 4-15" fill="none" stroke="#e2c56b" stroke-width="3.2" '
    'stroke-linejoin="round" stroke-linecap="round"/></svg>'
)

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lewis Wealth Global</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
  :root {
    color-scheme: light;
    --page: #f4f1ea; --surface: #fbf9f4; --surface-2: #efebe1; --raised: #ffffff;
    --ink: #101828; --ink-2: #3f4a5c; --muted: #7a8394;
    --hair: #e3ddcf; --axis: #c9c2b0; --ring: rgba(16,24,40,0.12);
    --navy: #14213d; --navy-2: #1f2f57; --gold: #b8922e; --gold-soft: #f1e4bf; --gold-ink: #7c6118;
    --good: #1a7f37; --good-mark: #2ea043; --bad: #b42318; --warn: #9a6700; --warn-mark: #d4a72c;
    --serif: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Palatino, Georgia, serif;
    --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
    --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  [data-theme="dark"] {
    color-scheme: dark;
    --page: #0b1020; --surface: #121a30; --surface-2: #182340; --raised: #1c2848;
    --ink: #f3f1ea; --ink-2: #c5c9d6; --muted: #8b93a7;
    --hair: #24304f; --axis: #34416a; --ring: rgba(243,241,234,0.10);
    --navy: #0b1020; --navy-2: #182340; --gold: #d4af4a; --gold-soft: #3a2f12; --gold-ink: #e6c66d;
    --good: #3fb950; --good-mark: #3fb950; --bad: #f47067; --warn: #e3b341; --warn-mark: #e3b341;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --page: #0b1020; --surface: #121a30; --surface-2: #182340; --raised: #1c2848;
      --ink: #f3f1ea; --ink-2: #c5c9d6; --muted: #8b93a7;
      --hair: #24304f; --axis: #34416a; --ring: rgba(243,241,234,0.10);
      --navy: #0b1020; --navy-2: #182340; --gold: #d4af4a; --gold-soft: #3a2f12; --gold-ink: #e6c66d;
      --good: #3fb950; --good-mark: #3fb950; --bad: #f47067; --warn: #e3b341; --warn-mark: #e3b341;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--page); color: var(--ink); }
  body { font: 14px/1.5 var(--sans); -webkit-font-smoothing: antialiased; }
  main { max-width: 1180px; margin: 0 auto; padding: 22px 24px 64px; }
  a { color: inherit; }

  /* masthead */
  .mast { display: flex; align-items: center; justify-content: space-between; gap: 18px; flex-wrap: wrap;
          padding: 6px 0 18px; border-bottom: 1px solid var(--hair); }
  .brand { display: flex; align-items: center; gap: 14px; min-width: 0; }
  .brand svg { width: 52px; height: 52px; flex: none; }
  .wordmark { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .wordmark .name { font: 600 22px/1.1 var(--serif); letter-spacing: 0.01em; color: var(--ink); white-space: nowrap; }
  .wordmark .name span { color: var(--gold); }
  .wordmark .creed { font: italic 13px/1.3 var(--serif); color: var(--gold-ink); letter-spacing: 0.02em; }
  .controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .pill { display: inline-flex; align-items: center; gap: 8px; padding: 7px 12px; border-radius: 999px;
          border: 1px solid var(--ring); background: var(--surface); font-size: 11px; font-weight: 700;
          letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-2); }
  .pill .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  .pill.live { color: var(--bad); border-color: color-mix(in oklab, var(--bad) 40%, transparent); }
  .pill.on .dot { background: var(--good-mark); box-shadow: 0 0 0 4px color-mix(in oklab, var(--good-mark) 25%, transparent); }
  .pill.halt .dot { background: var(--warn-mark); }
  button { font: 600 13px var(--sans); padding: 8px 14px; border-radius: 9px; border: 1px solid var(--ring);
           background: var(--surface); color: var(--ink); cursor: pointer; }
  button:hover { background: var(--surface-2); }
  button.pause { background: var(--gold); border-color: transparent; color: #1a1405; }
  button.resume { background: var(--good-mark); border-color: transparent; color: #fff; }
  button.stop { background: var(--bad); border-color: transparent; color: #fff; }
  button.ghost { background: transparent; }
  .theme { width: 36px; padding: 8px 0; text-align: center; }

  .subline { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin: 10px 0 0;
             color: var(--muted); font-size: 12px; }
  .banner { display: none; align-items: center; gap: 12px; padding: 12px 16px; border-radius: 12px; margin: 14px 0 0;
            border: 1px solid var(--ring); background: var(--surface); font-size: 13px; }
  .banner.show { display: flex; }
  .banner.warn { border-color: color-mix(in oklab, var(--warn-mark) 55%, transparent); background: color-mix(in oklab, var(--warn-mark) 12%, var(--surface)); }
  .banner.halt { border-color: color-mix(in oklab, var(--bad) 55%, transparent); background: color-mix(in oklab, var(--bad) 10%, var(--surface)); }
  .banner b { font-weight: 600; } .banner .spacer { flex: 1; }
  #err { color: var(--bad); }

  /* tabs */
  .tabs { display: flex; gap: 4px; margin: 18px 0 16px; border-bottom: 1px solid var(--hair); }
  .tab { padding: 10px 14px; border: none; border-bottom: 2px solid transparent; border-radius: 0; background: transparent;
         color: var(--muted); font-weight: 600; font-size: 13px; letter-spacing: 0.02em; }
  .tab:hover { background: transparent; color: var(--ink); }
  .tab.active { color: var(--ink); border-bottom-color: var(--gold); }
  .tab .n { margin-left: 6px; color: var(--muted); font-weight: 500; font-variant-numeric: tabular-nums; }
  .view { display: none; } .view.active { display: block; }

  /* cards and figures */
  .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px; }
  .card { background: var(--surface); border: 1px solid var(--ring); border-radius: 14px; padding: 18px 20px; }
  .hero { grid-column: span 5; display: flex; flex-direction: column; justify-content: space-between; min-height: 186px;
          background: linear-gradient(160deg, var(--surface) 0%, color-mix(in oklab, var(--gold) 7%, var(--surface)) 100%); }
  .tile { grid-column: span 7; display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
  .tile .card { padding: 16px 18px; }
  .label { color: var(--muted); font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; }
  .value { font: 600 26px/1.15 var(--serif); margin-top: 6px; font-variant-numeric: tabular-nums; }
  .value.hero-fig { font-size: 54px; line-height: 1.02; margin-top: 10px; }
  .delta { color: var(--ink-2); font-size: 13px; margin-top: 5px; }
  .up { color: var(--good); } .down { color: var(--bad); } .flat { color: var(--ink-2); }
  .meter { margin-top: 12px; }
  .meter .track { height: 6px; border-radius: 3px; background: var(--surface-2); overflow: hidden; }
  .meter .fill { height: 100%; background: var(--gold); border-radius: 3px; transition: width .4s ease; }
  .meter .fill.warn { background: var(--warn-mark); } .meter .fill.crit { background: var(--bad); }
  .meter .cap { display: flex; justify-content: space-between; color: var(--muted); font-size: 12px; margin-top: 6px; }
  .chart { grid-column: span 12; }
  .chart-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; gap: 12px; flex-wrap: wrap; }
  .chart-head .label { margin: 0; }
  .chart-head .value { font: 600 14px var(--sans); margin: 0; color: var(--ink-2); }
  .plot { position: relative; height: 230px; }
  .plot svg { width: 100%; height: 100%; display: block; }
  .plot .grid-line { stroke: var(--hair); stroke-width: 1; }
  .plot .base { stroke: var(--axis); stroke-width: 1; }
  .plot .line { fill: none; stroke: var(--gold); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
  .plot .area { fill: var(--gold); opacity: 0.10; }
  .plot .xhair { stroke: var(--axis); stroke-width: 1; display: none; }
  .plot .pt { fill: var(--gold); stroke: var(--surface); stroke-width: 2; display: none; }
  .plot .end { fill: var(--gold); stroke: var(--surface); stroke-width: 2; }
  .plot text { fill: var(--muted); font-size: 11px; }
  .plot .endlab { fill: var(--ink); font-size: 12px; font-weight: 600; }
  .tip { position: absolute; pointer-events: none; display: none; background: var(--raised);
         border: 1px solid var(--ring); border-radius: 10px; padding: 8px 10px; font-size: 12px;
         box-shadow: 0 8px 24px rgba(0,0,0,0.18); white-space: nowrap; }
  .tip b { font-weight: 600; }
  .tip .k { display: inline-block; width: 14px; height: 2px; background: var(--gold); vertical-align: middle; margin-right: 6px; }
  .empty { color: var(--muted); font-size: 13px; padding: 60px 0; text-align: center; }
  .split { grid-column: span 12; display: grid; grid-template-columns: 2fr 1fr; gap: 14px; }
  .note { color: var(--ink-2); font-size: 13px; }

  /* tables */
  .tablewrap { overflow-x: auto; margin-top: 10px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--hair); white-space: nowrap; }
  th { color: var(--muted); font-weight: 700; font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; user-select: none; }
  th.sortable { cursor: pointer; } th.sortable:hover { color: var(--ink); }
  th.sorted::after { content: " ▾"; color: var(--gold); } th.sorted.asc::after { content: " ▴"; }
  tr:last-child td { border-bottom: none; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  tbody tr.row { cursor: pointer; } tbody tr.row:hover td { background: var(--surface-2); }
  tbody tr.row.open td { background: color-mix(in oklab, var(--gold) 10%, var(--surface)); }
  tr.detail td { background: var(--surface-2); white-space: normal; padding: 12px 14px 14px; }
  .detail-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px 16px; font-size: 12px; }
  .detail-grid .k { color: var(--muted); font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; }
  .detail-grid .v { font-variant-numeric: tabular-nums; }
  .reason { margin-top: 10px; font-size: 12px; color: var(--ink-2); }
  .mono { font-family: var(--mono); font-size: 12px; color: var(--ink-2); }
  .side { display: inline-block; min-width: 34px; text-align: center; padding: 2px 6px; border-radius: 6px;
          font-size: 11px; font-weight: 700; letter-spacing: 0.06em; border: 1px solid var(--ring); color: var(--ink-2); }
  .side.yes { border-color: color-mix(in oklab, var(--good) 45%, transparent); color: var(--good); }
  .side.no { border-color: color-mix(in oklab, var(--bad) 45%, transparent); color: var(--bad); }
  .tag { display: inline-block; padding: 2px 7px; border-radius: 6px; font-size: 11px; font-weight: 600; background: var(--surface-2); color: var(--ink-2); }
  .tag.gold { background: var(--gold-soft); color: var(--gold-ink); }
  .filters { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 4px; }
  select, input[type=search] { font: 13px var(--sans); padding: 7px 10px; border-radius: 8px; border: 1px solid var(--ring);
           background: var(--surface); color: var(--ink); }
  .chip { display: inline-flex; align-items: center; gap: 6px; padding: 5px 10px; border-radius: 999px; background: var(--gold-soft);
          color: var(--gold-ink); font-size: 12px; font-weight: 600; }
  .chip button { padding: 0 4px; border: none; background: transparent; color: inherit; font-size: 14px; line-height: 1; }

  .open-list { display: flex; flex-direction: column; gap: 10px; margin-top: 10px; }
  .open-item { display: flex; justify-content: space-between; gap: 10px; padding: 10px 12px; border-radius: 10px;
               background: var(--surface-2); font-size: 13px; }
  .open-item .t { color: var(--muted); font-variant-numeric: tabular-nums; }
  .series-row { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid var(--hair); font-size: 13px; }
  .series-row:last-child { border-bottom: none; }
  .series-row .n { color: var(--ink-2); font-variant-numeric: tabular-nums; }

  /* analysis */
  .analysis { display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px; }
  .analysis .card { grid-column: span 6; }
  .analysis .card.wide { grid-column: span 12; }
  .analysis tbody tr { cursor: pointer; } .analysis tbody tr:hover td { background: var(--surface-2); }
  .analysis tbody tr.picked td { background: color-mix(in oklab, var(--gold) 12%, var(--surface)); }
  .bar { display: inline-block; height: 8px; border-radius: 4px; vertical-align: middle; background: var(--gold); opacity: .8; }
  .bar.neg { background: var(--bad); }
  ul.tips { margin: 8px 0 0; padding-left: 18px; font-size: 13px; color: var(--ink-2); }
  ul.tips li { margin: 4px 0; }

  /* activity */
  .ev-list { display: flex; flex-direction: column; margin-top: 8px; }
  .ev { display: grid; grid-template-columns: 64px 12px 78px 1fr; gap: 10px; align-items: baseline; padding: 9px 0;
        border-bottom: 1px solid var(--hair); font-size: 13px; }
  .ev:last-child { border-bottom: none; }
  .ev .t { color: var(--muted); font-variant-numeric: tabular-nums; font-size: 12px; }
  .ev .lvl { width: 8px; height: 8px; border-radius: 50%; background: var(--axis); position: relative; top: -1px; }
  .ev.warn .lvl { background: var(--warn-mark); } .ev.halt .lvl { background: var(--bad); }
  .ev.halt .x { color: var(--bad); font-weight: 600; } .ev.warn .x { color: var(--warn); }
  .ev .src { color: var(--muted); font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; }

  footer { margin-top: 36px; padding-top: 14px; border-top: 1px solid var(--hair); display: flex; justify-content: space-between;
           gap: 12px; flex-wrap: wrap; color: var(--muted); font-size: 12px; }
  footer .creed { font: italic 13px var(--serif); color: var(--gold-ink); }

  @media (max-width: 900px) {
    .hero, .tile, .split, .analysis .card { grid-column: span 12; }
    .split { grid-template-columns: 1fr; }
    .tile { grid-template-columns: 1fr 1fr; }
    .value.hero-fig { font-size: 42px; }
    .wordmark .name { font-size: 18px; }
  }
</style>
</head>
<body>
<main>
  <header class="mast">
    <div class="brand">
      <svg viewBox="0 0 64 64" role="img" aria-label="Lewis Wealth Global">
        <defs>
          <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stop-color="#e2c56b"/><stop offset="1" stop-color="#a8822a"/>
          </linearGradient>
        </defs>
        <circle cx="32" cy="32" r="30" fill="var(--navy)" stroke="url(#g)" stroke-width="2.5"/>
        <path d="M12 32a20 20 0 0 1 40 0" fill="none" stroke="url(#g)" stroke-width="1.2" opacity=".6"/>
        <path d="M12 32a20 20 0 0 0 40 0" fill="none" stroke="url(#g)" stroke-width="1.2" opacity=".35"/>
        <ellipse cx="32" cy="32" rx="8" ry="20" fill="none" stroke="url(#g)" stroke-width="1" opacity=".35"/>
        <line x1="12" y1="32" x2="52" y2="32" stroke="url(#g)" stroke-width="1" opacity=".35"/>
        <path d="M21 20v22h11" fill="none" stroke="url(#g)" stroke-width="4" stroke-linecap="square"/>
        <path d="M31 24l4 15 4-11 4 11 4-15" fill="none" stroke="url(#g)" stroke-width="2.8" stroke-linejoin="round" stroke-linecap="round"/>
      </svg>
      <div class="wordmark">
        <div class="name">Lewis <span>Wealth</span> Global</div>
        <div class="creed">Faith without Works is Dead. God Move.</div>
      </div>
    </div>
    <div class="controls">
      <div class="pill" id="pill"><span class="dot"></span><span id="pilltext">–</span></div>
      <button class="pause" id="pausebtn" onclick="post('/api/pause')">Pause entries</button>
      <button class="resume" id="resumebtn" onclick="post('/api/resume')" style="display:none">Resume</button>
      <button class="stop" onclick="post('/api/stop')">Stop loop</button>
      <button class="ghost" onclick="post('/api/clear-stop')">Clear stop file</button>
      <button class="ghost theme" id="themebtn" title="Toggle light and dark" onclick="toggleTheme()">◐</button>
    </div>
  </header>
  <div class="subline"><span id="cfg">connecting…</span><span id="err"></span></div>
  <div class="banner" id="banner"><span id="bannertext"></span><span class="spacer"></span><span class="note" id="bannernote"></span></div>

  <nav class="tabs">
    <button class="tab active" data-view="overview" onclick="showView('overview')">Overview</button>
    <button class="tab" data-view="trades" onclick="showView('trades')">Trades<span class="n" id="ntrades"></span></button>
    <button class="tab" data-view="analysis" onclick="showView('analysis')">Analysis</button>
    <button class="tab" data-view="activity" onclick="showView('activity')">Activity<span class="n" id="nevents"></span></button>
  </nav>

  <section class="view active" id="view-overview">
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
        <div class="card"><div class="label">Results</div><div class="value" id="trades">–</div><div class="delta" id="record"></div></div>
        <div class="card"><div class="label">Win rate</div><div class="value" id="winrate">–</div><div class="delta" id="avgnet"></div></div>
        <div class="card"><div class="label">Fees paid</div><div class="value" id="fees">–</div><div class="delta" id="feenote"></div></div>
        <div class="card"><div class="label">Stake</div><div class="value" id="size">–</div><div class="delta" id="sizenote"></div></div>
        <div class="card"><div class="label">Strategy</div><div class="value" id="strategy">–</div><div class="delta" id="stratnote"></div></div>
        <div class="card"><div class="label">Heartbeat</div><div class="value" id="tick">–</div><div class="delta" id="ticknote"></div></div>
      </section>
      <section class="card chart">
        <div class="chart-head"><div class="label">Equity curve, cumulative net by result</div><div class="value" id="curvenote"></div></div>
        <div class="plot" id="plot"><div class="tip" id="tip"></div></div>
      </section>
      <section class="split">
        <div class="card">
          <div class="label">Latest results</div>
          <div class="tablewrap"><table><thead><tr>
            <th>Time</th><th>Market</th><th>Side</th><th class="num">Qty</th><th class="num">Price</th><th>Result</th><th class="num">Net</th>
          </tr></thead><tbody id="recent"></tbody></table></div>
          <div class="note" style="margin-top:8px"><a href="#" onclick="showView('trades');return false">All trades, sortable and filterable →</a></div>
        </div>
        <div>
          <div class="card" style="margin-bottom:14px">
            <div class="label">Open positions</div>
            <div class="open-list" id="open"><div class="note">none</div></div>
          </div>
          <div class="card">
            <div class="label">By series</div>
            <div id="series" style="margin-top:6px"><div class="note">nothing booked yet</div></div>
          </div>
        </div>
      </section>
    </div>
  </section>

  <section class="view" id="view-trades">
    <div class="card">
      <div class="chart-head">
        <div class="label">Every booked result</div>
        <div class="filters">
          <select id="f-series" onchange="renderTrades()"><option value="">All series</option></select>
          <select id="f-side" onchange="renderTrades()"><option value="">Both sides</option><option value="yes">YES</option><option value="no">NO</option></select>
          <select id="f-how" onchange="renderTrades()"><option value="">Sold and settled</option><option value="sold">Sold</option><option value="settled">Settled</option></select>
          <input type="search" id="f-text" placeholder="market…" oninput="renderTrades()">
          <span id="f-chip"></span>
        </div>
      </div>
      <div class="tablewrap"><table id="tradetable"><thead><tr>
        <th class="sortable" data-k="settled_ts">Time</th>
        <th class="sortable" data-k="ticker">Market</th>
        <th class="sortable" data-k="side">Side</th>
        <th class="sortable num" data-k="count">Qty</th>
        <th class="sortable num" data-k="price">Entry</th>
        <th class="sortable" data-k="maker">Type</th>
        <th class="sortable num" data-k="p_side">Conf.</th>
        <th class="sortable num" data-k="secs_to_close">To close</th>
        <th class="sortable" data-k="result">Result</th>
        <th class="sortable num" data-k="fee">Fee</th>
        <th class="sortable num" data-k="net">Net</th>
      </tr></thead><tbody id="trades-body"></tbody></table></div>
      <div class="note" id="trades-foot" style="margin-top:10px"></div>
    </div>
  </section>

  <section class="view" id="view-analysis">
    <div class="analysis" id="analysis-grid">
      <div class="card wide">
        <div class="chart-head"><div class="label">Where the money went</div><div class="value" id="an-head"></div></div>
        <div class="note">Click any row to see just those trades.</div>
      </div>
      <div class="card"><div class="label">How it ended</div><div class="tablewrap" id="an-how"></div></div>
      <div class="card"><div class="label">Side</div><div class="tablewrap" id="an-side"></div></div>
      <div class="card"><div class="label">Model confidence for the side bought</div><div class="tablewrap" id="an-confidence"></div></div>
      <div class="card"><div class="label">Seconds to close at entry</div><div class="tablewrap" id="an-ttc"></div></div>
      <div class="card"><div class="label">Spot distance from strike at entry</div><div class="tablewrap" id="an-distance"></div></div>
      <div class="card"><div class="label">Series</div><div class="tablewrap" id="an-series"></div></div>
      <div class="card wide">
        <div class="chart-head"><div class="label">Confidence tiers that gate sizing</div><div class="value" id="tiernote"></div></div>
        <div class="tablewrap" id="an-tiers"></div>
      </div>
      <div class="card wide">
        <div class="label">What the numbers support</div>
        <ul class="tips" id="tips"><li class="note">nothing booked yet</li></ul>
      </div>
    </div>
  </section>

  <section class="view" id="view-activity">
    <div class="card">
      <div class="chart-head"><div class="label">Activity, latest first</div><div class="value" id="evnote"></div></div>
      <div class="ev-list" id="events"><div class="note">no events yet</div></div>
    </div>
  </section>

  <footer>
    <span>Lewis Wealth Global · Kalshi 15-minute desk</span>
    <span class="creed">Faith without Works is Dead. God Move.</span>
    <span id="foot-file"></span>
  </footer>
</main>
<script>
const $ = id => document.getElementById(id);
const fmt = (x, d) => (x === null || x === undefined || Number.isNaN(Number(x))) ? '–' : Number(x).toFixed(d);
const money = x => { const v = Number(x || 0); return (v < 0 ? '−$' : '$') + Math.abs(v).toFixed(2); };
const signed = x => { const v = Number(x || 0); return (v > 0 ? '+' : v < 0 ? '−' : '') + '$' + Math.abs(v).toFixed(2); };
const tm = t => t ? new Date(t * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}) : '';
const dt = t => t ? new Date(t * 1000).toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : '';
const cls = v => v > 0 ? 'up' : v < 0 ? 'down' : 'flat';
const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
const sideTag = s => `<span class="side ${s}">${String(s || '').toUpperCase()}</span>`;
let analysis = null, snap = null, picked = null, sortKey = 'settled_ts', sortAsc = false, openRow = null, view = 'overview';

// theme -------------------------------------------------------------
function applyTheme() {
  let t = null; try { t = localStorage.getItem('lwg-theme'); } catch (e) {}
  if (t) document.documentElement.setAttribute('data-theme', t); else document.documentElement.removeAttribute('data-theme');
}
function toggleTheme() {
  const dark = document.documentElement.getAttribute('data-theme') === 'dark' ||
    (!document.documentElement.getAttribute('data-theme') && window.matchMedia('(prefers-color-scheme: dark)').matches);
  try { localStorage.setItem('lwg-theme', dark ? 'light' : 'dark'); } catch (e) {}
  applyTheme(); if (snap) curve((snap.state || {}).history || []);
}
applyTheme();

// views -------------------------------------------------------------
function showView(name) {
  view = name;
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.view === name));
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
  if (name === 'trades') renderTrades();
  if (name === 'analysis') renderAnalysis();
  if (name === 'overview' && snap) curve((snap.state || {}).history || []);
}
async function post(path) {
  try { await fetch(path, {method: 'POST'}); await refresh(); }
  catch (e) { $('err').textContent = String(e); }
}

// overview ----------------------------------------------------------
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
  if (!hist.length) { const e = document.createElement('div'); e.className = 'empty'; e.textContent = 'The curve starts at the first result.'; plot.appendChild(e); $('curvenote').textContent = ''; return; }
  const W = plot.clientWidth || 800, H = plot.clientHeight || 230, L = 64, R = 76, T = 14, B = 26;
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
  for (let k = 0; k <= 4; k++) {
    const v = lo + (hi - lo) * k / 4; const yy = y(v);
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
  mk('text', {x: L, y: H - 6}, hist[0] ? dt(hist[0].settled_ts) : '');
  mk('text', {x: W - R, y: H - 6, 'text-anchor': 'end'}, dt(last.t));
  const xh = mk('line', {y1: T, y2: H - B, class: 'xhair'});
  const pt = mk('circle', {r: 5, class: 'pt'});
  svg.addEventListener('mousemove', ev => {
    const r = svg.getBoundingClientRect(); const mx = (ev.clientX - r.left) * W / r.width;
    let best = pts[0]; for (const p of pts) if (Math.abs(x(p.i) - mx) < Math.abs(x(best.i) - mx)) best = p;
    xh.setAttribute('x1', x(best.i)); xh.setAttribute('x2', x(best.i)); xh.style.display = 'block';
    pt.setAttribute('cx', x(best.i)); pt.setAttribute('cy', y(best.y)); pt.style.display = 'block';
    tip.style.display = 'block';
    tip.innerHTML = best.h
      ? `<div><span class="k"></span><b>${signed(best.y)}</b> cumulative</div><div class="note">${dt(best.t)} · ${esc(best.h.ticker)}</div><div class="note">${String(best.h.side).toUpperCase()} ×${fmt(best.h.count, 0)} @ ${fmt(best.h.price, 2)} → ${String(best.h.result).toUpperCase()} · <span class="${cls(best.h.net)}">${signed(best.h.net)}</span></div>`
      : `<div><span class="k"></span><b>$0.00</b> start</div>`;
    const px = x(best.i) * r.width / W; const left = px + 14 + tip.offsetWidth > r.width ? px - tip.offsetWidth - 14 : px + 14;
    tip.style.left = left + 'px'; tip.style.top = Math.max(0, y(best.y) * r.height / H - 40) + 'px';
  });
  svg.addEventListener('mouseleave', () => { xh.style.display = 'none'; pt.style.display = 'none'; tip.style.display = 'none'; });
  plot.appendChild(svg);
  $('curvenote').textContent = `peak ${signed(Math.max(...ys))} · trough ${signed(Math.min(...ys))}`;
}
function renderOverview(d) {
  const s = d.state || {}; const c = s.config || {};
  const live = c.env === 'LIVE';
  document.title = (live ? 'LIVE · ' : '') + 'Lewis Wealth Global';
  const series = Array.isArray(c.series) ? c.series.join(' · ') : (c.series || '');
  $('cfg').textContent = d.state ? `${live ? 'Real money' : (c.env || 'idle')} · ${series}` : `no state file yet at ${d.state_file}`;
  $('foot-file').textContent = d.state_file || '';
  const hb = d.heartbeat || 'none';
  const pill = $('pill'); pill.className = 'pill' + (live ? ' live' : '') + (s.halted || hb === 'paused' || hb === 'stale' ? ' halt' : d.alive ? ' on' : '');
  $('pilltext').textContent = s.halted ? 'halted' : hb === 'paused' ? (live ? 'live · paused' : 'paused') : hb === 'stale' ? 'no heartbeat' : d.alive ? (live ? 'live · running' : 'running') : (s.stopped ? 'stopped' : 'not running');
  const paused = d.pause_file_present;
  $('pausebtn').style.display = paused ? 'none' : '';
  $('resumebtn').style.display = paused ? '' : 'none';
  const banner = $('banner');
  if (hb === 'stale') { banner.className = 'banner show halt'; $('bannertext').innerHTML = `<b>No heartbeat.</b> The loop last ticked at ${tm(s.last_tick_ts)} and has gone quiet: it is not trading and not watching its positions.`; $('bannernote').textContent = 'restart it in its window, or on the server'; }
  else if (s.halted) { banner.className = 'banner show halt'; $('bannertext').innerHTML = `<b>Halted.</b> ${esc(s.halted)}`; $('bannernote').textContent = d.alive ? 'open positions settle, then the loop exits' : (s.stopped ? 'the loop has exited' : ''); }
  else if (s.breaker_until && s.breaker_until > d.now) { banner.className = 'banner show warn'; $('bannertext').innerHTML = `<b>Loss breaker.</b> ${s.loss_streak || ''} losing results in a row; no new entries until ${tm(s.breaker_until)}. Open positions are still managed.`; $('bannernote').textContent = `${Math.ceil((s.breaker_until - d.now) / 60)} min left`; }
  else if (paused) { banner.className = 'banner show warn'; $('bannertext').innerHTML = `<b>Paused.</b> Nothing new is opened; open positions are still managed and settle normally.`; $('bannernote').textContent = d.alive ? 'Resume to trade again' : 'the loop is not running'; }
  else if (d.stop_file_present) { banner.className = 'banner show warn'; $('bannertext').innerHTML = `<b>Stop file present.</b> The loop exits and will not start until it is cleared.`; $('bannernote').textContent = d.stop_file; }
  else { banner.className = 'banner'; }
  const p = Number(s.realized_pnl || 0);
  const pnl = $('pnl'); pnl.textContent = signed(p); pnl.className = 'value hero-fig ' + cls(p);
  const hist = s.history || [];
  $('pnlnote').textContent = s.halted ? s.halted : (s.stopped ? 'stopped: ' + s.stopped : (hist.length ? `last result ${dt(hist[hist.length - 1].settled_ts)}` : 'no results yet'));
  meter(p, c.loss_cap);
  $('trades').textContent = s.trades ?? '–';
  $('record').textContent = `${s.wins || 0} won · ${s.losses || 0} lost` + (c.max_trades ? ` · max ${c.max_trades}` : '');
  const settled = (s.wins || 0) + (s.losses || 0);
  $('winrate').textContent = settled ? Math.round(100 * (s.wins || 0) / settled) + '%' : '–';
  $('avgnet').textContent = settled ? `${signed(p / settled)} per result` : '';
  $('fees').textContent = money(s.fees_paid || 0);
  $('feenote').textContent = settled ? `${money((s.fees_paid || 0) / settled)} per result` : '';
  $('size').textContent = c.dollars ? '$' + fmt(c.dollars, 2) : (c.contracts ? c.contracts + ' ct' : '–');
  $('sizenote').textContent = (c.max_dollars ? `up to $${fmt(c.max_dollars, 0)} once a tier has earned it · ` : '') + `max price ${fmt(c.max_price, 2)}` + (c.profit_target ? ` · target +$${fmt(c.profit_target, 2)}` : ' · no profit cap');
  $('sizenote').title = c.track_record || '';
  $('strategy').textContent = c.strategy === 'fairvalue' ? 'Fair value' : c.strategy === 'alternate' ? 'Alternate' : (c.strategy || '–');
  $('stratnote').textContent = c.strategy === 'fairvalue' ? `margin ${fmt(c.margin, 2)} · vol ${Math.round((c.vol_window || 0) / 60)} min` : (c.strategy === 'alternate' ? 'YES / NO in turn' : '');
  $('tick').textContent = s.last_tick_ts ? tm(s.last_tick_ts) : '–';
  $('ticknote').textContent = s.last_tick_ts ? `${Math.max(0, Math.round(d.now - s.last_tick_ts))}s ago` : '';
  if (view === 'overview') curve(hist);
  const opens = Object.entries(s.series || {}).filter(([, v]) => v && v.open).map(([n, v]) => [n, v.open]);
  $('open').innerHTML = opens.length ? opens.map(([n, o]) => {
    const left = Math.max(0, Math.round(o.close_ts - d.now));
    return `<div class="open-item"><div>${sideTag(o.side)} ×${o.count} <span class="mono">${esc(o.ticker)}</span><br><span class="note">${o.filled_count > 0 ? 'filled at ' + fmt(o.fill_price, 3) : 'resting at ' + fmt(o.limit_price, 3)}${o.maker ? ' · maker' : ''}</span></div><div class="t">${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}</div></div>`;
  }).join('') : '<div class="note">none</div>';
  const bySeries = {};
  hist.forEach(h => { const k = h.series || (h.ticker || '').split('-')[0]; const b = bySeries[k] = bySeries[k] || {n: 0, w: 0, net: 0}; b.n++; b.w += h.won ? 1 : 0; b.net += Number(h.net || 0); });
  const keys = Object.keys(bySeries);
  $('series').innerHTML = keys.length ? keys.map(k => `<div class="series-row"><span>${esc(k)}</span><span class="n">${bySeries[k].n} results · ${Math.round(100 * bySeries[k].w / bySeries[k].n)}% · <span class="${cls(bySeries[k].net)}">${signed(bySeries[k].net)}</span></span></div>`).join('') : '<div class="note">nothing booked yet</div>';
  $('recent').innerHTML = hist.slice().reverse().slice(0, 12).map(h =>
    `<tr><td>${tm(h.settled_ts)}</td><td class="mono">${esc(h.ticker)}</td><td>${sideTag(h.side)}</td><td class="num">${fmt(h.count, 0)}</td><td class="num">${fmt(h.price, 3)}</td><td>${String(h.result).toUpperCase()}</td><td class="num ${cls(h.net)}">${signed(h.net)}</td></tr>`
  ).join('') || '<tr><td colspan="7" class="note">nothing booked yet</td></tr>';
  $('ntrades').textContent = hist.length || '';
  const evs = (d.alerts || []).slice().sort((a, b) => Number(b.ts || 0) - Number(a.ts || 0));
  const warnCount = evs.filter(e => e.level !== 'info').length;
  $('nevents').textContent = evs.length || '';
  $('evnote').textContent = evs.length ? `${evs.length} shown` + (warnCount ? ` · ${warnCount} need attention` : '') : '';
  $('events').innerHTML = evs.length ? evs.map(e =>
    `<div class="ev ${esc(e.level || 'info')}"><span class="t">${tm(e.ts)}</span><span class="lvl"></span><span class="src">${esc(e.source || '')}</span><span class="x">${esc(e.text)}</span></div>`
  ).join('') : '<div class="note">no events yet</div>';
}

// trades ------------------------------------------------------------
function rowsFiltered() {
  if (!analysis) return [];
  const fs = $('f-series').value, fd = $('f-side').value, fh = $('f-how').value, ft = $('f-text').value.trim().toLowerCase();
  return analysis.rows.filter(r =>
    (!fs || r.series === fs) && (!fd || r.side === fd) && (!fh || r.how === fh) &&
    (!ft || String(r.ticker || '').toLowerCase().includes(ft)) &&
    (!picked || picked.match(r)));
}
function pick(label, match) { picked = {label, match}; $('f-chip').innerHTML = `<span class="chip">${esc(label)}<button onclick="clearPick()" title="clear">×</button></span>`; showView('trades'); }
function clearPick() { picked = null; $('f-chip').innerHTML = ''; renderTrades(); renderAnalysis(); }
function renderTrades() {
  if (!analysis) return;
  const sel = $('f-series'); const have = new Set([...sel.options].map(o => o.value));
  [...new Set(analysis.rows.map(r => r.series).filter(Boolean))].forEach(sname => { if (!have.has(sname)) { const o = document.createElement('option'); o.value = sname; o.textContent = sname; sel.appendChild(o); } });
  const rows = rowsFiltered().slice().sort((a, b) => {
    let va = a[sortKey], vb = b[sortKey];
    if (sortKey === 'maker') { va = a.maker ? 1 : 0; vb = b.maker ? 1 : 0; }
    if (va === null || va === undefined) return 1; if (vb === null || vb === undefined) return -1;
    if (typeof va === 'string') return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
    return sortAsc ? va - vb : vb - va;
  });
  document.querySelectorAll('#tradetable th.sortable').forEach(th => { th.classList.toggle('sorted', th.dataset.k === sortKey); th.classList.toggle('asc', th.dataset.k === sortKey && sortAsc); });
  const net = rows.reduce((a, r) => a + Number(r.net || 0), 0), wins = rows.filter(r => r.net > 0).length;
  $('trades-foot').textContent = rows.length ? `${rows.length} results · ${Math.round(100 * wins / rows.length)}% won · net ${signed(net)}` : 'no results match';
  $('trades-body').innerHTML = rows.map(r =>
    `<tr class="row${openRow === r.id ? ' open' : ''}" data-id="${r.id}" onclick="toggleRow(${r.id})"><td>${dt(r.settled_ts)}</td><td class="mono">${esc(r.ticker)}</td><td>${sideTag(r.side)}</td><td class="num">${fmt(r.count, 0)}</td><td class="num">${fmt(r.price, 3)}</td><td>${r.maker === true ? '<span class="tag gold">maker</span>' : r.maker === false ? '<span class="tag">taker</span>' : ''}</td><td class="num">${r.p_side === null || r.p_side === undefined ? '–' : Math.round(r.p_side * 100) + '%'}</td><td class="num">${r.secs_to_close ? Math.round(r.secs_to_close / 60) + ' min' : '–'}</td><td>${String(r.result || '').toUpperCase()}${r.sold_at ? ' @ ' + fmt(r.sold_at, 3) : ''}</td><td class="num">${r.fee !== undefined && r.fee !== null ? money(r.fee) : '–'}</td><td class="num ${cls(r.net)}">${signed(r.net)}</td></tr>` +
    (openRow === r.id ? `<tr class="detail"><td colspan="11" id="detail-${r.id}"><span class="note">loading…</span></td></tr>` : '')
  ).join('') || '<tr><td colspan="11" class="note">nothing booked yet</td></tr>';
  if (openRow !== null) { const r = analysis.rows.find(x => x.id === openRow); if (r) loadDetail(r); }
}
document.querySelectorAll('#tradetable th.sortable').forEach(th => th.addEventListener('click', () => { const k = th.dataset.k; if (sortKey === k) sortAsc = !sortAsc; else { sortKey = k; sortAsc = k === 'ticker' || k === 'side'; } renderTrades(); }));
function toggleRow(id) { openRow = openRow === id ? null : id; renderTrades(); }
async function loadDetail(r) {
  const cell = $('detail-' + r.id); if (!cell) return;
  let decs = [];
  try { decs = await (await fetch('/api/decisions?ticker=' + encodeURIComponent(r.ticker))).json(); } catch (e) {}
  const entry = decs.filter(d => d.action === 'trade' && d.side === r.side && Number(d.ts) <= Number(r.settled_ts)).pop();
  const exits = decs.filter(d => d.action === 'exit');
  const inp = (entry && entry.inputs) || {};
  const kv = [
    ['Entered', entry ? dt(entry.ts) : '–'], ['Stake', entry && entry.dollars ? '$' + fmt(entry.dollars, 2) + (entry.scaled ? ' (scaled)' : '') : '–'],
    ['Model p(YES)', fmt(inp.p_yes, 3)], ['Edge', entry && entry.edge !== undefined ? fmt(entry.edge, 3) : '–'],
    ['Spot', inp.spot !== undefined ? fmt(inp.spot, inp.spot < 10 ? 5 : 2) : '–'], ['Strike', inp.strike !== undefined ? fmt(inp.strike, inp.strike < 10 ? 5 : 2) : '–'],
    ['Spot vs strike', inp.spot_vs_strike_bps !== undefined ? fmt(inp.spot_vs_strike_bps, 1) + ' bps' : '–'], ['Trend 5 min', inp.trend_bps !== undefined ? fmt(inp.trend_bps, 1) + ' bps' : '–'],
    ['Ann. vol', inp.ann_vol !== undefined ? Math.round(inp.ann_vol * 100) + '%' + (inp.ann_vol_raw !== undefined ? ` (raw ${Math.round(inp.ann_vol_raw * 100)}%)` : '') : '–'], ['To close', inp.secs_to_close !== undefined ? Math.round(inp.secs_to_close) + ' s' : '–'],
    ['YES ask / NO ask', inp.yes_ask !== undefined ? `${fmt(inp.yes_ask, 3)} / ${fmt(inp.no_ask, 3)}` : '–'], ['Fee booked', r.fee !== undefined && r.fee !== null ? money(r.fee) + (r.fee_reported ? ' (exchange)' : ' (model)') : '–'],
  ];
  cell.innerHTML = `<div class="detail-grid">${kv.map(([k, v]) => `<div><div class="k">${k}</div><div class="v">${esc(v)}</div></div>`).join('')}</div>` +
    (entry ? `<div class="reason"><b>Entry:</b> ${esc(entry.reason)}</div>` : '<div class="reason">No decision-log entry matched this result.</div>') +
    exits.map(x => `<div class="reason"><b>Exit ${tm(x.ts)}:</b> ${esc(x.reason)}</div>`).join('');
}

// analysis ----------------------------------------------------------
function anTable(id, cuts, matcher) {
  const el = $('an-' + id); if (!el) return;
  if (!cuts || !cuts.length) { el.innerHTML = '<div class="note" style="margin-top:8px">nothing booked yet</div>'; return; }
  const maxAbs = Math.max(...cuts.map(c => Math.abs(c.net)), 0.01);
  el.innerHTML = `<table><thead><tr><th>Bucket</th><th class="num">n</th><th class="num">Win</th><th class="num">Per</th><th class="num">Net</th><th></th></tr></thead><tbody>` +
    cuts.map(c => `<tr class="${picked && picked.label === id + ': ' + c.bucket ? 'picked' : ''}" onclick="pickBucket('${id}', '${esc(c.bucket)}')"><td>${esc(c.bucket)}</td><td class="num">${c.n}</td><td class="num">${Math.round(c.win_rate * 100)}%</td><td class="num ${cls(c.per_trade)}">${signed(c.per_trade)}</td><td class="num ${cls(c.net)}">${signed(c.net)}</td><td><span class="bar ${c.net < 0 ? 'neg' : ''}" style="width:${Math.round(60 * Math.abs(c.net) / maxAbs)}px"></span></td></tr>`).join('') + '</tbody></table>';
}
const BUCKETS = {
  confidence: {key: 'p_side', edges: [['<0.55', 0, 0.55], ['0.55-0.65', 0.55, 0.65], ['0.65-0.80', 0.65, 0.80], ['>=0.80', 0.80, 1.01]]},
  ttc: {key: 'secs_to_close', edges: [['<3 min', 0, 180], ['3-6 min', 180, 360], ['6-10 min', 360, 600], ['>=10 min', 600, 1e12]]},
  distance: {key: 'strike_bps', abs: true, edges: [['<5 bps', 0, 5], ['5-15 bps', 5, 15], ['15-40 bps', 15, 40], ['>=40 bps', 40, 1e12]]},
};
function pickBucket(id, bucket) {
  let match;
  if (BUCKETS[id]) {
    const spec = BUCKETS[id]; const e = spec.edges.find(x => x[0] === bucket);
    match = r => { let v = r[spec.key]; if (v === null || v === undefined) return bucket === 'unknown'; if (spec.abs) v = Math.abs(v); return e ? v >= e[1] && v < e[2] : false; };
  } else { match = r => String(r[id]) === bucket; }
  pick(id + ': ' + bucket, match);
}
function renderAnalysis() {
  if (!analysis) return;
  const rows = analysis.rows; const net = rows.reduce((a, r) => a + r.net, 0);
  $('an-head').textContent = rows.length ? `${rows.length} results · net ${signed(net)} · before fees ${signed(analysis.gross)} · fees ${money(analysis.fees)}` : '';
  ['how', 'side', 'series', 'confidence', 'ttc', 'distance'].forEach(k => anTable(k, analysis.cuts[k]));
  const tiers = analysis.tiers || [];
  $('tiernote').textContent = `a tier scales above the base stake after ${analysis.min_tier_results} results with a positive net`;
  $('an-tiers').innerHTML = tiers.length ? `<table><thead><tr><th>Tier</th><th class="num">Results</th><th class="num">Win</th><th class="num">Net</th><th>Sizing</th></tr></thead><tbody>` +
    tiers.map(t => `<tr onclick="pickBucket('confidence', '${t.tier === '0.65-0.75' ? '0.65-0.80' : t.tier === '>=0.85' ? '>=0.80' : '0.65-0.80'}')"><td>${esc(t.tier)}</td><td class="num">${t.n}</td><td class="num">${Math.round(t.win_rate * 100)}%</td><td class="num ${cls(t.net)}">${signed(t.net)}</td><td>${t.scaling ? '<span class="tag gold">scaling on</span>' : `<span class="tag">base stake · ${Math.max(0, analysis.min_tier_results - t.n)} more needed</span>`}</td></tr>`).join('') + '</tbody></table>'
    : '<div class="note" style="margin-top:8px">no results in a confidence tier yet (the floor is 0.65)</div>';
  $('tips').innerHTML = (analysis.suggestions || []).map(t => `<li>${esc(t)}</li>`).join('') || '<li class="note">nothing booked yet</li>';
}

// refresh -----------------------------------------------------------
async function refresh() {
  try { snap = await (await fetch('/api/state')).json(); $('err').textContent = ''; }
  catch (e) { $('err').textContent = 'dashboard server unreachable'; return; }
  renderOverview(snap);
  try { analysis = await (await fetch('/api/analysis')).json(); } catch (e) { analysis = analysis || {rows: [], cuts: {}, tiers: [], suggestions: []}; }
  if (view === 'trades') renderTrades();
  if (view === 'analysis') renderAnalysis();
}
refresh(); setInterval(refresh, 3000); window.addEventListener('resize', () => { if (snap && view === 'overview') curve((snap.state || {}).history || []); });
</script>
</body>
</html>
"""
