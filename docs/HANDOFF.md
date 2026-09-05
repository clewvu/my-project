# Kalshi 15-minute crypto bot: handoff

Paste or upload this file at the start of a new chat to continue the work.
It is the complete context as of 2026-09-04 (evening). The code is the source of truth;
this document says what exists, what was learned, what was decided, and what
comes next.

## 1. Goal and current phase

Cameron (cameronlewis49@gmail.com, Windows, PowerShell) wants an automated
trader for Kalshi's 15-minute BTC and DOGE up/down markets, starting from a
$200 bankroll with a stated aim of $1,000. The agreed approach is research
first: record data, test hypotheses against pre-registered thresholds, and
only build a live trader if a test passes out of sample. "Stop, no edge
found" is an explicit acceptable outcome.

Phases:

1. Done: config, RSA-PSS signing, HTTP client, CLI.
2. Done: market-data recorder (books, trades, settlements, spot).
3. Tooling done, awaiting data: research reports (`analyze`, `whale`,
   `fairvalue`). Both pre-registered hypotheses now have a test.
4. Not started: risk engine, decision log / feature store, strategy interface.
5. Not started: demo run with paper orders.
6. Not started: production at minimum size.

The recorder is currently running on Cameron's laptop against production
public endpoints. It started collecting clean data (schema v3) around
2026-09-04 17:00 UTC. Nothing has traded. No API key has been configured yet.

## 2. Repository

- GitHub: `clewvu/my-project`. Work through 2026-09-04 afternoon is on
  `claude/kalshi-trading-automation-45k7jt`; the fair-value test was added on
  `claude/kalshi-crypto-bot-handoff-w39dj8`, which contains that branch plus
  the newer commits. Pull the newer branch. Master has only a devcontainer.
- Local clone on Cameron's machine: `C:\Users\lewiscc2\kalshi-bot`, venv at
  `.venv`, activated with `.\.venv\Scripts\Activate.ps1`.
- Python 3.11+ (Cameron has 3.14). Install: `pip install -e ".[dev]"`.
- Tests: `pytest` (138 passing). Lint: `ruff check . && ruff format .`.
- Commit convention: descriptive message, tests and lint clean before push,
  push with `git push -u origin <branch>`.

Layout:

```
kalshi_bot/
  config.py    Settings from .env: KALSHI_ENV (demo|prod), KALSHI_API_KEY_ID,
               KALSHI_PRIVATE_KEY_PATH, KALSHI_DRY_RUN (default true),
               KALSHI_MIN_REQUEST_INTERVAL, KALSHI_LOG_LEVEL
  auth.py      RSA-PSS signer: sign(timestamp_ms + METHOD + /trade-api/v2/path)
  client.py    KalshiClient: httpx, throttle, retry/backoff on 429/5xx,
               dry_run, allow_live guard; reads are unsigned public endpoints
  models.py    Market, Orderbook, Level, Balance, Position, Order, Fill, Trade,
               Candle. Prices are DOLLARS (float, 0.001 grid). Counts are floats.
  storage.py   MarketDataStore (SQLite, WAL, schema v3 with migrations)
  recorder.py  polling loop: markets, books, trades, settlements, spot
  spot.py      Coinbase REST spot poll (every tick)
  spot_ws.py   Coinbase WebSocket ticker feed (background thread)
  fees.py      Kalshi fee model: 7% x price x (1 - price) per contract,
               rounded up to the cent per order (verify against schedule)
  analysis.py  research report (pandas)
  whale.py     whale-follow hypothesis test (pandas)
  fairvalue.py fair-value model, backtest, verdict, basis measurement (pandas)
  demo_loop.py demo-only alternating YES/NO trader, caps, stop file, JSON state
  demo_ui.py   localhost dashboard for the demo loop (stdlib, polls the state file)
  cli.py       kalshi-bot command line
docs/
  research-brief.md  revised, pre-registered research plan (read this)
  HANDOFF.md         this file
tests/         pytest, all HTTP mocked, no network needed
```

CLI (`kalshi-bot --env prod|demo <command>`):

| command | needs key | what |
| --- | --- | --- |
| check | no | validate .env and load the key, no network |
| status | yes | exchange status, balance, positions, resting orders |
| markets --series X [--raw] | no | list markets; --raw prints API JSON |
| orderbook TICKER [--raw] | no | resting book |
| candles TICKER | no | OHLC candles |
| record | no | the recorder (defaults to production data) |
| record-stats / record-dump | no | what has been captured; latest raw rows |
| spot-ws --seconds N | no | test the WebSocket feed alone |
| analyze | no | research report over the database |
| whale [--threshold 1000] | no | whale-follow test |
| fairvalue [--min-ttc 120] [--show-trades N] | no | fair-value test |
| demo-trade [--contracts N --max-price 0.60 --loss-cap 5 --profit-target 10 --max-trades N] | demo key unless dry run | alternating up/down paper trader; --status, --reset |
| live-trade --dollars 2 --real-money [--loss-cap 40] | prod key, dry run off | same loop on production with REAL MONEY; typed confirmation |
| demo-ui [--port 8765] [--state-file state/live_loop.json] | no | dashboard at http://127.0.0.1:8765 with a Stop button |
| cancel-all | yes | cancel resting orders (honours dry run) |

## 3. Safety model (do not weaken)

Three independent gates: `KALSHI_ENV` defaults to demo; `KALSHI_DRY_RUN`
defaults to true and returns the would-be order instead of sending it; and
`KalshiClient(allow_live=True)` is required before any order or cancel on
production. Exactly one CLI command sets `allow_live`: `live-trade`, added
2026-09-04 at Cameron's explicit request, and only after `--env prod`,
`KALSHI_DRY_RUN=false`, `--real-money`, a per-trade size of at most $20, a
loss cap of at most $50, a balance check, and a typed `TRADE` confirmation.
No other command can reach production orders. Private keys live outside the
repo; `.gitignore` covers `*.pem`, `.env`, and `state/`.

## 4. Facts learned about Kalshi (verified against production)

- Base URLs: prod `https://api.elections.kalshi.com/trade-api/v2`, demo
  `https://demo-api.kalshi.co/trade-api/v2`. Demo lists almost no 15-minute
  markets, so all recording uses prod public endpoints. Demo and prod API
  keys are separate.
- Auth headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (ms),
  `KALSHI-ACCESS-SIGNATURE` = base64(RSA-PSS-SHA256(ts + METHOD + full path
  without query)). Path includes `/trade-api/v2`.
- Series: `KXBTC15M`, `KXDOGE15M`. Ticker example
  `KXBTC15M-26SEP041215-15` (close time in Eastern). One market per series
  is open at a time, for exactly its 15-minute window (`open_time` to
  `close_time`).
- Settlement: `floor_strike` is the average of the CF Benchmarks real-time
  index over the last minute before the window opened. YES if the same
  average over the last minute before close is >= strike
  (`strike_type: greater_or_equal`). `expiration_value` carries the
  settlement index value once settled (captured by the recorder).
- Prices arrive as fixed-point strings (`yes_bid_dollars`, etc.); counts as
  `*_fp` strings and are fractional. Price structure is `tapered_deci_cent`:
  0.001 steps below $0.10 and above $0.90, 0.01 between. Legacy integer-cent
  fields are also accepted by the parsers.
- Orderbook response: `{"orderbook_fp": {"yes_dollars": [[price, count]...],
  "no_dollars": [...]}}`. Lists are bids on each side; YES ask = 1 - best NO bid.
- Trades: `/markets/trades?ticker=&min_ts=&limit=&cursor=`; fields
  `trade_id`, `created_time` (microseconds), `yes_price_dollars`,
  `no_price_dollars`, `count_fp`, `taker_side`, `taker_book_side`. BTC prints
  ~20/s in busy minutes; DOGE about a tenth. Capture follows the cursor.
- Books are deep and tight mid-window: ~50k contracts within ten levels per
  side, 1-cent spread on BTC.
- Rate limits: token bucket, basic tier reportedly 200 reads/s; 429 has no
  Retry-After header. Client spaces requests 0.15 s apart and backs off.
- Orders (verified against production 2026-09-04): the legacy
  `POST /portfolio/orders` now answers 410 `deprecated_v1_order_endpoint`.
  The V2 endpoint is `POST /portfolio/events/orders` with a single YES-book
  shape: `ticker`, `client_order_id`, `side` = `bid` | `ask`, `count` as a
  fixed-point string ("3"), `price` as a dollar string ("0.5300", the YES
  price), `time_in_force` (`good_till_canceled` | `immediate_or_cancel` |
  `fill_or_kill`, required), `self_trade_prevention_type`
  (`taker_at_cross` | `maker`, required), optional `expiration_time`
  (unix seconds), `post_only`, `reduce_only`. Mapping: buy YES at p = bid
  at p; buy NO at q = ask at 1 - q; sell YES at p = ask at p; sell NO at q
  = bid at 1 - q (`client.book_side_and_price`). Response: `order_id`,
  `client_order_id`, `fill_count`, `remaining_count`, `average_fill_price`,
  `average_fee_paid`, `ts_ms`. Cancel is `DELETE
  /portfolio/events/orders/{id}?market_ticker=...`, returning `reduced_by`.
  `GET /portfolio/orders` and `GET /portfolio/fills` still work; fills and
  orders now carry `outcome_side`, `book_side`, `count_fp`, `*_price_dollars`
  and, on fills, `fee_cost`. Source: Kalshi's `kalshi-typescript` SDK 3.29.0
  on npm (docs.kalshi.com is blocked from the sandbox; the SDK tarball is
  the way to read the current spec).
- Exchange shards (verified 2026-09-04): Kalshi runs several exchange
  instances, each with its own balance. `GET /exchange/status` lists them in
  `exchange_index_statuses`: 0 Default, 1 Combos, 2 Crypto, 3 Tennis &
  Baseball. `GET /portfolio/balance` gives the total plus `balance_breakdown`
  per shard; each market carries `exchange_index` (the 15-minute crypto
  markets are on shard 2). An order draws only on its market's shard, so a
  funded account gets `insufficient_balance` until money is moved with
  `POST /portfolio/intra_exchange_instance_transfer` (`source` and
  `destination` both `event_contract`, `amount` in centicents,
  `source_exchange_shard`, `destination_exchange_shard`). The transfer
  lookup endpoint answered 404 for a transfer that went through; poll the
  destination balance instead. `status` prints the breakdown, `transfer`
  moves funds, and `live-trade` funds the markets' shard before starting
  (`client.transfer_between_shards`, `cli.shard_plan`).

## 5. Database (state/market_data.sqlite, schema v3)

All timestamps unix seconds; prices dollars; counts contracts.

- `markets`: ticker, series_ticker, event_ticker, title, strike, strike_type,
  expiration_value, open_ts, close_ts, expiration_ts, status, result,
  first_seen_ts, last_seen_ts, settled_ts, raw (JSON).
- `snapshots` (every 5 s per open market): ts, ticker, secs_to_close,
  yes_bid, yes_ask, no_bid, no_ask, yes_bid_size, yes_ask_size, last_price,
  volume, open_interest, yes_depth, no_depth, yes_levels, no_levels (JSON),
  book_raw (JSON).
- `trades`: trade_id, ticker, ts (exchange time), yes_price, no_price, count,
  taker_side, raw (JSON, includes taker_book_side).
- `spot`: ts (local), source (`coinbase` = 5 s REST, `coinbase_ws` =
  sub-second WebSocket, rate-limited to 5 rows/s/symbol), symbol, price,
  exchange_ts.
- `meta`: schema_version. Older files migrate in place on open.

## 6. Research plan (docs/research-brief.md, pre-registered)

Primary tests, both on pooled BTC + DOGE with a series term:

1. Whale-follow: does a large sweep (notional >= $1,000; ladder at $250 and
   $5,000) predict settlement beyond the market-implied probability at the
   print? Statistic: excess = win rate - implied. CIs bootstrapped over
   markets. Copy P&L as taker (next ask, exact fee) and maker (rest one tick
   inside, filled only if traded through). Spot-conditioned null. Gate: 200
   whale sweeps, time-ordered 70/30 split. Viable only if held-out taker net
   >= +1 cent/contract with a 95% CI excluding zero.
2. Fair value (coded, `kalshi_bot/fairvalue.py`): p_up = Phi(ln(S/K) /
   (sigma * sqrt(tau))) with sigma the RMS of 5-second Coinbase log returns
   over 30 or 60 minutes and tau the variance-equivalent horizon of the
   one-minute settlement average ((t - 60) + 20 outside the last minute, t/3
   inside). Rule: one entry per market at the first snapshot where a side's
   fair value beats its ask by fee plus margin (ladder 0 to 5 cents), no
   entry under 120 s, fill at the next snapshot's ask, hold to settlement;
   maker variant as for whales. Vol window and margin are fitted on the first
   70% of markets by the lower CI bound of taker net; verdict on the last 30%
   with the whale gate (200 trades, 60 held out) and threshold (+1 cent, CI
   excluding zero). Clusters are 15-minute windows (BTC and DOGE closing
   together share one move). Diagnostics: annualised realised vol, Brier of
   model vs market mid by horizon, realised outcome by model-minus-market
   gap bucket, and the Coinbase-vs-settlement basis from `expiration_value`.
   Synthetic-world check: with a book that ignores spot the test says VIABLE;
   with a book equal to true fair value it says INCONCLUSIVE with negative
   training net. Sample-size reality from the same worlds: an edge of about
   5 cents/contract needs roughly 200+ held-out trades before the CI clears
   zero, so expect INCONCLUSIVE for the first days even if an edge exists.
   `analyze` still reports the spot-vs-strike signal, calibration, Brier
   scores, lead-lag, and the fee-inclusive backtest grid as groundwork.

Dropped: perp funding/basis (no information on a 15-minute horizon), news or
search tools, random-entry nulls, unclustered confidence intervals.

Section 6 of Cameron's brief (self-improvement loop) was reviewed and
accepted in principle: no online learning, scheduled retraining, promotion
gate with a paper period, size the only live-adjustable parameter and only
downward, auto-revert on drift, simple residual model, Claude as adversarial
reviewer. Agreed adjustments: build only the feature store now (it is the
decision log from the risk section); evaluate candidates paired on the same
windows; monthly cadence at first; drop funding features. Sample-size
reality: distinguishing a 2-cent/contract difference needs ~9,800 windows
per arm unpaired (about 50 days at one trade per window on both series);
1 cent needs ~39,000. Not yet written into the brief.

## 7. Risk framework design (agreed, not built)

`RiskEngine.check(order, state)` between strategy and client, state in
SQLite so restarts cannot reset limits. Hard cutoffs: daily loss cap with
manual reset; max contracts per market and one position per window; max
fraction of bankroll at risk; consecutive-loss breaker; no entry under 120 s
to close; stale-data guard (no orders if spot or book older than 10 s,
cancel resting if older than 30 s); kill switch via file flag and Telegram
command; decision log with full inputs per decision (doubles as the feature
store); reconciliation against exchange positions on start and periodically;
every resting order expires before the no-entry window; fixed-fraction
sizing, never full Kelly. Ops: Linux VPS under systemd or Docker, Telegram
alerts with a dead-man heartbeat, paper mode default, minimal dashboard.

## 8. How to work with this project

- The Claude Code remote sandbox cannot reach Kalshi or Coinbase (egress
  proxy returns 403). Build and unit-test there; Cameron runs live commands
  on his laptop and pastes output. `record-dump` and `--raw` flags exist
  for exactly this.
- After pulling, Cameron restarts the recorder (Ctrl-C, `kalshi-bot record`).
  The database migrates automatically.
- `deploy/` holds a Dockerfile, a docker-compose file (services: `live`,
  `dashboard`, `recorder`) and a step-by-step guide for running everything
  on a $5 Linux server so the laptop can be off. Written 2026-09-04 without
  a Docker daemon in the sandbox, so the image build is unverified; the
  first server run may need a small fix. The `live` service passes `--yes`,
  so it starts trading on boot without the typed confirmation.
- Windows notes: activate the venv in every new window; execution policy may
  need `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
- Be frank about edge: these markets are near coin flips with a fee of
  about 1.75 cents per contract at 50 cents. Do not lower the pre-registered
  bar to avoid a "no edge" verdict.

## 9. Immediate next steps

1. Cameron: `git pull`, `pip install -e ".[dev]"`, restart the recorder.
2. Wait for roughly a day of data, then run `kalshi-bot record-stats`,
   `kalshi-bot analyze`, `kalshi-bot whale`, and `kalshi-bot fairvalue`;
   paste the output into the chat and interpret against the brief's
   thresholds. Look first at the `fairvalue` basis table (how often the
   Coinbase minute-average disagreed with the result: that is the floor on
   model error near the strike) and at the gap-signal table (whether
   model-minus-market predicts anything at all). Expect INCONCLUSIVE from
   both verdicts for the first days; that is the gate working.
3. If the first real report shows the vol estimator misbehaving on live data
   (annualised vol far from 30 to 80%, or low model coverage), fix the
   estimator before reading anything else. Possible causes: WebSocket ticks
   with sub-5-second gaps are fine, but a stalled feed forward-fills zeros
   into the returns.
4. Phase 4 has started early at Cameron's request (2026-09-05): the
   strategy interface and the decision log / feature store exist
   (`kalshi_bot/strategy.py`, `state/decisions.jsonl`), and the fair-value
   model can run live with `--strategy fairvalue`. Still to build from the
   risk section: persisted daily loss cap with manual reset (the loop's cap
   is per run-state), consecutive-loss breaker, reconciliation against
   exchange positions, Telegram kill switch and heartbeat. The live
   fair-value run has no verdict behind it yet; the pre-registered gate is
   still `kalshi-bot fairvalue` on recorded data. Use the decision log to
   compare the live model's p_yes with settlements as a second check.
5. Fold the section 6 review into docs/research-brief.md now that phase 4
   has started.
6. Cameron's current live settings (2026-09-05): $5 per trade, $50 loss
   cap, no profit cap, balance about $140 after a $100 deposit.
7. The self-improvement loop exists (`kalshi_bot/learn.py`, `kalshi-bot
   learn [--every 3600]`, research brief section 6a) and exits exist
   (`strategy.exit`, `DemoLoop._maybe_exit`, `--exit-margin`,
   `--take-profit`, `--stop-loss`, `--no-exits`). The strategy reloads
   `state/params.json` within a minute of a change. Cameron should run
   `learn --every 3600` in a third window (or the compose `learn`
   service). Expect "candidate failed the promotion gate" for days; that is
   the gate. Sells use `time_in_force=immediate_or_cancel` on the V2
   endpoint, untested against a real fill as of this note.
8. Re-entry (Cameron's ask, 2026-09-05): after a sale the same market may
   be entered again on a fresh signal, up to `--max-entries` (6) per
   market; beyond `--free-entries` (2) the market must be in profit so far
   (`SeriesState.entries_allowed`). Still one open position per series.
9. Robustness round (Cameron's "do all", 2026-09-05, with "no telegram
   alerts just professional ui"):
   * Fixed-fraction sizing: `learn.kelly_fraction` publishes
     `risk_fraction` in params.json only on promotion; `DemoLoop.trade_dollars`
     spends that fraction of the shard balance, capped by `--max-dollars`
     (20) and the live ceiling; `learn.drawdown_check` halves size on a
     drawdown of half the cap.
   * Maker entries (`--entry maker`, default): `maker_price` rests one tick
     inside the spread on the right grid; `_maker_to_taker` re-sends at the
     ask after `--maker-wait` (20 s). Untested against a real fill.
   * Reconciliation (`DemoLoop._reconcile`, `--reconcile` 120 s): compares
     filled open trades with `get_positions(settlement_status="unsettled")`
     on our series; warns once, halts if the mismatch repeats; ignores
     tickers already booked (Kalshi settles a few minutes after close) and
     dry-run fills.
   * Event feed and controls: `kalshi_bot/alerts.py` (`AlertLog`, `tail`)
     writes `state/alerts.jsonl` from the loop (start, fills, sales,
     settlements, halts, pause/resume, reconciliation) and the learner
     (promotions, size changes, drift, drawdown). The dashboard shows the
     tail, a stale-heartbeat banner after 90 s of silence, and Pause /
     Resume (`state/PAUSE`: keep ticking, open nothing new) beside Stop.
   * Spot source (`--spot-source auto|db|rest`): `strategy.DbSpotFeed`
     reads the newest recorder tick per symbol, uses it when under 5 s
     old, else falls back to Coinbase REST.
   * Deployment: `deploy/` has a Dockerfile and compose file (live,
     dashboard, recorder, learn) with a server guide; the image build is
     unverified (no Docker in the sandbox).
   Not built: persisted daily loss cap with manual reset, consecutive-loss
   breaker. The loss cap is cumulative realised loss per run-state, which
   is what Cameron asked for.

Demo trading loop (added 2026-09-04 evening at Cameron's request, separate
from the research plan): `kalshi_bot/demo_loop.py` alternates YES/NO across
successive 15-minute markets on the demo exchange, one entry per market,
fill at the ask, hold to settlement. Bounds: max price per contract, loss
cap, profit target, max trades, no-entry window. Refuses production; dry run
simulates fills so it runs without a key. State in `state/demo_loop.json`
(P&L, counts, last side, open trade, heartbeat, run config); stop via
Ctrl-C, `state/STOP`, or the dashboard button. `kalshi_bot/demo_ui.py`
serves the dashboard on localhost. It is plumbing practice with no edge
claim; it does not touch the recorder, the research modules, or the safety
gates. Known limits: the demo exchange lists few 15-minute crypto markets
(the loop waits when none is open); the order body has not yet been
verified against a real demo fill, so the first run with
`KALSHI_DRY_RUN=false` should be watched and the `create_order` field names
fixed if the API rejects them; P&L is computed locally from the fill price
and the fee model rather than read back from the exchange.

Live trading decision (2026-09-04, later): Cameron asked, after hearing the
concern, to run the alternating loop with real money from his Kalshi
balance: BTC and DOGE 15-minute markets, about $2 per trade, loss cap $40.
Claude stated the expected loss (fee drag of roughly 2 cents per contract,
so on the order of $5 to $10 a day at that cadence) and that the order
format was untested. `live-trade` was built with the gates listed in
section 3. It runs the same loop (`DemoLoop(..., allow_production=True)`,
`self.live` marks orders "REAL MONEY" in the log). Loss cap for live
defaults to $40; the command refuses more than $50 or more than $20 per
trade. State file `state/live_loop.json`; the dashboard needs
`--state-file state/live_loop.json`. If the first real order is rejected
by the API, the fix is in `KalshiClient.create_order`'s body fields; ask
Cameron to paste the error.
