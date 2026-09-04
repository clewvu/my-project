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
| `allow_live` | `False` | Even with dry-run off, the client refuses to place or cancel orders on production unless constructed with `allow_live=True`. The CLI never sets it. |

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

`kalshi_bot/fees.py` holds the fee model (7% x price x (1 - price) per
contract for takers, rounded up to the cent per order). Check it against
Kalshi's current schedule for the series before relying on it.

## Development

```bash
pytest
ruff check . && ruff format .
```

## Roadmap

- Phase 2 (done): market-data recorder for BTC/DOGE 15-minute markets.
- Phase 3 (tooling done): research on the recorded data; decide whether an edge exists.
- Phase 4: risk engine (per-order, per-market and daily-loss limits, kill
  switch), execution layer, strategy interface.
- Phase 5: run on demo with real (paper) orders.
- Phase 6: production, small limits.
