"""Command line entry point: ``kalshi-bot <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, datetime

from . import __version__
from .client import KalshiClient, KalshiError
from .config import Settings


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _client(settings: Settings, *, need_auth: bool) -> KalshiClient:
    if need_auth and not settings.has_credentials:
        sys.exit(
            "KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH must be set for this command "
            "(see .env.example)"
        )
    return KalshiClient.from_settings(settings)


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
        print(f"balance:    ${bal.dollars:,.2f}")
        positions = [p for p in client.get_positions() if p.position != 0]
        print(f"positions:  {len(positions)}")
        for p in positions:
            print(
                f"  {p.ticker:32} {p.side:>3} x{abs(p.position):<5} cost=${p.total_cost / 100:.2f} "
                f"realized=${p.realized_pnl / 100:.2f}"
            )
        orders = client.get_orders(status="resting")
        print(f"resting orders: {len(orders)}")
        for o in orders:
            px = o.yes_price if o.side == "yes" else o.no_price
            print(
                f"  {o.ticker:32} {o.action} {o.side} x{o.remaining_count} @ {px}c  id={o.order_id}"
            )
    return 0


def cmd_markets(settings: Settings, args: argparse.Namespace) -> int:
    """List markets in a series."""
    with _client(settings, need_auth=False) as client:
        markets = client.get_markets(
            series_ticker=args.series, status=args.status, limit=args.limit, max_pages=1
        )
    markets.sort(key=lambda m: m.close_time or datetime.max.replace(tzinfo=UTC))
    print(f"{'ticker':34} {'status':8} {'bid':>4} {'ask':>4} {'last':>4} {'vol':>7}  close (UTC)")
    for m in markets[: args.limit]:
        print(
            f"{m.ticker:34} {m.status:8} {m.yes_bid or '-':>4} {m.yes_ask or '-':>4} "
            f"{m.last_price or '-':>4} {m.volume:>7}  {_fmt_time(m.close_time)}"
        )
    if not markets:
        print("(no markets returned)")
    return 0


def cmd_orderbook(settings: Settings, args: argparse.Namespace) -> int:
    """Show the resting book for one market."""
    with _client(settings, need_auth=False) as client:
        m = client.get_market(args.ticker)
        book = client.get_orderbook(args.ticker, depth=args.depth)
    print(f"{m.ticker}  {m.title}")
    print(
        f"status={m.status}  closes {_fmt_time(m.close_time)}  "
        f"yes bid/ask={book.best_yes_bid}/{book.best_yes_ask}  mid={book.yes_mid}"
    )
    print(f"{'YES bids':>16}    {'NO bids':>16}")
    for i in range(max(len(book.yes), len(book.no))):
        y = f"{book.yes[i].price:>3}c x{book.yes[i].count:<6}" if i < len(book.yes) else " " * 12
        n = f"{book.no[i].price:>3}c x{book.no[i].count:<6}" if i < len(book.no) else ""
        print(f"{y:>16}    {n:>16}")
    return 0


def cmd_candles(settings: Settings, args: argparse.Namespace) -> int:
    """Print 1-minute candles for a market (last N minutes)."""
    end = int(time.time())
    start = end - args.minutes * 60
    with _client(settings, need_auth=False) as client:
        candles = client.get_candlesticks(
            args.series, args.ticker, start_ts=start, end_ts=end, period_interval=args.interval
        )
    print(f"{'start (UTC)':20} {'open':>4} {'high':>4} {'low':>4} {'close':>5} {'vol':>6}")
    for c in candles:
        ts = datetime.fromtimestamp(c.start_ts, tz=UTC).strftime("%Y-%m-%d %H:%M")
        print(
            f"{ts:20} {c.open or '-':>4} {c.high or '-':>4} {c.low or '-':>4} "
            f"{c.close or '-':>5} {c.volume:>6}"
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


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kalshi-bot", description="Kalshi trading bot")
    p.add_argument("--env-file", default=None, help="path to a .env file (default: ./.env)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help=cmd_check.__doc__).set_defaults(func=cmd_check)
    sub.add_parser("status", help=cmd_status.__doc__).set_defaults(func=cmd_status)

    s = sub.add_parser("markets", help=cmd_markets.__doc__)
    s.add_argument("--series", default="KXBTC15M", help="series ticker, e.g. KXBTC15M")
    s.add_argument("--status", default="open", help="open | closed | settled | (empty for all)")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_markets)

    s = sub.add_parser("orderbook", help=cmd_orderbook.__doc__)
    s.add_argument("ticker")
    s.add_argument("--depth", type=int, default=10)
    s.set_defaults(func=cmd_orderbook)

    s = sub.add_parser("candles", help=cmd_candles.__doc__)
    s.add_argument("ticker")
    s.add_argument("--series", default="KXBTC15M")
    s.add_argument("--minutes", type=int, default=60)
    s.add_argument("--interval", type=int, default=1, help="candle size in minutes: 1, 60, 1440")
    s.set_defaults(func=cmd_candles)

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
    _setup_logging(settings.log_level)
    if args.command != "check" and args.command != "markets":
        logging.getLogger(__name__).info("env=%s dry_run=%s", settings.env, settings.dry_run)
    try:
        return args.func(settings, args)
    except KalshiError as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
