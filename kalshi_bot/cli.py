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


def cmd_setup(_: Settings, args: argparse.Namespace) -> int:
    """Write .env from a key file (as downloaded or pasted into a text file) and verify it."""
    from pathlib import Path

    from .auth import Signer, find_key_id

    key_file = Path(args.key_file).expanduser()
    if not key_file.exists():
        sys.exit(f"key file not found: {key_file}")
    raw = key_file.read_bytes()
    key_id = args.key_id or find_key_id(raw)
    if not key_id:
        key_id = input("Key id (shown on Kalshi's API keys page, looks like 8-4-4-4-12 hex): ")
    key_id = key_id.strip()
    try:
        Signer.from_pem(key_id, raw)
    except ValueError as exc:
        sys.exit(f"cannot use {key_file}: {exc}")
    env_path = Path(args.env_out)
    if env_path.exists():
        backup = env_path.with_suffix(env_path.suffix + ".bak")
        backup.write_bytes(env_path.read_bytes())
        print(f"backed up existing {env_path} to {backup}")
    live = bool(args.live)
    env_path.write_text(
        "# written by kalshi-bot setup\n"
        f"KALSHI_ENV={'prod' if live else 'demo'}\n"
        f"KALSHI_API_KEY_ID={key_id}\n"
        f"KALSHI_PRIVATE_KEY_PATH='{key_file}'\n"
        f"KALSHI_DRY_RUN={'false' if live else 'true'}\n"
        "KALSHI_MIN_REQUEST_INTERVAL=0.15\n"
        "KALSHI_LOG_LEVEL=INFO\n"
    )
    print(f"wrote {env_path}: env={'prod' if live else 'demo'} key id={key_id}")
    print(f"private key loads OK from {key_file}")
    if live:
        print("\nNext, in this window:            kalshi-bot --env prod status")
        print(
            "Then start the dashboard here:   kalshi-bot demo-ui --state-file state/live_loop.json"
        )
        print(
            "And in a second window:          "
            "kalshi-bot --env prod live-trade --dollars 2 --loss-cap 40 --real-money"
        )
    else:
        print("\nNext: kalshi-bot status")
    return 0


def cmd_status(settings: Settings, _: argparse.Namespace) -> int:
    """Exchange status, balance and open positions."""
    with _client(settings, need_auth=True) as client:
        status = client.exchange_status()
        print(
            f"exchange:   trading_active={status.get('trading_active')} "
            f"exchange_active={status.get('exchange_active')}"
        )
        for idx in status.get("exchange_index_statuses") or []:
            print(
                f"  shard {idx.get('exchange_index')}: {idx.get('description', '')}  "
                f"trading_active={idx.get('trading_active')}"
            )
        bal = client.get_balance()
        print(f"balance:    ${bal.balance:,.2f}")
        for idx, amount in sorted(bal.breakdown.items()):
            print(f"  shard {idx}: ${amount:,.2f}")
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
        f"{'strike':>10} {'shard':>5}  close (UTC)"
    )
    for m in markets[: args.limit]:
        strike = f"{m.strike:,.2f}" if m.strike is not None else "-"
        shard = "-" if m.exchange_index is None else str(m.exchange_index)
        print(
            f"{m.ticker:30} {m.status:8} {_px(m.yes_bid):>6} {_px(m.yes_ask):>6} "
            f"{_px(m.last_price):>6} {m.volume:>10,.0f} {strike:>10} {shard:>5}  "
            f"{_fmt_time(m.close_time)}"
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


def cmd_learn(_: Settings, args: argparse.Namespace) -> int:
    """Retrain on recorded data, gate promotion, check live drift; write state/params.json."""
    try:
        from . import learn
    except ImportError:
        sys.exit('learn needs pandas: pip install -e ".[research]"')
    from pathlib import Path

    def once() -> None:
        params = learn.run_cycle(
            db_path=args.db,
            params_path=args.params,
            history_path=args.history,
            decisions_path=args.decisions or None,
            live_state_path=args.live_state or None,
            alerts_path=args.alerts or None,
        )
        record = {}
        try:
            lines = Path(args.history).read_text(encoding="utf-8").splitlines()
            record = json.loads(lines[-1]) if lines else {}
        except (OSError, ValueError):
            pass
        print(learn.describe(params, record))

    if not args.every:
        once()
        return 0
    print(f"learning every {args.every:.0f}s; Ctrl-C to stop")
    while True:
        try:
            once()
        except Exception as exc:  # noqa: BLE001 - a bad cycle must not stop the schedule
            logging.getLogger(__name__).warning("learn cycle failed: %s", exc)
        try:
            time.sleep(args.every)
        except KeyboardInterrupt:
            return 0


def cmd_fairvalue(_: Settings, args: argparse.Namespace) -> int:
    """Test the realised-volatility fair-value model against the book (brief, section 3)."""
    try:
        from . import fairvalue
    except ImportError:
        sys.exit('fairvalue needs pandas: pip install -e ".[research]"')
    data = fairvalue.load(args.db, series=args.series or None)
    print(fairvalue.report(data, min_ttc=args.min_ttc, show_trades=args.show_trades))
    return 0


def _loop_config(args: argparse.Namespace):  # -> LoopConfig
    from pathlib import Path

    from .demo_loop import DEFAULT_SERIES, LoopConfig

    cfg = LoopConfig(
        series=tuple(args.series) if args.series else DEFAULT_SERIES,
        contracts=args.contracts,
        dollars=args.dollars,
        max_price=args.max_price,
        loss_cap=args.loss_cap,
        profit_target=args.profit_target if args.profit_target > 0 else None,
        max_trades=args.max_trades,
        min_ttc=args.min_ttc,
        interval=args.interval,
        first_side=args.first_side,
        stop_file=Path(args.stop_file),
        state_file=Path(args.state_file),
        strategy=args.strategy,
        margin=args.margin,
        vol_window=args.vol_window,
        spot_db=Path(args.spot_db) if args.spot_db else None,
        decision_log=Path(args.decision_log) if args.decision_log else None,
        params_path=Path(args.params) if args.params else None,
        exits=not args.no_exits,
        exit_margin=args.exit_margin,
        take_profit=args.take_profit,
        stop_loss=args.stop_loss,
        max_entries=args.max_entries,
        free_entries=args.free_entries,
        risk_fraction=args.risk_fraction,
        max_dollars=args.max_dollars,
        entry=args.entry,
        maker_wait_s=args.maker_wait,
        reconcile_s=args.reconcile,
        spot_source=args.spot_source,
        spot_smooth_s=args.spot_smooth,
        stop_value=args.stop_value,
        trend_window=args.trend_window,
        trend_bps=args.trend_bps,
        min_hold_s=args.min_hold,
        reentry_cooloff_s=args.cooloff,
        allow_flip=args.allow_flip,
        max_consecutive_losses=args.max_consecutive_losses,
        loss_pause_s=args.loss_pause,
        pause_file=Path(args.pause_file),
        alerts_path=Path(args.alerts) if args.alerts else None,
    )
    try:
        cfg.validate()
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    return cfg


def _loop_housekeeping(cfg, args: argparse.Namespace) -> bool:
    """--reset and --status handling shared by demo-trade and live-trade. True = done."""
    from .demo_loop import LoopState

    if args.reset:
        if cfg.state_file.exists():
            cfg.state_file.unlink()
            print(f"cleared {cfg.state_file}")
        if cfg.stop_file.exists():
            cfg.stop_file.unlink()
            print(f"cleared {cfg.stop_file}")
        if args.status or not args.run_after_reset:
            return True
    if args.status:
        state = LoopState.load(cfg.state_file)
        print(state.summary())
        for h in state.history[-10:]:
            print(
                f"  {h['ticker']:32} {h['side']:3} x{h['count']:.0f} @ {h['price']:.3f} "
                f"{h['result']:3} {h['net']:+.2f}"
            )
        return True
    if cfg.stop_file.exists():
        sys.exit(f"{cfg.stop_file} exists; delete it (or use --reset) to start")
    return False


def cmd_demo_trade(settings: Settings, args: argparse.Namespace) -> int:
    """Demo-only alternating up/down trader with loss cap and profit target."""
    from .demo_loop import DemoLoop

    if settings.env != "demo":
        sys.exit("demo-trade only runs against the demo exchange: set KALSHI_ENV=demo")
    cfg = _loop_config(args)
    if _loop_housekeeping(cfg, args):
        return 0
    if settings.dry_run:
        print("KALSHI_DRY_RUN=true: orders are simulated at the limit price, nothing is sent.")
    else:
        print("Sending paper orders to the demo exchange. Ctrl-C or the stop file stops it.")
    print(f"State: {cfg.state_file}   Stop file: {cfg.stop_file}   Dashboard: kalshi-bot demo-ui")
    with _client(settings, need_auth=not settings.dry_run) as client:
        loop = DemoLoop(client, cfg)
        reason = loop.run()
    print(f"stopped: {reason}")
    print(loop.state.summary())
    return 0


LIVE_MAX_DOLLARS = 20.0
LIVE_MAX_LOSS_CAP = 50.0


def cmd_live_trade(settings: Settings, args: argparse.Namespace) -> int:
    """The same alternating trader on PRODUCTION with real money. Requires --real-money."""
    from .demo_loop import DemoLoop
    from .fees import fee_per_contract

    if settings.env != "prod":
        sys.exit(
            "live-trade needs production selected explicitly: kalshi-bot --env prod live-trade"
        )
    if settings.dry_run:
        sys.exit("live-trade needs KALSHI_DRY_RUN=false in .env (it is true, the safe default)")
    cfg = _loop_config(args)
    if _loop_housekeeping(cfg, args):
        return 0
    if not args.real_money:
        sys.exit(
            "live-trade places real orders from your Kalshi balance. Re-run with --real-money "
            "if that is what you want."
        )
    if cfg.dollars is None or cfg.dollars > LIVE_MAX_DOLLARS:
        sys.exit(f"live-trade requires --dollars, at most {LIVE_MAX_DOLLARS:.0f} per trade")
    if cfg.loss_cap > LIVE_MAX_LOSS_CAP:
        sys.exit(f"live-trade caps --loss-cap at {LIVE_MAX_LOSS_CAP:.0f}")
    with _client(settings, need_auth=True) as probe:
        bal = probe.get_balance()
        shards = market_shards(probe, cfg.series)
    balance = bal.balance
    plan = shard_plan(bal, shards, needed=min(balance, cfg.loss_cap + 5.0))
    if plan is not None and args.move_funds is not None:
        plan = (args.move_funds, plan[1], plan[2])
    fee = fee_per_contract(0.5)
    per_trade = max(1, int(cfg.dollars / 0.5)) * fee
    print("=" * 72)
    if cfg.strategy == "fairvalue":
        print("REAL MONEY. Strategy: fair value. Trades only when the model's probability")
        print(f"beats the ask by the fee plus {cfg.margin:.2f}; most markets will be skipped.")
        print("Its edge is unproven until the recorded data says otherwise (kalshi-bot fairvalue).")
    else:
        print("REAL MONEY. This strategy alternates YES/NO with no edge; its expected")
        print(
            f"result is minus the fee, about ${per_trade:.2f} per trade at 50c, before the spread."
        )
    print(f"Balance ${balance:,.2f}. Per trade ~${cfg.dollars:.2f} on {', '.join(cfg.series)}.")
    gain = "(no profit cap)"
    if cfg.profit_target is not None:
        gain = f"or +${cfg.profit_target:.2f} gain"
    print(f"Stops at -${cfg.loss_cap:.2f} realised loss {gain}.")
    print(f"Stop any time: Ctrl-C, the file {cfg.stop_file}, or the dashboard button.")
    print("=" * 72)
    from .demo_loop import LoopState

    so_far = LoopState.load(cfg.state_file).realized_pnl
    remaining = cfg.loss_cap + so_far  # realised P&L is negative when losing
    print(f"Realised so far {so_far:+.2f}; ${max(remaining, 0):,.2f} more loss until the cap.")
    if remaining > balance:
        print(f"Note: the balance (${balance:,.2f}) would run out before the cap does.")
    if bal.breakdown:
        print("Balance by exchange shard: " + _shards_text(bal))
        print("Markets trade on: " + ", ".join(f"{k}: shard {v}" for k, v in shards.items()))
    if plan is not None:
        amount, src, dst = plan
        if amount > bal.on_shard(src):
            sys.exit(f"shard {src} holds ${bal.on_shard(src):,.2f}, less than ${amount:,.2f}")
        print(
            f"The markets' shard {dst} holds ${bal.on_shard(dst):,.2f} and orders draw on it, "
            f"so ${amount:,.2f} will be moved from shard {src} to shard {dst} when you continue."
        )
    if not args.yes:
        answer = input("Type TRADE to place real orders, anything else to abort: ")
        if answer.strip() != "TRADE":
            print("aborted")
            return 1
    client = KalshiClient.from_settings(settings, allow_live=True)
    with client:
        if plan is not None:
            _move_funds(client, *plan)
        loop = DemoLoop(client, cfg, allow_production=True)
        reason = loop.run()
    print(f"stopped: {reason}")
    print(loop.state.summary())
    return 0


def market_shards(client: KalshiClient, series: tuple[str, ...]) -> dict[str, int]:
    """Exchange shard of the open market in each series, where the API reports one."""
    out: dict[str, int] = {}
    for name in series:
        for m in client.get_markets(series_ticker=name, status="open", max_pages=1):
            if m.exchange_index is not None:
                out[name] = m.exchange_index
                break
    return out


def shard_plan(bal, shards: dict[str, int], needed: float) -> tuple[float, int, int] | None:
    """(amount, source shard, destination shard) to fund the markets' shard, or None.

    None when the API reports no breakdown, the markets report no shard, the
    markets sit on different shards, or the destination already holds
    ``needed`` dollars. Kalshi runs several exchange shards and an order
    draws only on the balance of the shard its market lives on.
    """
    if not bal.breakdown or not shards:
        return None
    targets = set(shards.values())
    if len(targets) != 1:
        return None
    dst = targets.pop()
    have = bal.breakdown.get(dst, 0.0)
    if have >= needed:
        return None
    others = [(i, v) for i, v in bal.breakdown.items() if i != dst]
    if not others:
        return None
    src, src_amount = max(others, key=lambda t: t[1])
    if src_amount <= 0.01:
        return None
    return (round(min(needed - have, src_amount), 2), src, dst)


def _shards_text(bal) -> str:
    return ", ".join(f"shard {i}: ${v:,.2f}" for i, v in sorted(bal.breakdown.items()))


def _move_funds(client: KalshiClient, amount: float, src: int, dst: int) -> None:
    transfer_id = client.transfer_between_shards(amount, source_shard=src, destination_shard=dst)
    print(f"moving ${amount:,.2f} from shard {src} to shard {dst}: transfer {transfer_id}")
    if transfer_id is None:
        return
    # The status lookup can answer 404 for a transfer that did go through, so the
    # balance on the destination shard is the source of truth.
    before = client.get_balance().on_shard(dst)
    bal = None
    for _ in range(30):
        bal = client.get_balance()
        if bal.on_shard(dst) >= before + amount - 0.01:
            break
        time.sleep(1.0)
    if bal is not None:
        print("balance by shard now: " + _shards_text(bal))
        if bal.on_shard(dst) < before + amount - 0.01:
            print("the transfer has not shown up on the destination shard yet; continuing")


def cmd_transfer(settings: Settings, args: argparse.Namespace) -> int:
    """Move funds between Kalshi exchange shards within your own account."""
    if settings.dry_run:
        sys.exit("transfer needs KALSHI_DRY_RUN=false")
    with _client(settings, need_auth=True) as probe:
        bal = probe.get_balance()
    print(
        "balance by shard: " + (_shards_text(bal) or f"${bal.balance:,.2f} (no breakdown reported)")
    )
    if bal.on_shard(args.source) < args.amount:
        sys.exit(f"shard {args.source} holds ${bal.on_shard(args.source):,.2f}, less than asked")
    if not args.yes:
        answer = input(
            f"Move ${args.amount:,.2f} from shard {args.source} to shard {args.to}? Type MOVE: "
        )
        if answer.strip() != "MOVE":
            print("aborted")
            return 1
    with KalshiClient.from_settings(settings, allow_live=settings.is_prod) as client:
        _move_funds(client, args.amount, args.source, args.to)
    return 0


def cmd_demo_ui(_: Settings, args: argparse.Namespace) -> int:
    """Local web dashboard for the demo loop (reads its state file; can stop it)."""
    from pathlib import Path

    from .demo_ui import serve

    files = (
        [Path(args.state_file)]
        if args.state_file
        else [Path("state/live_loop.json"), Path("state/demo_loop.json")]
    )
    server = serve(
        files,
        Path(args.stop_file),
        host=args.host,
        port=args.port,
        pause_file=Path(args.pause_file),
        alerts_file=Path(args.alerts) if args.alerts else None,
    )
    print(f"dashboard at http://{args.host}:{server.server_address[1]}/  (Ctrl-C to stop)")
    print("showing whichever of these was updated most recently: " + ", ".join(map(str, files)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
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

    s = sub.add_parser("setup", help=cmd_setup.__doc__)
    s.add_argument("--key-file", required=True, help="path to the downloaded key or a text file")
    s.add_argument("--key-id", default=None, help="Kalshi key id; found in the file if omitted")
    s.add_argument(
        "--live",
        action="store_true",
        help="configure for production with real orders (else demo, dry run)",
    )
    s.add_argument("--env-out", default=".env", help="where to write the settings")
    s.set_defaults(func=cmd_setup)
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

    s = sub.add_parser("fairvalue", help=cmd_fairvalue.__doc__)
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--series", action="append", help="restrict to a series; repeatable")
    s.add_argument(
        "--min-ttc", type=float, default=120.0, help="no entries under this many seconds to close"
    )
    s.add_argument(
        "--show-trades", type=int, default=0, help="also print the last N trades of the backtest"
    )
    s.set_defaults(func=cmd_fairvalue)

    s = sub.add_parser("learn", help=cmd_learn.__doc__)
    s.add_argument("--db", default=DEFAULT_DB, help="recorder database to retrain on")
    s.add_argument("--params", default="state/params.json", help="parameter file to write")
    s.add_argument("--history", default="state/learn_history.jsonl")
    s.add_argument("--decisions", default="state/decisions.jsonl", help="live decision log")
    s.add_argument("--live-state", default="state/live_loop.json", help="live loop state")
    s.add_argument("--alerts", default="state/alerts.jsonl", help="event feed for the dashboard")
    s.add_argument(
        "--every", type=float, default=0.0, help="repeat every N seconds (0 = run once and exit)"
    )
    s.set_defaults(func=cmd_learn)

    s = sub.add_parser("demo-trade", help=cmd_demo_trade.__doc__)
    _add_loop_args(s, state_file="state/demo_loop.json")
    s.set_defaults(func=cmd_demo_trade)

    s = sub.add_parser("live-trade", help=cmd_live_trade.__doc__)
    _add_loop_args(
        s,
        state_file="state/live_loop.json",
        loss_cap=50.0,
        profit_target=0.0,
        strategy="fairvalue",
    )
    s.add_argument(
        "--real-money",
        action="store_true",
        help="required: acknowledge that orders come from your real Kalshi balance",
    )
    s.add_argument("--yes", action="store_true", help="skip the typed TRADE confirmation")
    s.add_argument(
        "--move-funds",
        type=float,
        default=None,
        help="dollars to move to the markets' exchange shard first (default: loss cap + 5)",
    )
    s.set_defaults(func=cmd_live_trade)

    s = sub.add_parser("transfer", help=cmd_transfer.__doc__)
    s.add_argument("--amount", type=float, required=True, help="dollars to move")
    s.add_argument("--source", type=int, default=0, help="source shard index (default 0)")
    s.add_argument("--to", type=int, required=True, help="destination shard index")
    s.add_argument("--yes", action="store_true", help="skip the typed MOVE confirmation")
    s.set_defaults(func=cmd_transfer)

    s = sub.add_parser("demo-ui", help=cmd_demo_ui.__doc__)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument(
        "--state-file",
        default=None,
        help="loop state to show (default: the fresher of state/live_loop.json and "
        "state/demo_loop.json)",
    )
    s.add_argument("--stop-file", default="state/STOP")
    s.add_argument("--pause-file", default="state/PAUSE")
    s.add_argument("--alerts", default="state/alerts.jsonl", help="event feed to show")
    s.set_defaults(func=cmd_demo_ui)

    s = sub.add_parser("cancel-all", help=cmd_cancel_all.__doc__)
    s.add_argument("--ticker", default=None)
    s.set_defaults(func=cmd_cancel_all)
    return p


def _add_loop_args(
    s: argparse.ArgumentParser,
    *,
    state_file: str,
    loss_cap: float = 5.0,
    profit_target: float = 10.0,
    strategy: str = "alternate",
) -> None:
    s.add_argument(
        "--series",
        action="append",
        help="series to trade; repeatable (default: KXBTC15M and KXDOGE15M)",
    )
    s.add_argument(
        "--dollars",
        type=float,
        default=None,
        help="spend about this much per trade (contracts = dollars / ask); overrides --contracts",
    )
    s.add_argument("--contracts", type=int, default=1, help="contracts per trade")
    s.add_argument(
        "--max-price",
        type=float,
        default=0.60,
        help="never pay more than this per contract (dollars); caps the loss per trade",
    )
    s.add_argument(
        "--loss-cap",
        type=float,
        default=loss_cap,
        help=f"stop when realised P&L falls to -X dollars (default {loss_cap:.0f})",
    )
    s.add_argument(
        "--profit-target",
        type=float,
        default=profit_target,
        help=f"stop when realised P&L reaches X dollars; 0 means no profit cap "
        f"(default {profit_target:g})",
    )
    s.add_argument("--max-trades", type=int, default=None, help="stop after N trades")
    s.add_argument(
        "--min-ttc", type=float, default=120.0, help="no entries under this many seconds to close"
    )
    s.add_argument("--interval", type=float, default=5.0, help="seconds between ticks")
    s.add_argument("--first-side", choices=["yes", "no"], default="yes")
    s.add_argument(
        "--strategy",
        choices=["alternate", "fairvalue"],
        default=strategy,
        help="alternate: YES/NO in turn (no edge). fairvalue: the research brief's model; "
        f"trades only when fair value beats the ask by the fee plus --margin (default {strategy})",
    )
    s.add_argument(
        "--margin",
        type=float,
        default=0.03,
        help="fairvalue: required edge beyond the fee (default 0.03)",
    )
    s.add_argument(
        "--vol-window",
        type=float,
        default=1800.0,
        help="fairvalue: seconds of spot history behind the volatility estimate",
    )
    s.add_argument(
        "--spot-db",
        default="state/market_data.sqlite",
        help="recorder database used to seed spot history at start ('' to skip)",
    )
    s.add_argument(
        "--decision-log",
        default="state/decisions.jsonl",
        help="where every decision and its inputs are appended ('' to disable)",
    )
    s.add_argument(
        "--params",
        default="state/params.json",
        help="parameter file written by `kalshi-bot learn`; the strategy reloads it live "
        "('' to ignore)",
    )
    s.add_argument("--no-exits", action="store_true", help="always hold to settlement")
    s.add_argument(
        "--exit-margin",
        type=float,
        default=0.02,
        help="fairvalue: sell when the bid beats the model's value by this after fees "
        "(never below --margin, so a round trip needs at least the entry edge back)",
    )
    s.add_argument(
        "--take-profit",
        type=float,
        default=0.0,
        help="alternate: sell when the bid is this far above entry (dollars; 0 = off)",
    )
    s.add_argument(
        "--stop-loss",
        type=float,
        default=0.0,
        help="alternate: sell when the bid is this far below entry (dollars; 0 = off)",
    )
    s.add_argument(
        "--max-entries",
        type=int,
        default=2,
        help="entries per 15-minute market; a re-entry needs the previous position sold and "
        "a fresh signal (default 2, 1 = one entry only, at most 6)",
    )
    s.add_argument(
        "--free-entries",
        type=int,
        default=1,
        help="entries beyond this in one market require that market to be in profit so far "
        "(default 1)",
    )
    s.add_argument(
        "--min-hold",
        type=float,
        default=60.0,
        help="seconds a filled position is held before an exit may fire (default 60)",
    )
    s.add_argument(
        "--cooloff",
        type=float,
        default=120.0,
        help="seconds after selling out of a market before it may be entered again (default 120)",
    )
    s.add_argument(
        "--allow-flip",
        action="store_true",
        help="allow buying the opposite side of a market already traded (off by default)",
    )
    s.add_argument(
        "--max-consecutive-losses",
        type=int,
        default=3,
        help="after this many losing results in a row, no entries for --loss-pause seconds "
        "(default 3; 0 disables)",
    )
    s.add_argument(
        "--loss-pause",
        type=float,
        default=1800.0,
        help="seconds the consecutive-loss breaker holds entries (default 1800, two windows)",
    )
    s.add_argument(
        "--stop-value",
        type=float,
        default=0.10,
        help="fairvalue: a sale below entry is only made when the model values the position "
        "at or under this (default 0.10; 0 = never sell at a loss, hold to settlement)",
    )
    s.add_argument(
        "--trend-window",
        type=float,
        default=300.0,
        help="fairvalue: never fade a spot move over this many seconds (default 300)",
    )
    s.add_argument(
        "--trend-bps",
        type=float,
        default=10.0,
        help="fairvalue: ...of at least this many basis points (default 10; 0 disables)",
    )
    s.add_argument(
        "--spot-smooth",
        type=float,
        default=10.0,
        help="fairvalue: the model's spot is the mean over this many seconds (default 10; "
        "0 = the last print)",
    )
    s.add_argument(
        "--risk-fraction",
        type=float,
        default=None,
        help="stake this fraction of the bankroll per trade (e.g. 0.02); default: whatever "
        "the learning loop has promoted (0 until a candidate passes), else --dollars",
    )
    s.add_argument(
        "--max-dollars",
        type=float,
        default=20.0,
        help="ceiling per trade under fixed-fraction sizing (default 20)",
    )
    s.add_argument(
        "--entry",
        choices=["maker", "taker"],
        default="maker",
        help="maker: rest one tick inside the spread, then take after --maker-wait seconds "
        "if unfilled (saves the spread and usually the fee). taker: pay the ask at once",
    )
    s.add_argument("--maker-wait", type=float, default=20.0, help="seconds a maker order may rest")
    s.add_argument(
        "--reconcile",
        type=float,
        default=120.0,
        help="seconds between checks of the loop's positions against the exchange; a mismatch "
        "that persists across two checks halts the loop (0 = never check)",
    )
    s.add_argument(
        "--spot-source",
        choices=["auto", "db", "rest"],
        default="auto",
        help="fairvalue: where spot comes from. auto: the recorder database when its latest "
        "tick is fresh, else Coinbase REST. db: the database only. rest: Coinbase only",
    )
    s.add_argument("--state-file", default=state_file)
    s.add_argument("--stop-file", default="state/STOP")
    s.add_argument("--pause-file", default="state/PAUSE")
    s.add_argument("--alerts", default="state/alerts.jsonl", help="event feed for the dashboard")
    s.add_argument("--status", action="store_true", help="print the saved state and exit")
    s.add_argument(
        "--reset", action="store_true", help="delete the saved state and stop file, then exit"
    )
    s.add_argument(
        "--run-after-reset", action="store_true", help="with --reset: start the loop afterwards"
    )


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
        "setup",
        "markets",
        "record-stats",
        "record-dump",
        "analyze",
        "spot-ws",
        "whale",
        "fairvalue",
        "learn",
        "demo-ui",
    ):
        logging.getLogger(__name__).info("env=%s dry_run=%s", settings.env, settings.dry_run)
    try:
        return args.func(settings, args)
    except (KalshiError, SchemaMismatch) as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
