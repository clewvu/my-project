"""Backfill historical data to grow the sample the model is validated on.

Two sources, both free:

* **Coinbase spot** (BTC/ETH/SOL/DOGE) 1-minute candles -> the ``spot`` table
  (source ``coinbase_hist``). This is the real limiter: the fair-value model needs
  spot at each moment, and the live recorder only has it going forward.
* **Settled Kalshi markets** (strike, result, close) over the same window -> the
  ``markets`` table, extending the set of settled outcomes backward.

Order-book history (bid/ask snapshots) is NOT available from Kalshi, so the exact
fill-based backtest still needs live-recorded snapshots; what this enables is
calibration and out-of-sample checks of the model's predictive power over a much
longer window.

    python -m kalshi_bot.backfill --days 14
"""

from __future__ import annotations

import argparse
import time

import httpx

from .config import Settings
from .client import KalshiClient
from .recorder import DEFAULT_SPOT_SYMBOLS
from .storage import MarketDataStore

CB_CANDLES = "https://api.exchange.coinbase.com/products/{product}/candles"
CRYPTO_SERIES = ["KXBTC15M", "KXDOGE15M", "KXETH15M", "KXSOL15M"]
MAX_CANDLES = 300  # Coinbase returns at most 300 candles per request


def backfill_spot(store: MarketDataStore, symbol: str, start: float, end: float,
                  http: httpx.Client) -> int:
    """Insert 1-minute close prices as spot rows over [start, end]."""
    inserted = 0
    step = MAX_CANDLES * 60
    t = start
    while t < end:
        w_end = min(t + step, end)
        try:
            r = http.get(CB_CANDLES.format(product=symbol),
                         params={"granularity": 60, "start": int(t), "end": int(w_end)})
            r.raise_for_status()
            rows = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            print(f"  {symbol}: candle fetch failed at {int(t)}: {exc}")
            t = w_end
            continue
        # each candle: [time, low, high, open, close, volume]
        batch = [(float(c[0]), "coinbase_hist", symbol, float(c[4]), None)
                 for c in rows if c and c[4] is not None]
        if batch:
            store.insert_spots(batch)
            inserted += len(batch)
        t = w_end
        time.sleep(0.2)  # be gentle with the public endpoint
    return inserted


def backfill_markets(store: MarketDataStore, client: KalshiClient, series: list[str],
                     min_close: float, max_close: float) -> tuple[int, int]:
    """Upsert settled markets whose close falls in the window. Returns (markets, settled)."""
    total = settled = 0
    now = time.time()
    for s in series:
        try:
            mkts = client.get_markets(series_ticker=s, min_close_ts=int(min_close),
                                      max_close_ts=int(max_close), max_pages=60)
        except Exception as exc:  # noqa: BLE001
            print(f"  {s}: market list failed: {exc}")
            continue
        for m in mkts:
            store.upsert_market(m, now)
            total += 1
            if m.result in ("yes", "no"):
                store.mark_settled(m, now)
                settled += 1
        print(f"  {s}: {len(mkts)} markets")
    return total, settled


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill historical spot and settled markets.")
    ap.add_argument("--db", default="state/market_data.sqlite")
    ap.add_argument("--days", type=float, default=14.0)
    ap.add_argument("--now", type=float, default=None, help="override 'now' (unix ts) for tests")
    ap.add_argument("--skip-spot", action="store_true")
    ap.add_argument("--skip-markets", action="store_true")
    args = ap.parse_args()

    now = args.now if args.now is not None else time.time()
    start = now - args.days * 86400
    store = MarketDataStore(args.db)
    print(f"backfilling {args.days:.0f} days into {args.db}")

    if not args.skip_spot:
        print("spot (Coinbase 1-min candles):")
        with httpx.Client(timeout=30.0) as http:
            for sym in DEFAULT_SPOT_SYMBOLS:
                n = backfill_spot(store, sym, start, now, http)
                print(f"  {sym}: +{n} rows")

    if not args.skip_markets:
        print("settled markets (Kalshi):")
        settings = Settings.from_env()
        client = KalshiClient.from_settings(settings)
        total, settled = backfill_markets(store, client, CRYPTO_SERIES, start, now)
        print(f"  {total} markets touched, {settled} settled")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
