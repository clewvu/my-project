"""Command line entry point: ``kalshi-sports <command>``.

Phase 1 commands: probe Kalshi's sports catalogue, record everything, inspect
what was recorded, test the external feeds, and print the first
Kalshi-versus-consensus table. Nothing here places orders.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sqlite3
import sys
import time
from datetime import UTC, datetime

from kalshi_bot.client import KalshiClient, KalshiError
from kalshi_bot.config import BASE_URLS, Settings

from . import __version__, leagues
from .catalog import classify
from .compare import compare, format_table
from .config import SportsSettings
from .consensus import consensus_for_game, recent_games
from .feeds.devig import METHODS, american_to_decimal, devig, overround
from .feeds.odds import TheOddsApiFeed
from .feeds.scores import EspnScoreFeed, today_eastern
from .recorder import SportsRecorder
from .storage import SchemaMismatch, SportsDataStore

DEFAULT_DB = "state/sports_data.sqlite"


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _leagues(args: argparse.Namespace, sports: SportsSettings) -> tuple[str, ...]:
    keys = tuple(k.lower() for k in (args.league or [])) or sports.leagues
    for k in keys:
        if k not in leagues.LEAGUES:
            sys.exit(f"unknown league {k!r}; choose from {', '.join(leagues.LEAGUES)}")
    return keys


def _series(args: argparse.Namespace, keys: tuple[str, ...]) -> list[str]:
    return list(args.series) if args.series else leagues.series_for_leagues(keys)


def _fmt_ts(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%MZ") if ts else "-"


# ---------------------------------------------------------------- commands


def cmd_discover(settings: Settings, args: argparse.Namespace) -> int:
    """Probe Kalshi for sports series and markets; the answers to the design's 'verify' items."""
    sports = SportsSettings.from_env()
    keys = _leagues(args, sports)
    with KalshiClient.from_settings(settings) as client:
        print(f"# kalshi-sports {__version__} discover against {settings.base_url}")
        if args.list_series:
            try:
                series = client.get_series_list(category=args.category)
            except KalshiError as exc:
                print(f"GET /series failed: {exc}")
                series = []
            print(f"\n## /series category={args.category!r}: {len(series)} returned")
            words = tuple(w.lower() for lg in keys for w in leagues.LEAGUES[lg].keywords)
            for s in series:
                text = json.dumps(s, default=str).lower()
                ticker = s.get("ticker") or s.get("series_ticker") or "?"
                if args.raw:
                    print(json.dumps(s, default=str))
                elif any(w in text for w in words) or str(ticker).upper().startswith("KX"):
                    print(
                        f"{ticker:<28} {s.get('title') or ''}  [{s.get('category') or ''}]"
                        f" tags={s.get('tags')}"
                    )
        print("\n## candidate series -> open markets")
        for series_ticker in _series(args, keys):
            try:
                markets = client.get_markets(
                    series_ticker=series_ticker, status="open", max_pages=3
                )
            except KalshiError as exc:
                print(f"{series_ticker:<20} ERROR {exc}")
                continue
            lg = leagues.league_for_series(series_ticker)
            print(
                f"{series_ticker:<20} {len(markets):>4} open  league={lg.key if lg else '?'} "
                f"kind={lg.series_kind(series_ticker) if lg else '?'}"
            )
            events: dict[str, dict] = {}
            if markets and args.events:
                try:
                    for ev in client.get_events(series_ticker=series_ticker, status="open"):
                        events[str(ev.get("event_ticker") or ev.get("ticker"))] = ev
                except KalshiError as exc:
                    print(f"    events: ERROR {exc}")
            for m in markets[: args.show]:
                gm = classify(m, events.get(m.event_ticker or ""))
                if args.raw:
                    print(json.dumps(m.raw, default=str))
                    if m.event_ticker in events:
                        print(json.dumps(events[m.event_ticker], default=str))
                else:
                    print(
                        f"    {m.ticker:<36} {gm.game_date or '?'} "
                        f"{gm.away or '?'}({gm.away_abbr})@{gm.home or '?'}({gm.home_abbr})"
                        f" side={gm.side_team or gm.side} line={gm.line} shard={m.exchange_index}"
                        f" start={_fmt_ts(gm.start_ts)}{'' if gm.start_exact else '~'}"
                        f" bid={m.yes_bid} ask={m.yes_ask} vol={m.volume:.0f}"
                        f" close={_fmt_ts(m.close_time.timestamp() if m.close_time else None)}"
                    )
                    print(f"        title: {m.title}")
                    if gm.subtitle:
                        print(f"        sub:   {gm.subtitle}")
                    if gm.rules and args.rules:
                        print(f"        rules: {str(gm.rules)[:400]}")
    print(
        "\nPaste this output back into the chat. If a series shows 0 open or ERROR, the ticker "
        "guess is wrong: run with --list-series --raw to find the real one."
    )
    return 0


def cmd_record(settings: Settings, args: argparse.Namespace) -> int:
    """Record sports books, trades, settlements, odds and game state to SQLite (Ctrl-C stops)."""
    sports = SportsSettings.from_env()
    keys = _leagues(args, sports)
    odds = None
    if not args.no_odds:
        if sports.has_odds:
            odds = TheOddsApiFeed(sports.odds_api_key, regions=sports.odds_regions)
        else:
            logging.getLogger(__name__).warning(
                "ODDS_API_KEY not set: recording without sportsbook odds (H1 needs them)"
            )
    scores = None if args.no_scores else EspnScoreFeed()
    try:
        with KalshiClient.from_settings(settings) as client, SportsDataStore(args.db) as store:
            rec = SportsRecorder(
                client,
                store,
                series=_series(args, keys),
                league_keys=keys,
                interval=args.interval,
                fast_cadence=args.fast,
                book_window_s=args.book_window * 3600,
                discover_interval=args.discover_every,
                book_depth=args.depth,
                odds=odds,
                odds_interval=args.odds_every or sports.odds_interval,
                scores=scores,
                scores_interval_live=sports.scores_interval_live,
                scores_interval_idle=sports.scores_interval_idle,
            )
            rec.run(max_ticks=args.ticks)
    except SchemaMismatch as exc:
        sys.exit(str(exc))
    finally:
        if odds is not None:
            odds.close()
        if scores is not None:
            scores.close()
    return 0


def cmd_record_stats(_: Settings, args: argparse.Namespace) -> int:
    """Summarise what the sports recorder has captured."""
    try:
        with SportsDataStore(args.db) as store:
            st = store.stats()
    except SchemaMismatch as exc:
        sys.exit(str(exc))
    print(f"db:          {args.db}")
    print(f"window:      {_fmt_ts(st['first_ts'])} .. {_fmt_ts(st['last_ts'])}")
    print(
        f"series:      {st['series']}   events: {st['events']}   markets: {st['markets']}"
        f"   settled: {st['settled']}"
    )
    print(
        f"snapshots:   {st['snapshots']}   (with book: {st['book_snapshots']}, "
        f"empty books: {st['empty_books']})"
    )
    print(f"events with exact start: {st['events_exact_start']} of {st['events']}")
    print(f"trades:      {st['trades']}")
    print(f"odds:        {st['odds']} quotes from {st['odds_books']} books  {st['odds_by_league']}")
    print(f"game states: {st['game_states']}  {st['states_by_league']}")
    if st["by_series"]:
        print("\nby series:")
        for row in st["by_series"]:
            print(
                f"  {row['series']:<20} {row['league'] or '?':<6} {row['kind'] or '?':<10} "
                f"markets={row['markets']:<4} settled={row['settled'] or 0:<4} "
                f"unparsed_teams={row['unparsed'] or 0}"
            )
    return 0


def cmd_record_dump(_: Settings, args: argparse.Namespace) -> int:
    """Print the most recent row of each table, raw, to check real API shapes."""
    try:
        with SportsDataStore(args.db) as store:
            rows = store.latest_rows()
    except SchemaMismatch as exc:
        sys.exit(str(exc))
    for table, row in rows.items():
        print(f"\n== {table}")
        if row is None:
            print("(empty)")
            continue
        for k, v in row.items():
            text = str(v)
            if len(text) > args.width:
                text = text[: args.width] + "..."
            print(f"  {k:<16} {text}")
    return 0


def cmd_odds_test(_: Settings, args: argparse.Namespace) -> int:
    """Fetch odds for one league once and show what came back (uses one API request)."""
    sports = SportsSettings.from_env()
    if not sports.has_odds:
        sys.exit("ODDS_API_KEY is not set (see .env.example)")
    lg = leagues.LEAGUES[args.league_key.lower()]
    feed = TheOddsApiFeed(sports.odds_api_key, regions=sports.odds_regions)
    try:
        quotes = feed.fetch(lg)
    finally:
        feed.close()
    games = {q.game_id: q for q in quotes}
    books = sorted({q.book for q in quotes})
    print(f"{lg.name}: {len(games)} games, {len(quotes)} quotes, books={books}")
    print(f"quota: remaining={feed.remaining} used={feed.used}")
    for q in list(games.values())[: args.show]:
        print(f"  {q.away} @ {q.home}  starts {_fmt_ts(q.commence_ts)}  id={q.game_id}")
    if args.raw and quotes:
        print(json.dumps(dataclasses.asdict(quotes[0]), default=str, indent=1))
    return 0


def cmd_scores_test(_: Settings, args: argparse.Namespace) -> int:
    """Fetch the ESPN scoreboard for one league once and show the games."""
    lg = leagues.LEAGUES[args.league_key.lower()]
    feed = EspnScoreFeed()
    try:
        states = feed.fetch(lg, date=args.date or today_eastern())
    finally:
        feed.close()
    print(f"{lg.name} {args.date or today_eastern()}: {len(states)} games")
    for s in states[: args.show]:
        print(
            f"  {s.away} ({s.away_abbr}) {s.away_score if s.away_score is not None else '-'} @ "
            f"{s.home} ({s.home_abbr}) {s.home_score if s.home_score is not None else '-'}  "
            f"{s.state} {s.detail or ''} id={s.game_id} start={_fmt_ts(s.start_ts)}"
        )
    if args.raw and states:
        print(json.dumps(states[0].raw, default=str, indent=1)[:3000])
    return 0


def cmd_devig(_: Settings, args: argparse.Namespace) -> int:
    """De-vig a set of American odds, e.g. `devig -150 +130`."""
    decs = [american_to_decimal(float(o)) for o in args.odds]
    print(f"decimal:     {[round(d, 4) for d in decs]}")
    print(f"overround:   {overround(decs) * 100:.2f}%")
    for name in METHODS:
        ps = devig(decs, name)
        print(f"{name:<14} {[round(p, 4) for p in ps]}")
    return 0


def cmd_compare(_: Settings, args: argparse.Namespace) -> int:
    """Kalshi moneyline prices against the de-vigged sportsbook consensus (report only)."""
    try:
        with SportsDataStore(args.db) as store:
            conn = sqlite3.connect(store.path) if store.path != ":memory:" else store._conn
            conn.row_factory = sqlite3.Row
            now = time.time()
            rows = compare(conn, now=now, league=args.league_key, method=args.method)
            print(format_table(rows[: args.show]))
            matched = sum(1 for r in rows if r.p_consensus is not None)
            print(f"\n{len(rows)} open moneyline markets, {matched} matched to odds")
            if args.games:
                print("\nconsensus by game:")
                for g in recent_games(conn, args.league_key, now - 3 * 3600)[: args.show]:
                    for c in consensus_for_game(conn, g["league"], g["game_id"], now=now):
                        probs = {k: round(v, 3) for k, v in c.probs.items()}
                        print(
                            f"  {g['away']} @ {g['home']} {c.market} {c.point} {probs} "
                            f"books={c.n_books} sharp={c.sharp_books} disp={c.dispersion:.3f}"
                        )
    except SchemaMismatch as exc:
        sys.exit(str(exc))
    return 0


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kalshi-sports", description=__doc__)
    p.add_argument("--env", choices=sorted(BASE_URLS), help="override KALSHI_ENV (default prod)")
    p.add_argument("--log-level", default=None)
    sub = p.add_subparsers(dest="command", required=True)

    def league_args(s: argparse.ArgumentParser) -> None:
        s.add_argument(
            "--league",
            action="append",
            choices=sorted(leagues.LEAGUES),
            help="league; repeatable (default: SPORTS_LEAGUES or all)",
        )
        s.add_argument("--series", action="append", help="Kalshi series ticker; repeatable")

    s = sub.add_parser("discover", help=cmd_discover.__doc__)
    league_args(s)
    s.add_argument("--list-series", action="store_true", help="also list GET /series")
    s.add_argument("--category", default="Sports", help="category filter for --list-series")
    s.add_argument("--events", action="store_true", help="also fetch events for start times")
    s.add_argument("--rules", action="store_true", help="print settlement rules text")
    s.add_argument("--show", type=int, default=5, help="markets to print per series")
    s.add_argument("--raw", action="store_true", help="print raw API JSON")
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("record", help=cmd_record.__doc__)
    league_args(s)
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--interval", type=float, default=5.0, help="seconds between ticks")
    s.add_argument("--fast", type=float, default=5.0, help="book cadence near/in game (s)")
    s.add_argument(
        "--book-window", type=float, default=24.0, help="hours before start to poll books/trades"
    )
    s.add_argument(
        "--discover-every",
        type=float,
        default=300.0,
        help="market list refresh (s); light snapshots",
    )
    s.add_argument("--odds-every", type=float, default=None, help="odds poll seconds per league")
    s.add_argument("--depth", type=int, default=10)
    s.add_argument("--ticks", type=int, default=None, help="stop after N ticks")
    s.add_argument("--no-odds", action="store_true")
    s.add_argument("--no-scores", action="store_true")
    s.set_defaults(func=cmd_record)

    s = sub.add_parser("record-stats", help=cmd_record_stats.__doc__)
    s.add_argument("--db", default=DEFAULT_DB)
    s.set_defaults(func=cmd_record_stats)

    s = sub.add_parser("record-dump", help=cmd_record_dump.__doc__)
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--width", type=int, default=600)
    s.set_defaults(func=cmd_record_dump)

    s = sub.add_parser("odds-test", help=cmd_odds_test.__doc__)
    s.add_argument("league_key", choices=sorted(leagues.LEAGUES))
    s.add_argument("--show", type=int, default=10)
    s.add_argument("--raw", action="store_true")
    s.set_defaults(func=cmd_odds_test)

    s = sub.add_parser("scores-test", help=cmd_scores_test.__doc__)
    s.add_argument("league_key", choices=sorted(leagues.LEAGUES))
    s.add_argument("--date", default=None, help="YYYY-MM-DD (default: today, Eastern)")
    s.add_argument("--show", type=int, default=20)
    s.add_argument("--raw", action="store_true")
    s.set_defaults(func=cmd_scores_test)

    s = sub.add_parser("devig", help=cmd_devig.__doc__)
    s.add_argument("odds", nargs="+", help="American odds, e.g. -150 +130")
    s.set_defaults(func=cmd_devig)

    s = sub.add_parser("compare", help=cmd_compare.__doc__)
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--league", dest="league_key", choices=sorted(leagues.LEAGUES), default=None)
    s.add_argument("--method", choices=sorted(METHODS), default="multiplicative")
    s.add_argument("--show", type=int, default=40)
    s.add_argument("--games", action="store_true", help="also print consensus per game")
    s.set_defaults(func=cmd_compare)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        parser.exit(2, f"config error: {exc}\n")
    env = args.env or "prod"  # sports markets are only listed on production
    if env != settings.env:
        settings = dataclasses.replace(settings, env=env)
    _setup_logging(args.log_level or settings.log_level)
    try:
        return args.func(settings, args)
    except KalshiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
