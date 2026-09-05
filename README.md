# kalshi-bot

Automated trading for Kalshi's 15-minute crypto markets (`KXBTC15M`, `KXDOGE15M`).

So far: configuration, request signing, a throttled and retrying HTTP client,
typed models, a CLI for looking at markets and your account, and a market-data
recorder. No strategy yet, and `dry_run` is on by default so nothing can trade.

## Setup

1. Python 3.11+.
2. Create a virtualenv and install:

   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -e ".[dev]"
   ```

3. Create an API key on Kalshi. Do this on the **demo** site first
   (`demo.kalshi.co`, paper money). Account -> API keys -> create. Save the
   key id and download the private key `.pem`. Demo and production keys are
   separate; you will repeat this for production later.
4. Store the `.pem` outside the repo, for example `~/.kalshi/demo.pem`.
5. `cp .env.example .env` and fill in `KALSHI_API_KEY_ID` and
   `KALSHI_PRIVATE_KEY_PATH`. Leave `KALSHI_ENV=demo` and `KALSHI_DRY_RUN=true`.
6. Verify locally, then against the exchange:

   ```bash
   kalshi-bot check      # loads config and the key, no network
   kalshi-bot status     # exchange status, balance, positions, resting orders
   kalshi-bot markets --series KXBTC15M
   kalshi-bot orderbook KXBTC15M-26SEP041500-...   # a ticker from the list above
   kalshi-bot candles  KXBTC15M-26SEP041500-... --minutes 30
   ```

## Safety model

Three independent gates stand between the code and your money:

| Gate | Default | What it does |
| --- | --- | --- |
| `KALSHI_ENV` | `demo` | Selects the paper-trading host. Production is opt-in. |
| `KALSHI_DRY_RUN` | `true` | Orders are logged and returned, never sent. |
| `allow_live` | `False` | Even with dry-run off, the client refuses to place or cancel orders on production unless constructed with `allow_live=True`. Only one CLI command sets it: `live-trade`, and only after `--env prod`, `--real-money`, and a typed `TRADE` confirmation. |

Reads (markets, orderbook, candles) are always allowed and are unsigned public
endpoints. Account reads are signed but harmless.

## Layout

```
kalshi_bot/
  config.py   Settings from environment / .env
  auth.py     RSA-PSS signing (KALSHI-ACCESS-* headers)
  client.py   KalshiClient: throttle, retry/backoff, dry-run, live guard
  models.py   Market, Orderbook, Balance, Position, Order, Fill, Candle
  storage.py  SQLite store for recorded market data
  recorder.py polling loop: books, trades, settlements, spot
  spot.py     Coinbase REST spot price poll
  spot_ws.py  Coinbase WebSocket spot feed (background thread)
  fees.py     Kalshi fee model
  analysis.py research report over the recorded data (needs pandas)
  whale.py    whale-follow hypothesis test with clustered bootstrap (needs pandas)
  fairvalue.py realised-vol fair-value model and backtest (needs pandas)
  demo_loop.py demo-only alternating trader with loss cap and profit target
  demo_ui.py   local dashboard for the demo loop (stdlib http.server)
  cli.py      kalshi-bot command line
tests/        pytest suite; HTTP is mocked, no network needed
```

Prices are **dollars per contract** as floats on a 0.001 grid (Kalshi's
15-minute markets use tenth-of-a-cent ticks below $0.10 and above $0.90).
Contract counts are floats because the API reports fractional counts. The
parsers read the API's fixed-point string fields (`*_dollars`, `*_fp`) first
and fall back to the legacy integer-cent fields.

Each 15-minute market carries its reference price in `floor_strike`: the
average of the CF Benchmarks real-time index over the last minute before the
window opened. It settles YES if the same average over the last minute before
close is at least that value.

## Recording market data (phase 2)

Before writing a strategy we need data. The recorder polls the 15-minute BTC
and DOGE series and writes everything to SQLite:

```bash
kalshi-bot record                      # KXBTC15M + KXDOGE15M, every 5s, to state/market_data.sqlite
kalshi-bot record --series KXBTC15M --interval 3 --db ~/kalshi-data/btc.sqlite
kalshi-bot record-stats                # what has been captured so far
```

The recorder reads from the **production** exchange's public endpoints by
default, whatever `KALSHI_ENV` says, because the demo exchange lists almost no
15-minute markets. Reads cannot trade. Any other command can be pointed at
production for reading with `--env prod`, for example
`kalshi-bot --env prod markets --series KXBTC15M`.

Per tick it stores, for every open market in each series: a top-of-book
snapshot with the resting levels, any new public trades (cursor-paginated so
bursts are not truncated), and (unless `--no-spot`) the Coinbase spot price
for BTC-USD and DOGE-USD. Markets that have closed are re-fetched about once a
minute until their settlement result and settlement index value
(`expiration_value`) are known. No credentials are needed; every endpoint used
is public.

Alongside the 5-second poll, a WebSocket client subscribes to Coinbase's
public ticker channel and writes sub-second price changes to the same `spot`
table with source `coinbase_ws` (rate-limited to five rows per second per
symbol, with the exchange timestamp kept for latency measurement). It
reconnects with backoff and treats 30 seconds of silence as a dead
connection. Disable it with `--no-spot-ws`, or switch feeds with
`--spot-feed exchange` if the default Advanced Trade feed misbehaves. Test the
feed on its own with `kalshi-bot spot-ws --seconds 15`.

The database schema is versioned; older files are migrated in place on open.

Leave it running for a few days (a `tmux` or `screen` session is enough). A
single failing request is logged and skipped, and the loop backs off when the
exchange is unreachable. Ctrl-C stops it cleanly.

Tables: `markets`, `snapshots`, `trades`, `spot`. Timestamps are unix seconds,
prices are dollars, counts are contracts. Every snapshot also keeps the raw
orderbook JSON in `book_raw`, so nothing is lost if the parser misses a field.
A database created by an older schema version is refused with a message
telling you to delete it or pass `--db` with a new path.

Open it with any SQLite tool or pandas:

```python
import pandas as pd, sqlite3
con = sqlite3.connect("state/market_data.sqlite")
snaps = pd.read_sql("select * from snapshots", con, parse_dates={"ts": "s"})
```

## Research (phase 3)

Once the recorder has a day or more of settled markets:

```bash
pip install -e ".[research]"     # adds pandas
kalshi-bot analyze               # or --db path --series KXBTC15M
```

The report covers: coverage and base rates; Brier score of the market's
implied probability by horizon against a coin flip and a plain
"spot above strike" rule; calibration tables (implied vs realised YES rate);
accuracy of the spot-vs-strike signal bucketed by distance in basis points;
lead-lag correlation between spot moves and subsequent book moves; and a
fee-inclusive backtest of buying the spot-favoured side at the ask and
holding to settlement, across horizons, price caps, and distance filters.

`kalshi-bot whale` runs the whale-follow test from `docs/research-brief.md`:
prints are aggregated into sweeps, each sweep is scored against the market's
implied probability at that moment, and the report gives the pre-registered
verdict with confidence intervals bootstrapped over markets, copy P&L as taker
and maker after fees, spot conditioning, a time-ordered validation split, and
splits by threshold, time to close, series, and aggressor side. Under 200
whale sweeps it reports "inconclusive" by construction.

`kalshi-bot fairvalue` runs the fair-value test from section 3 of the brief.
Fair value is `Phi(ln(S/K) / (sigma * sqrt(tau)))` with `S` Coinbase spot,
`K` the strike, `sigma` the realised volatility of 5-second spot returns over
the last 30 or 60 minutes, and `tau` the variance-equivalent horizon of the
one-minute settlement average. The backtest buys one contract of the side
whose fair value beats its ask by the taker fee plus a margin, once per
market, never under 120 seconds to close, filled at the next snapshot's ask
and held to settlement. The report gives realised volatility by series, the
Coinbase-vs-settlement-index basis, Brier scores of the model against the
market mid, whether the model-minus-market gap predicts settlement, and the
pre-registered verdict: vol window and margin are fitted on the first 70% of
markets by close time and judged on the last 30% with the same gate and
threshold as the whale test. `--show-trades N` prints the last N trades.

`kalshi_bot/fees.py` holds the fee model (7% x price x (1 - price) per
contract for takers, rounded up to the cent per order). Check it against
Kalshi's current schedule for the series before relying on it.

## Demo trading loop (paper money)

`kalshi-bot demo-trade` is a deliberately simple trader for exercising the
order path on Kalshi's demo exchange: in each successive 15-minute market it
buys one side, alternating YES (up) and NO (down), holds to settlement and
books the result. It is plumbing practice, not a strategy; the research
commands decide whether anything has an edge.

```bash
# .env: KALSHI_ENV=demo, a demo API key, and KALSHI_DRY_RUN=false to send paper orders
kalshi-bot demo-trade                       # defaults: 1 contract, max price 60c,
                                            # loss cap $5, profit target $10
kalshi-bot demo-trade --contracts 2 --max-price 0.55 --loss-cap 3 --profit-target 6
kalshi-bot demo-trade --status              # saved state and last trades
kalshi-bot demo-trade --reset               # clear state and the stop file
kalshi-bot demo-ui                          # dashboard at http://127.0.0.1:8765
```

Bounds: `--max-price` caps what it pays per contract and so the loss per
trade; `--loss-cap` and `--profit-target` stop the loop on cumulative realised
P&L after fees; `--max-trades` stops after N trades; nothing is entered under
`--min-ttc` seconds to close and an unfilled order is cancelled there. State
lives in `state/demo_loop.json`, so a restart cannot reset the caps.

Stop it any time with Ctrl-C, by creating `state/STOP`, or with the Stop
button on the dashboard. A resting order is cancelled; a filled position is
held to settlement and booked on the next run. The dashboard is served from
the standard library on localhost only and polls the state file, so it shows
live status, P&L against both caps, the open position with a countdown, and
the settled trades.

Safety: `demo-trade` refuses to run unless `KALSHI_ENV=demo`, never sets
`allow_live`, and with `KALSHI_DRY_RUN=true` (the default) it simulates fills
at the limit price without sending anything, which needs no API key at all.
Note that the demo exchange lists few 15-minute crypto markets; when none is
open the loop waits and logs it.

### Real money: `live-trade`

The same loop can run on production with real money. It has no edge: each
trade is a coin flip that pays the taker fee (about 1.75 cents per contract
near 50 cents) plus the spread, so its expected result is a slow loss until
a cap stops it. It exists because the owner asked for it, not because the
research supports it.

```bash
# .env: production API key, KALSHI_DRY_RUN=false
kalshi-bot --env prod live-trade --dollars 2 --loss-cap 40 --real-money
kalshi-bot demo-ui --state-file state/live_loop.json
```

Gates, all required: `--env prod` on the command line, `KALSHI_DRY_RUN=false`,
`--real-money`, `--dollars` of at most $20 per trade, a loss cap of at most
$50 (default $40), and typing `TRADE` at the prompt (`--yes` skips the prompt
for unattended runs). The command prints your balance and the expected fee
drag first. State goes to `state/live_loop.json`; the stop file and the
dashboard button work the same way.

### Strategies: `--strategy alternate | fairvalue`

`kalshi_bot/strategy.py` separates *what to buy* from the loop's plumbing.
`alternate` is the coin-flip test above. `fairvalue` runs the research
brief's model live: Coinbase spot every tick, realised volatility over the
last 30 minutes (seeded from the recorder's database at start when it
exists, so there is no warm-up), fair value `Phi(ln(S/K) / (sigma sqrt(tau)))`,
and a trade only when a side's fair value beats its ask by the taker fee
plus `--margin` (default 2 cents). Guards: spot older than 10 seconds, too
little history for volatility, or an ask above `--max-price` all mean no
trade. Most markets are skipped; that is the design.

```bash
kalshi-bot --env prod live-trade --strategy fairvalue --margin 0.03 --dollars 5 --loss-cap 50 --profit-target 0 --real-money
```

Every decision, trade or skip, is appended to `state/decisions.jsonl`
with the strategy's inputs (spot, strike, sigma, seconds to close, both asks,
model probability, edges). Judge the model against what settled by joining
that file with the recorder's `markets` table. Its edge is unproven until
`kalshi-bot fairvalue` on recorded data says VIABLE; until then treat a live
fair-value run as a paid experiment.

### Self-improvement: `kalshi-bot learn`

`kalshi_bot/learn.py` retrains on the recorder's data, promotes new
parameters only past a gate, and lowers size or halts on live drift. It
writes `state/params.json`, which the fair-value strategy reloads within a
minute, and appends every evaluation to `state/learn_history.jsonl`.

```bash
kalshi-bot learn                 # one cycle, prints what it decided and why
kalshi-bot learn --every 3600    # keep going, hourly (a third terminal, or the compose service)
```

Rules, from the research brief section 6a: no online learning; a candidate
(volatility window, margin, probability calibration) is fitted on the first
70% of markets and promoted only if its held-out net is at least +1 cent per
contract with a confidence interval above zero and it beats the incumbent
on the same windows; size is the only live knob and only moves down; a
strategy whose live results fall three standard errors short of its own
probabilities halts until a later cycle passes.

### Exits

The loop can sell before settlement. With `fairvalue`, a position is sold
(immediate-or-cancel at the bid) when the bid exceeds the model's value of
the position by the selling fee plus `--exit-margin` (default 2 cents). With
`alternate`, `--take-profit` and `--stop-loss` set fixed levels in dollars
per contract. `--no-exits` holds everything to settlement.

After a sale the same market can be entered again if the strategy sees a
fresh edge: up to `--max-entries` per market (default 2, at most 6), of
which the first `--free-entries` (default 1) need only a signal and the
rest also require that market to be in profit so far. One position per
series at a time.

### Churn control and the loss breaker

The fair-value exit ignores the entry price on purpose: it sells whenever
the market pays more than the model's value, which is right in expectation
but, near the strike, the model's probability swings several points on a
0.1% move in spot while the book is stickier. Left alone the loop trades
that difference every minute and pays two fees and the spread each time.
Four rules hold it back, all on by default:

* `--min-hold` (60 s): no exit until the position has been held this long.
* `--cooloff` (120 s): after selling out of a market, no re-entry for this long.
* No flips: once a side has been bought in a market, the other side is never
  bought in that market (`--allow-flip` turns this off).
* The exit margin is never below the entry margin (`--exit-margin` is a
  floor; `--margin` 0.03 means a round trip needs the model to move at least
  3 cents plus fees both ways).

`--spot-smooth` (10 s) feeds the model the mean spot over the last ten
seconds instead of the last print, so a single tick cannot trigger a trade.
The raw print is still logged as `spot_last`.

The consecutive-loss breaker holds the line on a bad hour: after
`--max-consecutive-losses` (3) losing results in a row, sales and
settlements alike, the loop opens nothing for `--loss-pause` seconds
(1800, two windows) while still managing what it holds, then resumes and
says so in the dashboard. The `--loss-cap` remains the hard stop.

### Sizing that scales with the bankroll

`--dollars` is the floor. Once the learning loop has promoted parameters it
also publishes a risk fraction (a quarter of the Kelly fraction implied by
the held-out win rate, capped at 5% of the bankroll); the loop then spends
that fraction of the balance on the market's exchange shard per trade, at
most `--max-dollars` (default $20, and never above the live-trade ceiling).
So size grows as profit accumulates and shrinks after losses, and it only
ever exceeds `--dollars` when the gate has passed. `--risk-fraction`
overrides the learner's figure; a drawdown of half the loss cap holds size
at half until the equity recovers.

### Maker entries

By default (`--entry maker`) an entry rests one tick inside the spread
instead of paying the ask: on the 1-cent grid in the middle of the book and
the 0.1-cent grid in the tails. If it has not filled after `--maker-wait`
seconds (default 20) the order is cancelled and re-sent at the ask, so a
signal is never lost. A maker fill saves the spread and, on Kalshi's fee
schedule, usually the taker fee. `--entry taker` pays the ask at once.

### Reconciliation

Every `--reconcile` seconds (default 120, and on the first tick) the loop
compares the positions it believes it holds with the exchange's own
position list for the configured series. A position on the exchange the
loop did not open, a filled trade the exchange does not show, or a quantity
mismatch is raised as a warning; if the same problem is still there at the
next check the loop halts, because its P&L can no longer be trusted, and
says why in the dashboard. Markets the loop has already booked are ignored
while Kalshi settles them. `--reconcile 0` disables the check.

### Dashboard: pause, events, heartbeat

`kalshi-bot demo-ui` shows the running loop (it reads whichever of
`state/live_loop.json` and `state/demo_loop.json` is fresher, or the
`--state-file` you give it) and offers three controls:

* **Pause entries** writes `state/PAUSE`: the loop keeps ticking, manages
  and settles what it holds, but opens nothing new. **Resume** removes it.
* **Stop loop** writes `state/STOP`: resting orders are cancelled and the
  loop exits. **Clear stop file** lets it start again.
* The **Activity** panel is the event feed both loops append to
  (`state/alerts.jsonl`): fills, sales, settlements, halts, cap hits,
  reconciliation warnings, and the learner's promotions, size changes and
  drift halts. A loop that stops ticking for 90 seconds without saying why
  is flagged as having no heartbeat, in the pill and in a banner.

There are no push alerts by design; the page is the alert channel. On a
server, bind it with `--host 0.0.0.0` behind your own access control (see
`deploy/README.md`).

### Spot source

With `--strategy fairvalue` the model's spot price comes, by default
(`--spot-source auto`), from the recorder's database whenever its latest
WebSocket tick is under 5 seconds old, and from Coinbase's REST endpoint
otherwise. That means the live model sees the same sub-second feed the
research uses when the recorder is running alongside it. `--spot-source db`
never falls back to REST (a stale feed then means no trades); `--spot-source
rest` ignores the database.

## Development

```bash
pytest
ruff check . && ruff format .
```

## Roadmap

- Phase 2 (done): market-data recorder for BTC/DOGE 15-minute markets.
- Phase 3 (tooling done): research on the recorded data (`analyze`, `whale`,
  `fairvalue`); decide whether an edge exists.
- Phase 4: risk engine (per-order, per-market and daily-loss limits, kill
  switch), execution layer, strategy interface.
- Phase 5: run on demo with real (paper) orders.
- Phase 6: production, small limits.
