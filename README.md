# kalshi-bot

Automated trading for Kalshi's 15-minute crypto markets (`KXBTC15M`, `KXDOGE15M`).

Phase 1 (this commit): configuration, request signing, a throttled and retrying
HTTP client, typed models, and a CLI for looking at markets and your account.
No strategy yet, and `dry_run` is on by default so nothing can trade.

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
  cli.py      kalshi-bot command line
tests/        pytest suite; HTTP is mocked, no network needed
```

Prices are integer cents (1-99), sizes are contract counts. The client only
uses the cent fields, ignoring the newer `*_dollars` strings the API also returns.

## Development

```bash
pytest
ruff check . && ruff format .
```

## Roadmap

- Phase 2: risk engine (per-order, per-market and daily-loss limits, kill
  switch), SQLite state, execution layer.
- Phase 3: strategy interface, market-data recorder for BTC/DOGE 15-minute
  markets, research notebook, first strategy.
- Phase 4: run on demo with real (paper) orders.
- Phase 5: production, small limits.
