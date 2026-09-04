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
  spot.py     Coinbase public spot price feed
  cli.py      kalshi-bot command line
tests/        pytest suite; HTTP is mocked, no network needed
```

Prices are integer cents (1-99), sizes are contract counts. The client only
uses the cent fields, ignoring the newer `*_dollars` strings the API also returns.

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
snapshot with the resting levels, any new public trades, and (unless
`--no-spot`) the Coinbase spot price for BTC-USD and DOGE-USD. Markets that
have closed are re-fetched about once a minute until their settlement result is
known. No credentials are needed; every endpoint used is public.

Leave it running for a few days (a `tmux` or `screen` session is enough). A
single failing request is logged and skipped, and the loop backs off when the
exchange is unreachable. Ctrl-C stops it cleanly.

Tables: `markets`, `snapshots`, `trades`, `spot`. Timestamps are unix seconds,
prices are cents. Open it with any SQLite tool or pandas:

```python
import pandas as pd, sqlite3
con = sqlite3.connect("state/market_data.sqlite")
snaps = pd.read_sql("select * from snapshots", con, parse_dates={"ts": "s"})
```

## Development

```bash
pytest
ruff check . && ruff format .
```

## Roadmap

- Phase 2 (done): market-data recorder for BTC/DOGE 15-minute markets.
- Phase 3: research on the recorded data; decide whether an edge exists.
- Phase 4: risk engine (per-order, per-market and daily-loss limits, kill
  switch), execution layer, strategy interface.
- Phase 5: run on demo with real (paper) orders.
- Phase 6: production, small limits.
