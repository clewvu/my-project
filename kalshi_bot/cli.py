"""Command line entry point: ``kalshi-bot <command>``."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
import time
from datetime import UTC, datetime

from . import __version__
from .client import KalshiClient, KalshiError
from .config import BASE_URLS, Settings
from .recorder import DEFAULT_SERIES, DEFAULT_SPOT_SYMBOLS, Recorder
from .spot import SpotFeed
from .spot_ws import FEEDS, SpotWebSocket
from .storage import MarketDataStore, SchemaMismatch

DEFAULT_DB = "state/market_data.sqlite"


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _client(settings: Settings, *, need_auth: bool) -> KalshiClient:
    if need_auth and not settings.has_credentials:
        sys.exit(
            "KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH must be set for this command "
            "(see .env.example)"
        )
    return KalshiClient.from_settings(settings)


def _px(price: float | None) -> str:
    """Format a dollar price as cents with one decimal, e.g. 17.5c."""
    return "-" if price is None else f"{price * 100:.1f}c"


def _fmt_time(dt: datetime | None) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ") if dt else "-"


# ---------------------------------------------------------------- commands


def cmd_check(settings: Settings, _: argparse.Namespace) -> int:
    """Validate local configuration without touching the network."""
    print(f"kalshi-bot {__version__}")
    print(f"env:        {settings.env}  ({settings.base_url})")
    print(f"dry_run:    {settings.dry_run}")
    print(f"key id:     {settings.api_key_id or '(missing)'}")
    print(f"key path:   {settings.private_key_path or '(missing)'}")
    if settings.private_key_path is not None:
        if not settings.private_key_path.exists():
            print("            -> file not found")
            return 1
        try:
            from .auth import Signer

            Signer.from_pem_path(settings.api_key_id or "x", settings.private_key_path)
            print("            -> RSA private key loads OK")
        except Exception as exc:  # noqa: BLE001
            print(f"            -> cannot load key: {exc}")
            return 1
    print(f"throttle:   {settings.min_request_interval}s between requests")
    return 0


def cmd_status(settings: Settings, _: argparse.Namespace) -> int:
    """Exchange status, balance and open positions."""
    with _client(settings, need_auth=True) as client:
        status = client.exchange_status()
        print(
            f"exchange:   trading_active={status.get('trading_active')} "
            f"exchange_active={status.get('exchange_active')}"
        )
        bal = client.get_balance()
        print(f"balance:    ${bal.balance:,.2f}")
        positions = [p for p in client.get_positions() if p.position != 0]
        print(f"positions:  {len(positions)}")
        for p in positions:
            print(
                f"  {p.ticker:32} {p.side:>3} x{abs(p.position):<7g} cost=${p.total_cost:.2f} "
                f"realized=${p.realized_pnl:.2f}"
            )
        orders = client.get_orders(status="resting")
        print(f"resting orders: {len(orders)}")
        for o in orders:
            print(
                f"  {o.ticker:32} {o.action} {o.side} x{o.remaining_count:g} @ {_px(o.price)}"
                f"  id={o.order_id}"
            )
    return 0


def cmd_markets(settings: Settings, args: argparse.Namespace) -> int:
    """List markets in a series."""
    with _client(settings, need_auth=False) as client:
        markets = client.get_markets(
            series_ticker=args.series, status=args.status or None, limit=args.limit, max_pages=1
        )
    if args.raw:
        for m in markets[: args.limit]:
            print(json.dumps(m.raw, indent=2, default=str))
        return 0
    markets.sort(key=lambda m: m.close_time or datetime.max.replace(tzinfo=UTC))
    print(
        f"{'ticker':30} {'status':8} {'bid':>6} {'ask':>6} {'last':>6} {'volume':>10} "
        f"{'strike':>10}  close (UTC)"
    )
    for m in markets[: args.limit]:
        strike = f"{m.strike:,.2f}" if m.strike is not None else "-"
        print(
            f"{m.ticker:30} {m.status:8} {_px(m.yes_bid):>6} {_px(m.yes_ask):>6} "
            f"{_px(m.last_price):>6} {m.volume:>10,.0f} {strike:>10}  {_fmt_time(m.close_time)}"
        )
    if not markets:
        print("(no markets returned)")
    return 0


def cmd_orderbook(settings: Settings, args: argparse.Namespace) -> int:
    """Show the resting book for one market."""
    with _client(settings, need_auth=False) as client:
        m = client.get_market(args.ticker)
        book = client.get_orderbook(args.ticker, depth=args.depth)
    if args.raw:
        print(
            json.dumps(
                {"market": m.raw, "orderbook": dataclasses.asdict(book)}, indent=2, default=str
            )
        )
        return 0
    print(f"{m.ticker}  {m.title}")
    strike = f"{m.strike:,.2f}" if m.strike is not None else "-"
    print(
        f"status={m.status}  closes {_fmt_time(m.close_time)}  strike={strike}  "
        f"yes bid/ask={_px(book.best_yes_bid)}/{_px(book.best_yes_ask)}  mid={_px(book.yes_mid)}"
    )
    if book.is_empty:
        print("(orderbook parsed empty; raw response follows)")
        print(json.dumps(book.raw, indent=2, default=str))
        return 0
    print(f"{'YES bids':>20}    {'NO bids':>20}")
    for i in range(max(len(book.yes), len(book.no))):
        y = f"{_px(book.yes[i].price):>6} x{book.yes[i].count:<10.2f}" if i < len(book.yes) else ""
        n = f"{_px(book.no[i].price):>6} x{book.no[i].count:<10.2f}" if i < len(book.no) else ""
        print(f"{y:>20}    {n:>20}")
    return 0


def cmd_candles(settings: Settings, args: argparse.Namespace) -> int:
    """Print 1-minute candles for a market (last N minutes)."""
    end = int(time.time())
    start = end - args.minutes * 60
    with _client(settings, need_auth=False) as client:
        candles = client.get_candlesticks(
            args.series, args.ticker, start_ts=start, end_ts=end, period_interval=args.interval
        )
    print(f"{'start (UTC)':20} {'open':>6} {'high':>6} {'low':>6} {'close':>6} {'vol':>8}")
    for c in candles:
        ts = datetime.fromtimestamp(c.start_ts, tz=UTC).strftime("%Y-%m-%d %H:%M")
        print(
            f"{ts:20} {_px(c.open):>6} {_px(c.high):>6} {_px(c.low):>6} "
            f"{_px(c.close):>6} {c.volume:>8,.0f}"
        )
    if not candles:
        print("(no candles returned)")
    return 0


def cmd_cancel_all(settings: Settings, args: argparse.Namespace) -> int:
    """Cancel every resting order (honours dry-run)."""
    with _client(settings, need_auth=True) as client:
        ids = client.cancel_all_orders(ticker=args.ticker)
    verb = "would cancel" if settings.dry_run else "cancelled"
    print(f"{verb} {len(ids)} order(s)")
    return 0


def cmd_record(settings: Settings, args: argparse.Namespace) -> int:
    """Record orderbooks, trades, settlements and spot prices to SQLite (Ctrl-C to stop)."""
    symbols = args.spot_symbol or DEFAULT_SPOT_SYMBOLS
    spot = None if args.no_spot else SpotFeed(symbols)
    with _client(settings, need_auth=False) as client, MarketDataStore(args.db) as store:
        spot_ws = None if args.no_spot_ws else SpotWebSocket(store, symbols, feed=args.spot_feed)
        rec = Recorder(
            client,
            store,
            series=args.series or DEFAULT_SERIES,
            interval=args.interval,
            book_depth=args.depth,
            spot=spot,
            spot_ws=spot_ws,
        )
        try:
            rec.run(max_ticks=args.ticks)
        finally:
            if spot is not None:
                spot.close()
    return 0


def cmd_spot_ws(_: Settings, args: argparse.Namespace) -> int:
    """Run only the WebSocket spot feed for a few seconds and print what arrives."""
    symbols = args.spot_symbol or DEFAULT_SPOT_SYMBOLS
    with MarketDataStore(args.db) as store:
        ws = SpotWebSocket(store, symbols, feed=args.spot_feed)
        ws.start()
        deadline = time.time() + args.seconds
        try:
            while time.time() < deadline:
                time.sleep(1.0)
                parts = []
                for sym in symbols:
                    tick = ws.last_tick(sym)
                    if tick is None:
                        parts.append(f"{sym}=-")
                    else:
                        parts.append(f"{sym}={tick.price:,.4f} ({ws.age(sym):.1f}s old)")
                print(f"msgs={ws.messages:<6} reconnects={ws.reconnects}  {'  '.join(parts)}")
        finally:
            ws.stop()
    if ws.messages == 0:
        print(f"no messages received; last error: {ws.last_error}")
        return 1
    return 0


def cmd_record_stats(_: Settings, args: argparse.Namespace) -> int:
    """Summarise what the recorder has captured so far."""
    with MarketDataStore(args.db) as store:
        st = store.stats()
    span = ""
    if st["first_ts"] and st["last_ts"]:
        first = datetime.fromtimestamp(st["first_ts"], tz=UTC)
        last = datetime.fromtimestamp(st["last_ts"], tz=UTC)
        hours = (st["last_ts"] - st["first_ts"]) / 3600
        span = f"{_fmt_time(first)} -> {_fmt_time(last)} ({hours:.1f}h)"
    print(f"db:         {args.db}")
    print(f"snapshots:  {st['snapshots']:,}  {span}")
    if st["empty_books"]:
        print(f"            {st['empty_books']:,} with an empty/unparsed orderbook")
    print(f"trades:     {st['trades']:,}")
    sources = ", ".join(f"{k}={v:,}" for k, v in sorted(st["spot_by_source"].items()))
    print(f"spot:       {st['spot']:,}  ({sources or 'none'})")
    print(
        f"markets:    {st['markets']:,} seen, {st['settled']:,} settled, "
        f"{st['with_value']:,} with settlement value"
    )
    for row in st["by_series"]:
        settled = row["settled"] or 0
        yes = row["yes_wins"] or 0
        rate = f"{yes / settled:.1%} yes" if settled else "-"
        print(
            f"  {row['series'] or '?':12} markets={row['markets']:<5} settled={settled:<5} {rate}"
        )
    return 0


def cmd_record_dump(_: Settings, args: argparse.Namespace) -> int:
    """Print the latest row of each recorded table, with raw JSON expanded."""
    with MarketDataStore(args.db) as store:
        latest = store.latest_rows()
        counts = store.trade_counts()
    for table, row in latest.items():
        print(f"== {table}")
        if row is None:
            print("  (empty)")
            continue
        raw = row.pop("raw", None)
        for key, value in row.items():
            if key in ("yes_levels", "no_levels", "book_raw") and value:
                value = json.loads(value)
            print(f"  {key:16} {value}")
        if raw:
            print("  raw:")
            for line in json.dumps(json.loads(raw), indent=2, default=str).splitlines():
                print("    " + line)
    print("== trades per ticker")
    for ticker, n in counts:
        print(f"  {ticker:34} {n}")
    return 0


def cmd_analyze(_: Settings, args: argparse.Namespace) -> int:
    """Research report over recorded data: calibration, spot signal, lead-lag, backtest."""
    try:
        from . import analysis
    except ImportError:
        sys.exit('analysis needs pandas: pip install -e ".[research]"')
    try:
        ds = analysis.load(args.db, series=args.series or None)
    except SchemaMismatch as exc:
        sys.exit(f"error: {exc}")
    print(analysis.report(ds))
    return 0


def cmd_whale(_: Settings, args: argparse.Namespace) -> int:
    """Test whether large prints predict settlement beyond the market price."""
    try:
        from . import whale
    except ImportError:
        sys.exit('whale needs pandas: pip install -e ".[research]"')
    data = whale.load(args.db, series=args.series or None)
    print(whale.report(data, threshold=args.threshold))
    return 0


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kalshi-bot", description="Kalshi trading bot")
    p.add_argument("--env-file", default=None, help="path to a .env file (default: ./.env)")
    p.add_argument(
        "--env",
        choices=sorted(BASE_URLS),
        default=None,
        help="override KALSHI_ENV for this command (demo | prod)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help=cmd_check.__doc__).set_defaults(func=cmd_check)
    sub.add_parser("status", help=cmd_status.__doc__).set_defaults(func=cmd_status)

    s = sub.add_parser("markets", help=cmd_markets.__doc__)
    s.add_argument("--series", default="KXBTC15M", help="series ticker, e.g. KXBTC15M")
    s.add_argument("--status", default="open", help="open | closed | settled | (empty for all)")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--raw", action="store_true", help="print raw API JSON")
    s.set_defaults(func=cmd_markets)

    s = sub.add_parser("orderbook", help=cmd_orderbook.__doc__)
    s.add_argument("ticker")
    s.add_argument("--depth", type=int, default=10)
    s.add_argument("--raw", action="store_true", help="print raw API JSON")
    s.set_defaults(func=cmd_orderbook)

    s = sub.add_parser("candles", help=cmd_candles.__doc__)
    s.add_argument("ticker")
    s.add_argument("--series", default="KXBTC15M")
    s.add_argument("--minutes", type=int, default=60)
    s.add_argument("--interval", type=int, default=1, help="candle size in minutes: 1, 60, 1440")
    s.set_defaults(func=cmd_candles)

    s = sub.add_parser("record", help=cmd_record.__doc__)
    s.set_defaults(default_env="prod")  # public data; demo has almost no markets
    s.add_argument(
        "--series",
        action="append",
        help=f"series ticker; repeatable (default: {' '.join(DEFAULT_SERIES)})",
    )
    s.add_argument("--interval", type=float, default=5.0, help="seconds between ticks")
    s.add_argument("--depth", type=int, default=10, help="orderbook levels to store")
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument(
        "--ticks", type=int, default=None, help="stop after N ticks (default: run forever)"
    )
    s.add_argument("--no-spot", action="store_true", help="skip the 5s REST spot poll")
    s.add_argument("--no-spot-ws", action="store_true", help="skip the WebSocket spot feed")
    s.add_argument(
        "--spot-feed",
        choices=sorted(FEEDS),
        default="advanced",
        help="which Coinbase WebSocket to use",
    )
    s.add_argument(
        "--spot-symbol",
        action="append",
        help=f"spot symbol; repeatable (default: {' '.join(DEFAULT_SPOT_SYMBOLS)})",
    )
    s.set_defaults(func=cmd_record)

    s = sub.add_parser("spot-ws", help=cmd_spot_ws.__doc__)
    s.add_argument("--seconds", type=int, default=15)
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--spot-feed", choices=sorted(FEEDS), default="advanced")
    s.add_argument("--spot-symbol", action="append")
    s.set_defaults(func=cmd_spot_ws)

    s = sub.add_parser("record-stats", help=cmd_record_stats.__doc__)
    s.add_argument("--db", default=DEFAULT_DB)
    s.set_defaults(func=cmd_record_stats)

    s = sub.add_parser("record-dump", help=cmd_record_dump.__doc__)
    s.add_argument("--db", default=DEFAULT_DB)
    s.set_defaults(func=cmd_record_dump)

    s = sub.add_parser("analyze", help=cmd_analyze.__doc__)
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--series", action="append", help="restrict to a series; repeatable")
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("whale", help=cmd_whale.__doc__)
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--series", action="append", help="restrict to a series; repeatable")
    s.add_argument("--threshold", type=float, default=1000.0, help="whale notional in dollars")
    s.set_defaults(func=cmd_whale)

    s = sub.add_parser("cancel-all", help=cmd_cancel_all.__doc__)
    s.add_argument("--ticker", default=None)
    s.set_defaults(func=cmd_cancel_all)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env(args.env_file)
    except ValueError as exc:
        sys.exit(f"config error: {exc}")
    env_override = args.env or getattr(args, "default_env", None)
    if env_override and env_override != settings.env:
        settings = dataclasses.replace(settings, env=env_override)
    _setup_logging(settings.log_level)
    if args.command not in (
        "check",
        "markets",
        "record-stats",
        "record-dump",
        "analyze",
        "spot-ws",
        "whale",
    ):
        logging.getLogger(__name__).info("env=%s dry_run=%s", settings.env, settings.dry_run)
    try:
        return args.func(settings, args)
    except (KalshiError, SchemaMismatch) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
