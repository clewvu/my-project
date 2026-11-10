"""Command line entry point: ``kalshi-bot <command>``."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from . import __version__
from .client import KalshiClient, KalshiError
from .commodities import ProxyFeed
from .config import BASE_URLS, Settings
from .events import EventBlackout
from .models import Balance
from .pyth import PythClient, PythStream
from .recorder import DEFAULT_SERIES, DEFAULT_SPOT_SYMBOLS, Recorder
from .risk import RiskEngine, RiskLimits
from .spot import SpotFeed
from .spot_ws import FEEDS, SpotWebSocket
from .storage import MarketDataStore, SchemaMismatch
from .strategy import FairValueParams, FairValueStrategy
from .trader import Trader
from .universe import Universe, load_universe

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
    try:
        return KalshiClient.from_settings(settings)
    except KalshiError as exc:
        if need_auth:
            raise
        # public-data commands must not depend on the key being readable
        logging.getLogger(__name__).warning("%s; continuing without credentials", exc)
        return KalshiClient.from_settings(dataclasses.replace(settings, api_key_id=""))


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


def cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    """Exchange status, balance and open positions."""
    with _client(settings, need_auth=True) as client:
        status = client.exchange_status()
        print(
            f"exchange:   trading_active={status.get('trading_active')} "
            f"exchange_active={status.get('exchange_active')}"
        )
        raw_balance = client._request("GET", "/portfolio/balance")
        bal = Balance.from_dict(raw_balance)
        print(f"balance:    ${bal.balance:,.2f}")
        if getattr(args, "raw", False):
            print("raw balance response:")
            print(json.dumps(raw_balance, indent=2, default=str))
            raw_orders = client._request("GET", "/portfolio/orders", params={"limit": 20})
            print("raw orders response (first 20):")
            print(json.dumps(raw_orders, indent=2, default=str)[:3000])
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
    uni = load_universe(args.universe)
    series = args.series or uni.series
    symbols = args.spot_symbol or uni.coinbase_symbols
    spot = None if args.no_spot or not symbols else SpotFeed(symbols)
    with _client(settings, need_auth=False) as client, MarketDataStore(args.db) as store:
        spot_ws = None
        if not args.no_spot_ws and symbols:
            spot_ws = SpotWebSocket(store, symbols, feed=args.spot_feed)
        pyth = None
        if not args.no_pyth and uni.pyth_symbols:
            pyth = PythStream(store, uni.pyth_symbols)
        rec = Recorder(
            client,
            store,
            series=series,
            interval=args.interval,
            book_depth=args.depth,
            spot=spot,
            spot_ws=spot_ws,
            pyth=pyth,
        )
        try:
            rec.run(max_ticks=args.ticks)
        finally:
            if spot is not None:
                spot.close()
    return 0


def cmd_series(settings: Settings, args: argparse.Namespace) -> int:
    """List Kalshi series (optionally by category or substring) to discover tickers."""
    with _client(settings, need_auth=False) as client:
        rows = client.get_series(category=args.category)
    needle = (args.grep or "").upper()
    rows = [
        r
        for r in rows
        if needle in str(r.get("ticker", "")).upper() or needle in str(r.get("title", "")).upper()
    ]
    rows.sort(key=lambda r: str(r.get("ticker", "")))
    print(f"{'ticker':22} {'category':14} {'frequency':12} title")
    for r in rows:
        print(
            f"{str(r.get('ticker', '')):22} {str(r.get('category', '')):14} "
            f"{str(r.get('frequency', '')):12} {r.get('title', '')}"
        )
    print(f"({len(rows)} series)")
    return 0


def cmd_universe_check(settings: Settings, args: argparse.Namespace) -> int:
    """Verify every series in the universe has live markets and show how each settles."""
    uni = load_universe(args.universe)
    with _client(settings, need_auth=False) as client:
        for a in uni.assets:
            flag = "" if a.enabled else " (disabled)"
            try:
                markets = client.get_markets(series_ticker=a.series, status="open", max_pages=1)
            except KalshiError as exc:
                print(f"{a.series:14} {a.name:7}{flag}  ERROR {exc}")
                continue
            live = [m for m in markets if m.status.lower() not in ("closed", "settled")]
            if not live:
                print(f"{a.series:14} {a.name:7}{flag}  no open markets ({len(markets)} returned)")
                continue
            m = sorted(live, key=lambda x: x.close_time or datetime.max.replace(tzinfo=UTC))[0]
            rules = str(m.raw.get("rules_primary", ""))
            source = (
                "CF Benchmarks"
                if "CF Benchmarks" in rules
                else ("Pyth" if "Pyth" in rules else "unknown")
            )
            strike = f"{m.strike:,.4f}" if m.strike is not None else "-"
            print(
                f"{a.series:14} {a.name:7}{flag}  {m.ticker}  closes {_fmt_time(m.close_time)}  "
                f"strike={strike}  yes={_px(m.yes_bid)}/{_px(m.yes_ask)}  settles on {source}"
            )
            if args.rules:
                print(f"    {rules[:300]}")
    return 0


def cmd_pyth_feeds(_: Settings, args: argparse.Namespace) -> int:
    """Search Pyth price feeds, e.g. --query XAU or --query WTI, to find symbols."""
    client = PythClient()
    try:
        feeds = client.search_feeds(args.query, asset_type=args.asset_type)
    finally:
        client.close()
    print(f"{'symbol':28} {'type':12} {'id':66} description")
    for f in feeds:
        print(f"{f.symbol:28} {f.asset_type:12} {f.id:66} {f.description}")
    print(f"({len(feeds)} feeds)")
    return 0


def cmd_pyth_check(_: Settings, args: argparse.Namespace) -> int:
    """Resolve the universe's Pyth symbols and print their latest prices."""
    uni = load_universe(args.universe)
    client = PythClient()
    try:
        feeds = client.resolve(uni.pyth_symbols)
        try:
            ticks = client.latest(feeds) if feeds else []
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                hint = (
                    "PYTH_API_KEY is set but Hermes refused it"
                    if client.api_key
                    else "Hermes price updates need a Pyth API key; set PYTH_API_KEY in .env"
                )
                print(f"error: Pyth Hermes -> HTTP {exc.response.status_code}: {hint}")
                print(
                    "(feed lookup still works; the recorder disables the Pyth stream without a key)"
                )
                return 1
            raise
    finally:
        client.close()
    for sym in uni.pyth_symbols:
        f = feeds.get(sym)
        t = next((t for t in ticks if t.symbol == sym), None)
        if f is None:
            base = sym.split(".")[-1].split("/")[0]
            print(f"{sym:24} UNRESOLVED (try: kalshi-bot pyth-feeds --query {base})")
        elif t is None:
            print(f"{sym:24} id={f.id[:12]}...  no price returned")
        else:
            age = time.time() - (t.exchange_ts or t.local_ts)
            print(f"{sym:24} {t.price:>14,.4f}  published {age:.1f}s ago")
    return 0 if len(feeds) == len(uni.pyth_symbols) else 1


def cmd_crossasset(_: Settings, args: argparse.Namespace) -> int:
    """Cross-asset co-movement, lead-lag, and predictor tests across the universe."""
    try:
        from . import crossasset
    except ImportError:
        sys.exit('crossasset needs pandas: pip install -e ".[research]"')
    data = crossasset.load(args.db, universe=load_universe(args.universe))
    print(crossasset.report(data))
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


LIVE_ACK = "I-ACCEPT-LIVE-TRADING-RISK"


def _params(args: argparse.Namespace) -> FairValueParams:
    return FairValueParams(
        margin=args.margin,
        count=args.count,
        vol_window=args.vol_window,
        max_price=args.max_price,
        min_price=args.min_price,
        exec_mode=args.exec_mode,
        rest_seconds=args.rest_seconds,
        max_spread=args.max_spread,
        kelly_fraction=args.kelly,
        bankroll=args.bankroll,
        max_kelly_count=args.max_contracts,
        dollars=args.dollars,
        favourites_only=args.favourites_only,
        sigma_scale=args.sigma_scale,
    )


def _limits(args: argparse.Namespace) -> RiskLimits:
    return RiskLimits(
        bankroll=args.bankroll,
        daily_loss_cap=args.daily_loss_cap,
        max_contracts_per_order=args.max_contracts,
        max_open_positions=args.max_open,
        max_bankroll_fraction=args.max_fraction,
        consecutive_loss_limit=args.loss_streak,
        no_entry_seconds=args.no_entry,
        max_data_age=args.max_data_age,
        kill_file=args.kill_file,
    )


def cmd_trade(settings: Settings, args: argparse.Namespace) -> int:
    """Run the trading loop. Paper by default; --live needs every gate open."""
    uni = load_universe(args.universe)
    mode = "live" if args.live else "paper"
    if args.live:
        problems = []
        if settings.env != "prod":
            problems.append("KALSHI_ENV must be prod")
        if settings.dry_run:
            problems.append("KALSHI_DRY_RUN must be false")
        if not settings.has_credentials:
            problems.append("KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH must be set")
        if args.acknowledge != LIVE_ACK:
            problems.append(f"pass --acknowledge {LIVE_ACK}")
        if problems:
            sys.exit("refusing live mode:\n  " + "\n  ".join(problems))
    client = KalshiClient.from_settings(settings, allow_live=args.live)
    strategy = FairValueStrategy(_params(args))
    with client, MarketDataStore(args.db) as store:
        spot_ws, pyth, proxy = _price_feeds(store, uni)
        trader = Trader(
            client,
            store,
            uni,
            strategy,
            _limits(args),
            mode=mode,
            interval=args.interval,
            spot_ws=spot_ws,
            pyth=pyth,
            proxy=proxy,
            proxy_margin=args.proxy_margin,
            allow_proxy=args.allow_proxy,
            blackout=EventBlackout.load(args.events),
        )
        if args.live:
            bal = client.get_balance()
            print(
                f"LIVE TRADING on production. Balance ${bal.balance:,.2f}. "
                f"Limits: {trader.risk.limits}"
            )
            print("Starting in 10 seconds; Ctrl-C to abort.")
            time.sleep(10)
        trader.run(max_ticks=args.ticks)
    return 0


def cmd_trade_status(_: Settings, args: argparse.Namespace) -> int:
    """Paper/live P&L, open positions, risk state and recent decisions."""
    mode = "live" if args.live else "paper"
    with MarketDataStore(args.db) as store:
        risk = RiskEngine(store, RiskLimits(), mode)
        summary = store.pnl_summary(mode)
        open_pos = store.positions(mode, "open")
        settled = [p for p in store.positions(mode, "settled")][-args.limit :]
        decisions = store.decisions(mode, args.limit)
    st = risk.state
    print(f"mode:       {mode}")
    print(
        f"risk:       {'HALTED: ' + st.halt_reason if st.halted else 'ok'}  "
        f"day_pnl={st.day_pnl:+.2f}  loss_streak={st.consecutive_losses}"
    )
    n = summary["settled"]
    wr = f"{summary['wins'] / n:.0%}" if n else "-"
    print(
        f"settled:    {n} positions, win rate {wr}, pnl ${summary['pnl']:+.2f} "
        f"(fees ${summary['fees']:.2f}, staked ${summary['staked']:.2f})"
    )
    print(f"decisions:  {summary['decisions']}")
    print(f"open:       {len(open_pos)}")
    for p in open_pos:
        print(
            f"  {p['ticker']:30} {p['side']:3} x{p['count']:<4g} @{_px(p['price'])} "
            f"closes {_fmt_time(datetime.fromtimestamp(p['close_ts'], tz=UTC))}"
        )
    if settled:
        print("recent settled:")
        for p in settled:
            print(
                f"  {p['ticker']:30} {p['side']:3} x{p['count']:<4g} @{_px(p['price'])} "
                f"-> {p['result']:3} {p['pnl']:+.2f}"
            )
    print("recent decisions:")
    for d in decisions[: args.limit]:
        ts = _fmt_time(datetime.fromtimestamp(d["ts"], tz=UTC))
        fair = f"{d['fair_p']:.2f}" if d["fair_p"] is not None else "-"
        print(
            f"  {ts} {d['ticker'] or '-':30} fair={fair} yes={_px(d['yes_ask'])} "
            f"no={_px(d['no_ask'])} {d['action']:8} {d['reason']}"
        )
    return 0


def _dollars(d: dict, name: str) -> float:
    v = d.get(f"{name}_dollars")
    if v not in (None, ""):
        return float(v)
    v = d.get(name)
    return float(v) / 100 if v not in (None, "") else 0.0


def cmd_reconcile(settings: Settings, args: argparse.Namespace) -> int:
    """Exchange-side truth: balance, settlements, recent fills, compared with the local log."""
    with _client(settings, need_auth=True) as client, MarketDataStore(args.db) as store:
        bal = client.get_balance()
        settlements = client.get_settlements()
        fills = client.get_fills(limit=200)
        local = store.pnl_summary("live")
    print(f"exchange balance:      ${bal.balance:,.2f}")
    revenue = sum(_dollars(s, "revenue") for s in settlements)
    cost = sum(_dollars(s, "yes_total_cost") + _dollars(s, "no_total_cost") for s in settlements)
    fees = sum(_dollars(s, "fee") for s in settlements)
    print(
        f"exchange settlements:  {len(settlements)} markets, cost ${cost:,.2f}, "
        f"revenue ${revenue:,.2f}, fees ${fees:,.2f}, net ${revenue - cost - fees:+,.2f}"
    )
    print(
        f"local log (live):      {local['settled']} settled, pnl ${local['pnl']:+,.2f}, "
        f"fees ${local['fees']:,.2f}, staked ${local['staked']:,.2f}"
    )
    print(f"exchange fills seen:   {len(fills)} (most recent {args.limit} below)")
    for f in fills[: args.limit]:
        print(
            f"  {_fmt_time(f.created_time)} {f.ticker:30} {f.action:4} {f.side:3} x{f.count:<6g} "
            f"@{_px(f.price)} {'taker' if f.is_taker else 'maker'}"
        )
    if args.raw and settlements:
        print("\nraw settlement example:")
        print(json.dumps(settlements[0], indent=2, default=str))
    return 0


def cmd_order(settings: Settings, args: argparse.Namespace) -> int:
    """Place ONE small live limit order by hand, print the raw exchange response, then cancel.

    Diagnostic for order-format problems. Requires the same gates as live trading.
    """
    problems = []
    if settings.env != "prod":
        problems.append("KALSHI_ENV must be prod")
    if settings.dry_run:
        problems.append("KALSHI_DRY_RUN must be false")
    if not settings.has_credentials:
        problems.append("API key not configured")
    if args.acknowledge != LIVE_ACK:
        problems.append(f"pass --acknowledge {LIVE_ACK}")
    if problems:
        sys.exit("refusing: " + "; ".join(problems))
    with KalshiClient.from_settings(settings, allow_live=True) as client:
        bal = client.get_balance()
        print(f"balance before: ${bal.balance:,.2f}")
        m = client.get_market(args.ticker)
        book = client.get_orderbook(args.ticker, depth=3)
        print(
            f"{m.ticker} closes {_fmt_time(m.close_time)}  yes bid/ask "
            f"{_px(book.best_yes_bid)}/{_px(book.best_yes_ask)}  "
            f"no bid/ask {_px(book.best_no_bid)}/{_px(book.best_no_ask)}"
        )
        price = args.price
        if price is None:
            # one tick below the best bid on our side: rests, cannot fill, safe to cancel
            bid = book.best_yes_bid if args.side == "yes" else book.best_no_bid
            if bid is None:
                sys.exit("empty book on that side; pass --price")
            price = round(max(0.01, bid - 0.02), 4)
        print(
            f"placing: buy {args.side} x{args.count} @ {price:.4f} (cost ${args.count * price:.2f})"
        )
        try:
            order = client.create_order(
                args.ticker,
                side=args.side,
                action="buy",
                count=args.count,
                price=price,
                expiration_ts=int(time.time() + 30),
            )
        except KalshiError as exc:
            print(f"REFUSED: {exc}")
            return 1
        print(
            f"accepted: order_id={order.order_id} status={order.status} "
            f"count={order.count} remaining={order.remaining_count}"
        )
        time.sleep(args.hold)
        try:
            fresh = client.get_order(order.order_id)
            print(f"after {args.hold}s: status={fresh.status} remaining={fresh.remaining_count}")
        except KalshiError as exc:
            print(f"get_order failed: {exc}")
        if not args.keep:
            try:
                client.cancel_order(order.order_id, ticker=args.ticker)
                print("cancelled")
            except KalshiError as exc:
                print(f"cancel failed (may already be filled/expired): {exc}")
        bal = client.get_balance()
        print(f"balance after:  ${bal.balance:,.2f}")
    return 0


def cmd_risk_reset(_: Settings, args: argparse.Namespace) -> int:
    """Clear a risk halt / cool-off after a manual review."""
    mode = "live" if args.live else "paper"
    with MarketDataStore(args.db) as store:
        risk = RiskEngine(store, RiskLimits(), mode)
        was = risk.state.halt_reason or "(not halted)"
        risk.reset()
    print(f"{mode}: reset. previous halt reason: {was}")
    return 0


def cmd_learn(_: Settings, args: argparse.Namespace) -> int:
    """Scheduled retraining: calibrate, evaluate candidates on held-out data, gate promotion."""
    try:
        from . import learn
    except ImportError:
        sys.exit('learn needs pandas: pip install -e ".[research]"')
    mode = "live" if args.live else "paper"
    with MarketDataStore(args.db) as store:
        if args.rollback:
            print(learn.rollback(store, mode))
            return 0
        if args.activate is not None:
            store.activate_config(mode, args.activate)
            print(f"activated config id={args.activate}; restart the trader to use it")
            return 0
        if args.history:
            for c in store.config_history(mode):
                flag = "*" if c["active"] else " "
                when = _fmt_time(datetime.fromtimestamp(c["created_ts"], tz=UTC))
                print(f"{flag} id={c['id']} {when} {c['source']:9} {json.dumps(c['params'])}")
            return 0
        print(learn.run(store, mode, promote=args.promote))
    return 0


def _find_pem_files() -> list[Path]:
    home = Path.home()
    roots = [home / "Downloads", home / "Desktop", home / "Documents", home / ".kalshi", Path.cwd()]
    found: list[Path] = []
    for root in roots:
        if root.is_dir():
            found.extend(p for p in root.glob("*.pem") if p.is_file())
            found.extend(p for p in root.glob("*.txt") if "key" in p.name.lower())
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def cmd_setup(settings: Settings, args: argparse.Namespace) -> int:
    """One-time interactive setup: find the API key file, write .env, verify the balance."""
    print("kalshi-bot setup\n")
    print("1. Private key file")
    candidates = _find_pem_files()
    key_path: Path | None = None
    if candidates:
        for i, p in enumerate(candidates[:8], 1):
            print(f"   [{i}] {p}")
        print("   [0] type a path")
        choice = input("   which file is your Kalshi private key? ").strip()
        if choice.isdigit() and 1 <= int(choice) <= min(8, len(candidates)):
            key_path = candidates[int(choice) - 1]
    if key_path is None:
        typed = input("   full path to the .pem file: ").strip().strip('"')
        key_path = Path(typed).expanduser()
    if not key_path.is_file():
        print(f"   not found: {key_path}")
        print(
            "   Create a key on kalshi.com (Account -> API keys), save the download, rerun setup."
        )
        return 1
    dest = Path.home() / ".kalshi" / "prod.pem"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if key_path.resolve() != dest.resolve():
        dest.write_bytes(key_path.read_bytes())
    try:
        from .auth import Signer

        Signer.from_pem_path("x", dest)
    except Exception as exc:  # noqa: BLE001
        print(f"   that file is not a valid RSA private key: {exc}")
        return 1
    print(f"   ok -> {dest}\n")

    print("2. API key ID (shown next to the key on kalshi.com)")
    current = settings.api_key_id
    prompt = f"   key id [{current}]: " if current else "   key id: "
    key_id = input(prompt).strip() or current
    if not key_id:
        print("   a key id is required")
        return 1

    env_path = Path(args.env_file or ".env")
    env_path.write_text(
        "KALSHI_ENV=prod\n"
        f"KALSHI_API_KEY_ID={key_id}\n"
        f"KALSHI_PRIVATE_KEY_PATH={dest}\n"
        "KALSHI_DRY_RUN=false\n",
        encoding="ascii",
    )
    print(f"   wrote {env_path.resolve()}\n")

    print("3. Checking the key against Kalshi")
    new_settings = dataclasses.replace(
        settings, env="prod", api_key_id=key_id, private_key_path=dest, dry_run=False
    )
    try:
        with KalshiClient.from_settings(new_settings) as client:
            bal = client.get_balance()
    except KalshiError as exc:
        print(f"   FAILED: {exc}")
        print("   Check the key id, and that the key was created on kalshi.com (not demo).")
        return 1
    print(f"   balance ${bal.balance:,.2f}\n")
    print("Setup complete. Double-click start-paper.cmd (no real orders) or start-live.cmd.")
    return 0


def _price_feeds(
    store: MarketDataStore, uni: Universe
) -> tuple[SpotWebSocket | None, PythStream | None, ProxyFeed | None]:
    """The live spot feeds the trader reads: Coinbase, Pyth (with a key), Swissquote (without)."""
    spot_ws = SpotWebSocket(store, uni.coinbase_symbols) if uni.coinbase_symbols else None
    # Hermes answers 401 without a key, so only start the stream when one is set.
    has_pyth_key = bool(os.getenv("PYTH_API_KEY", "").strip())
    pyth = PythStream(store, uni.pyth_symbols) if uni.pyth_symbols and has_pyth_key else None
    # Keyless fallback for the precious metals (Swissquote, rebased per window).
    proxy = ProxyFeed(store, uni.proxy_symbols) if uni.proxy_symbols and not has_pyth_key else None
    return spot_ws, pyth, proxy


def cmd_go(settings: Settings, args: argparse.Namespace) -> int:
    """Run recorder and trader together in one window. Paper unless --live."""
    uni = load_universe(args.universe)
    mode = "live" if args.live else "paper"
    if args.live:
        problems = []
        if settings.env != "prod":
            problems.append("KALSHI_ENV must be prod")
        if settings.dry_run:
            problems.append("KALSHI_DRY_RUN must be false")
        if not settings.has_credentials:
            problems.append("run `kalshi-bot setup` first")
        if args.acknowledge != LIVE_ACK:
            problems.append(f"pass --acknowledge {LIVE_ACK}")
        if problems:
            sys.exit("refusing live mode:\n  " + "\n  ".join(problems))
    strategy = FairValueStrategy(_params(args))
    trade_client = KalshiClient.from_settings(settings, allow_live=args.live)
    record_client = KalshiClient(settings.base_url, None, min_request_interval=0.15)
    with trade_client, record_client, MarketDataStore(args.db) as store:
        spot_ws, pyth, proxy = _price_feeds(store, uni)
        trader = Trader(
            trade_client,
            store,
            uni,
            strategy,
            _limits(args),
            mode=mode,
            interval=args.interval,
            spot_ws=spot_ws,
            pyth=pyth,
            proxy=proxy,
            proxy_margin=args.proxy_margin,
            allow_proxy=args.allow_proxy,
            blackout=EventBlackout.load(args.events),
        )
        # record what can be priced; the rest would only slow the tick down
        recorder = Recorder(
            record_client,
            store,
            series=[a.series for a in trader.tradable_assets()],
            interval=5.0,
        )
        if args.live:
            bal = trade_client.get_balance()
            print(f"LIVE TRADING on production. Balance ${bal.balance:,.2f}.")
            print(f"Limits: {trader.risk.limits}")
            print("Starting in 10 seconds; Ctrl-C to abort.")
            time.sleep(10)
        rec_thread = threading.Thread(
            target=lambda: recorder.run(install_signals=False), name="recorder", daemon=True
        )
        rec_thread.start()
        try:
            trader.run(max_ticks=args.ticks)
        finally:
            recorder.stop()
            rec_thread.join(timeout=10)
    return 0


# ---------------------------------------------------------------- parser


def _strategy_and_risk_args(s: argparse.ArgumentParser) -> None:
    g = s.add_argument_group("strategy")
    g.add_argument("--margin", type=float, default=0.03, help="edge beyond fees required")
    g.add_argument("--count", type=int, default=2, help="contracts per order before sizing")
    g.add_argument(
        "--dollars",
        type=float,
        default=0.0,
        help="stake per order in dollars (contracts = floor(dollars / ask)); overrides --count",
    )
    g.add_argument("--vol-window", type=float, default=1800.0)
    g.add_argument("--max-price", type=float, default=0.90)
    g.add_argument("--min-price", type=float, default=0.05)
    g.add_argument(
        "--exec",
        dest="exec_mode",
        choices=["maker", "taker", "hybrid"],
        default="maker",
        help="maker rests inside the spread fee-free (default)",
    )
    g.add_argument("--rest-seconds", type=float, default=20.0, help="maker order lifetime")
    g.add_argument("--max-spread", type=float, default=0.04, help="skip books wider than this")
    g.add_argument(
        "--kelly", type=float, default=0.0, help="fraction of Kelly for sizing (0 = fixed --count)"
    )
    g.add_argument(
        "--favourites-only",
        action="store_true",
        help="never buy a side priced under 0.50 (the model overestimates cheap sides)",
    )
    g.add_argument("--sigma-scale", type=float, default=1.0, help="multiply realized vol")
    g.add_argument(
        "--allow-proxy",
        action="store_true",
        help="trade metals priced off the rebased Swissquote proxy (off: record only)",
    )
    g.add_argument("--events", default="events.yaml", help="scheduled-event blackout file")
    g.add_argument(
        "--proxy-margin",
        type=float,
        default=0.02,
        help="extra edge required when spot is the rebased Swissquote proxy, not Pyth",
    )
    r = s.add_argument_group("risk")
    r.add_argument("--bankroll", type=float, default=200.0)
    r.add_argument("--daily-loss-cap", type=float, default=20.0)
    r.add_argument("--max-contracts", type=int, default=5)
    r.add_argument("--max-open", type=int, default=3)
    r.add_argument("--max-fraction", type=float, default=0.25)
    r.add_argument("--loss-streak", type=int, default=4)
    r.add_argument("--no-entry", type=float, default=120.0)
    r.add_argument("--max-data-age", type=float, default=10.0)
    r.add_argument("--kill-file", default="state/KILL")


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
    s = sub.add_parser("status", help=cmd_status.__doc__)
    s.add_argument("--raw", action="store_true", help="print raw balance and orders JSON")
    s.set_defaults(func=cmd_status)

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
        help=f"series ticker; repeatable (default: universe = {' '.join(DEFAULT_SERIES)})",
    )
    s.add_argument("--universe", default=None, help="path to a universe.yaml override")
    s.add_argument("--no-pyth", action="store_true", help="skip the Pyth price stream")
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

    s = sub.add_parser("series", help=cmd_series.__doc__)
    s.add_argument("--category", default=None, help="e.g. Crypto, Commodities")
    s.add_argument("--grep", default="15M", help="substring filter on ticker/title (default 15M)")
    s.set_defaults(func=cmd_series, default_env="prod")

    s = sub.add_parser("universe-check", help=cmd_universe_check.__doc__)
    s.add_argument("--universe", default=None)
    s.add_argument("--rules", action="store_true", help="print each market's settlement rule")
    s.set_defaults(func=cmd_universe_check, default_env="prod")

    s = sub.add_parser("pyth-feeds", help=cmd_pyth_feeds.__doc__)
    s.add_argument("--query", required=True)
    s.add_argument("--asset-type", default=None, help="crypto | metal | commodities | fx | equity")
    s.set_defaults(func=cmd_pyth_feeds)

    s = sub.add_parser("pyth-check", help=cmd_pyth_check.__doc__)
    s.add_argument("--universe", default=None)
    s.set_defaults(func=cmd_pyth_check)

    s = sub.add_parser("crossasset", help=cmd_crossasset.__doc__)
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--universe", default=None)
    s.set_defaults(func=cmd_crossasset)

    s = sub.add_parser("setup", help=cmd_setup.__doc__)
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("go", help=cmd_go.__doc__)
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--universe", default=None)
    s.add_argument("--interval", type=float, default=5.0)
    s.add_argument("--ticks", type=int, default=None)
    s.add_argument("--live", action="store_true")
    s.add_argument("--acknowledge", default="")
    _strategy_and_risk_args(s)
    s.set_defaults(func=cmd_go, default_env="prod")

    s = sub.add_parser("trade", help=cmd_trade.__doc__)
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--universe", default=None)
    s.add_argument("--interval", type=float, default=5.0)
    s.add_argument("--ticks", type=int, default=None)
    s.add_argument("--live", action="store_true", help="real orders (see README gates)")
    s.add_argument("--acknowledge", default="", help=f"must equal {LIVE_ACK} for --live")
    _strategy_and_risk_args(s)
    s.set_defaults(func=cmd_trade, default_env="prod")

    s = sub.add_parser("learn", help=cmd_learn.__doc__)
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--live", action="store_true", help="learn from live decisions (default paper)")
    s.add_argument("--promote", action="store_true", help="activate the winning candidate")
    s.add_argument("--activate", type=int, default=None, help="activate a saved config by id")
    s.add_argument("--rollback", action="store_true", help="revert to the previous config")
    s.add_argument("--history", action="store_true", help="list saved configs")
    s.set_defaults(func=cmd_learn)

    s = sub.add_parser("trade-status", help=cmd_trade_status.__doc__)
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--live", action="store_true")
    s.add_argument("--limit", type=int, default=15)
    s.set_defaults(func=cmd_trade_status)

    s = sub.add_parser("reconcile", help=cmd_reconcile.__doc__)
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--raw", action="store_true")
    s.set_defaults(func=cmd_reconcile)

    s = sub.add_parser("order", help=cmd_order.__doc__)
    s.add_argument("--ticker", required=True)
    s.add_argument("--side", choices=["yes", "no"], default="yes")
    s.add_argument("--count", type=int, default=1)
    s.add_argument("--price", type=float, default=None, help="dollars; default: 2c under the bid")
    s.add_argument("--hold", type=float, default=5.0, help="seconds to leave it resting")
    s.add_argument("--keep", action="store_true", help="do not cancel afterwards")
    s.add_argument("--acknowledge", default="")
    s.set_defaults(func=cmd_order, default_env="prod")

    s = sub.add_parser("risk-reset", help=cmd_risk_reset.__doc__)
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--live", action="store_true")
    s.set_defaults(func=cmd_risk_reset)

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
        "series",
        "universe-check",
        "pyth-feeds",
        "pyth-check",
        "crossasset",
        "trade-status",
        "risk-reset",
        "learn",
        "setup",
    ):
        logging.getLogger(__name__).info("env=%s dry_run=%s", settings.env, settings.dry_run)
    try:
        return args.func(settings, args)
    except (KalshiError, SchemaMismatch) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
