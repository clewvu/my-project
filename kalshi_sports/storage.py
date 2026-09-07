"""SQLite storage for recorded sports data (separate file from the crypto database).

Tables
------
series      every Kalshi series we have looked at, with its raw record
events      one row per game (Kalshi event), with the league and teams we parsed
markets     one row per market, with the parsed kind/side/line and settlement
snapshots   top of book + depth per market at the recorder's cadence
trades      public trade prints (deduplicated by trade id)
odds        sportsbook quotes from the odds feed, one row per quote seen
game_state  live game state from the score feed (status, period, scores)

Timestamps are unix seconds. Prices are dollars. Odds are decimal.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_bot.models import Market, Orderbook, Trade

from .catalog import GameMarket

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS series (
    ticker         TEXT PRIMARY KEY,
    league         TEXT,
    kind           TEXT,
    title          TEXT,
    category       TEXT,
    first_seen_ts  REAL NOT NULL,
    last_seen_ts   REAL NOT NULL,
    open_markets   INTEGER,
    raw            TEXT
);

CREATE TABLE IF NOT EXISTS events (
    event_ticker   TEXT PRIMARY KEY,
    series_ticker  TEXT,
    league         TEXT,
    game_date      TEXT,
    away           TEXT,
    home           TEXT,
    start_ts       REAL,
    title          TEXT,
    status         TEXT,
    first_seen_ts  REAL NOT NULL,
    last_seen_ts   REAL NOT NULL,
    raw            TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_league_date ON events(league, game_date);

CREATE TABLE IF NOT EXISTS markets (
    ticker           TEXT PRIMARY KEY,
    event_ticker     TEXT,
    series_ticker    TEXT,
    league           TEXT,
    kind             TEXT,
    game_date        TEXT,
    away             TEXT,
    home             TEXT,
    side             TEXT,
    line             REAL,
    title            TEXT,
    subtitle         TEXT,
    rules            TEXT,
    exchange_index   INTEGER,
    open_ts          REAL,
    close_ts         REAL,
    expiration_ts    REAL,
    status           TEXT,
    result           TEXT,
    expiration_value REAL,
    first_seen_ts    REAL NOT NULL,
    last_seen_ts     REAL NOT NULL,
    settled_ts       REAL,
    raw              TEXT
);
CREATE INDEX IF NOT EXISTS idx_markets_event ON markets(event_ticker);
CREATE INDEX IF NOT EXISTS idx_markets_league_close ON markets(league, close_ts);

CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY,
    ts             REAL NOT NULL,
    ticker         TEXT NOT NULL,
    secs_to_start  REAL,
    secs_to_close  REAL,
    yes_bid        REAL,
    yes_ask        REAL,
    no_bid         REAL,
    no_ask         REAL,
    last_price     REAL,
    volume         REAL,
    open_interest  REAL,
    yes_depth      REAL,
    no_depth       REAL,
    yes_levels     TEXT,
    no_levels      TEXT,
    book_raw       TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ticker_ts ON snapshots(ticker, ts);

CREATE TABLE IF NOT EXISTS trades (
    trade_id    TEXT PRIMARY KEY,
    ticker      TEXT NOT NULL,
    ts          REAL,
    yes_price   REAL,
    no_price    REAL,
    count       REAL,
    taker_side  TEXT,
    raw         TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_ticker_ts ON trades(ticker, ts);

CREATE TABLE IF NOT EXISTS odds (
    id            INTEGER PRIMARY KEY,
    ts            REAL NOT NULL,
    source        TEXT NOT NULL,
    league        TEXT NOT NULL,
    game_id       TEXT NOT NULL,
    commence_ts   REAL,
    home          TEXT,
    away          TEXT,
    book          TEXT NOT NULL,
    market        TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    point         REAL,
    price_decimal REAL NOT NULL,
    book_ts       REAL
);
CREATE INDEX IF NOT EXISTS idx_odds_game_ts ON odds(league, game_id, ts);

CREATE TABLE IF NOT EXISTS game_state (
    id           INTEGER PRIMARY KEY,
    ts           REAL NOT NULL,
    source       TEXT NOT NULL,
    league       TEXT NOT NULL,
    game_id      TEXT NOT NULL,
    start_ts     REAL,
    home         TEXT,
    away         TEXT,
    state        TEXT,
    detail       TEXT,
    period       INTEGER,
    clock        TEXT,
    home_score   INTEGER,
    away_score   INTEGER,
    raw          TEXT
);
CREATE INDEX IF NOT EXISTS idx_state_game_ts ON game_state(league, game_id, ts);
"""

MIGRATIONS: dict[int, list[str]] = {}


class SchemaMismatch(Exception):
    pass


def _epoch(dt: datetime | None) -> float | None:
    return dt.timestamp() if dt else None


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


class SportsDataStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            row = None
            tables = {
                r[0]
                for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "meta" in tables:
                row = self._conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
            version = int(row[0]) if row else 0
            if version > SCHEMA_VERSION:
                raise SchemaMismatch(
                    f"{self.path} has schema version {version}, newer than this code "
                    f"({SCHEMA_VERSION}). Update kalshi-sports."
                )
            for target in range(version + 1, SCHEMA_VERSION + 1):
                for statement in MIGRATIONS.get(target, []):
                    self._conn.execute(statement)
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SportsDataStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ series / events

    def upsert_series(
        self,
        ticker: str,
        now: float,
        *,
        league: str | None,
        kind: str | None,
        title: str | None,
        category: str | None,
        open_markets: int | None,
        raw: dict[str, Any] | None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO series (ticker, league, kind, title, category, first_seen_ts,
                                    last_seen_ts, open_markets, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    league = COALESCE(excluded.league, series.league),
                    kind = COALESCE(excluded.kind, series.kind),
                    title = COALESCE(excluded.title, series.title),
                    category = COALESCE(excluded.category, series.category),
                    last_seen_ts = excluded.last_seen_ts,
                    open_markets = COALESCE(excluded.open_markets, series.open_markets),
                    raw = COALESCE(excluded.raw, series.raw)
                """,
                (
                    ticker,
                    league,
                    kind,
                    title,
                    category,
                    now,
                    now,
                    open_markets,
                    _json(raw) if raw is not None else None,
                ),
            )

    def upsert_event(self, gm: GameMarket, now: float, event: dict[str, Any] | None) -> None:
        if not gm.event_ticker:
            return
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO events (event_ticker, series_ticker, league, game_date, away, home,
                                    start_ts, title, status, first_seen_ts, last_seen_ts, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_ticker) DO UPDATE SET
                    league = COALESCE(excluded.league, events.league),
                    game_date = COALESCE(excluded.game_date, events.game_date),
                    away = COALESCE(excluded.away, events.away),
                    home = COALESCE(excluded.home, events.home),
                    start_ts = COALESCE(excluded.start_ts, events.start_ts),
                    title = COALESCE(excluded.title, events.title),
                    status = COALESCE(excluded.status, events.status),
                    last_seen_ts = excluded.last_seen_ts,
                    raw = COALESCE(excluded.raw, events.raw)
                """,
                (
                    gm.event_ticker,
                    gm.series_ticker,
                    gm.league,
                    gm.game_date,
                    gm.away,
                    gm.home,
                    gm.start_ts,
                    (event or {}).get("title") or None,
                    (event or {}).get("status") or None,
                    now,
                    now,
                    _json(event) if event is not None else None,
                ),
            )

    # ------------------------------------------------------------ markets

    def upsert_market(self, market: Market, gm: GameMarket, now: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO markets
                    (ticker, event_ticker, series_ticker, league, kind, game_date, away, home,
                     side, line, title, subtitle, rules, exchange_index, open_ts, close_ts,
                     expiration_ts, status, result, expiration_value, first_seen_ts,
                     last_seen_ts, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    status = excluded.status,
                    result = COALESCE(excluded.result, markets.result),
                    expiration_value = COALESCE(excluded.expiration_value,
                                                markets.expiration_value),
                    close_ts = COALESCE(excluded.close_ts, markets.close_ts),
                    line = COALESCE(excluded.line, markets.line),
                    away = COALESCE(excluded.away, markets.away),
                    home = COALESCE(excluded.home, markets.home),
                    rules = COALESCE(excluded.rules, markets.rules),
                    last_seen_ts = excluded.last_seen_ts,
                    raw = excluded.raw
                """,
                (
                    market.ticker,
                    market.event_ticker,
                    market.series_ticker,
                    gm.league,
                    gm.kind,
                    gm.game_date,
                    gm.away,
                    gm.home,
                    gm.side,
                    gm.line,
                    market.title,
                    gm.subtitle,
                    gm.rules,
                    gm.exchange_index,
                    _epoch(market.open_time),
                    _epoch(market.close_time),
                    _epoch(market.expiration_time),
                    market.status,
                    market.result,
                    market.expiration_value,
                    now,
                    now,
                    _json(market.raw),
                ),
            )

    def mark_settled(self, market: Market, now: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE markets SET status = ?, result = ?, expiration_value = ?, settled_ts = ?,
                                   last_seen_ts = ?, raw = ?
                WHERE ticker = ?
                """,
                (
                    market.status,
                    market.result,
                    market.expiration_value,
                    now,
                    now,
                    _json(market.raw),
                    market.ticker,
                ),
            )

    def pending_settlements(self, now: float, grace_seconds: float = 600.0) -> list[str]:
        """Tickers past close without a result. Sports settle minutes to hours after close."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT ticker FROM markets
                WHERE result IS NULL AND close_ts IS NOT NULL AND close_ts + ? <= ?
                  AND (settled_ts IS NULL)
                ORDER BY close_ts
                """,
                (grace_seconds, now),
            ).fetchall()
        return [r["ticker"] for r in rows]

    def event_start(self, event_ticker: str | None) -> float | None:
        if not event_ticker:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT start_ts FROM events WHERE event_ticker = ?", (event_ticker,)
            ).fetchone()
        return row["start_ts"] if row else None

    # ------------------------------------------------------------ snapshots / trades

    def insert_snapshot(
        self, now: float, market: Market, book: Orderbook | None, start_ts: float | None
    ) -> None:
        secs_close = market.seconds_to_close(datetime.fromtimestamp(now, tz=UTC))
        has_book = book is not None and not book.is_empty
        yes_levels = [(lv.price, lv.count) for lv in book.yes] if has_book else None
        no_levels = [(lv.price, lv.count) for lv in book.no] if has_book else None
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO snapshots
                    (ts, ticker, secs_to_start, secs_to_close, yes_bid, yes_ask, no_bid, no_ask,
                     last_price, volume, open_interest, yes_depth, no_depth, yes_levels,
                     no_levels, book_raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    market.ticker,
                    (start_ts - now) if start_ts else None,
                    secs_close,
                    book.best_yes_bid if has_book else market.yes_bid,
                    book.best_yes_ask if has_book else market.yes_ask,
                    book.best_no_bid if has_book else market.no_bid,
                    book.best_no_ask if has_book else market.no_ask,
                    market.last_price,
                    market.volume,
                    market.open_interest,
                    round(book.depth("yes"), 2) if has_book else None,
                    round(book.depth("no"), 2) if has_book else None,
                    _json(yes_levels) if yes_levels is not None else None,
                    _json(no_levels) if no_levels is not None else None,
                    _json(book.raw) if book is not None else None,
                ),
            )

    def last_snapshot_ts(self, ticker: str) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) AS ts FROM snapshots WHERE ticker = ?", (ticker,)
            ).fetchone()
        return row["ts"] if row and row["ts"] is not None else None

    def last_trade_ts(self, ticker: str) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) AS ts FROM trades WHERE ticker = ?", (ticker,)
            ).fetchone()
        return row["ts"] if row and row["ts"] is not None else None

    def insert_trades(self, ticker: str, trades: list[Trade]) -> int:
        rows = [
            (
                t.trade_id,
                t.ticker or ticker,
                _epoch(t.created_time),
                t.yes_price,
                t.no_price,
                t.count,
                t.taker_side,
                _json(t.raw),
            )
            for t in trades
            if t.trade_id
        ]
        if not rows:
            return 0
        with self._lock, self._conn:
            before = self._conn.total_changes
            self._conn.executemany(
                """
                INSERT OR IGNORE INTO trades
                    (trade_id, ticker, ts, yes_price, no_price, count, taker_side, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            return self._conn.total_changes - before

    # ------------------------------------------------------------ odds / state

    def insert_odds(self, rows: list[tuple[Any, ...]]) -> int:
        """Rows: (ts, source, league, game_id, commence_ts, home, away, book, market,
        outcome, point, price_decimal, book_ts)."""
        if not rows:
            return 0
        with self._lock, self._conn:
            self._conn.executemany(
                """
                INSERT INTO odds (ts, source, league, game_id, commence_ts, home, away, book,
                                  market, outcome, point, price_decimal, book_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def insert_game_states(self, rows: list[tuple[Any, ...]]) -> int:
        """Rows: (ts, source, league, game_id, start_ts, home, away, state, detail, period,
        clock, home_score, away_score, raw_json)."""
        if not rows:
            return 0
        with self._lock, self._conn:
            self._conn.executemany(
                """
                INSERT INTO game_state (ts, source, league, game_id, start_ts, home, away, state,
                                        detail, period, clock, home_score, away_score, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def last_game_state(self, league: str, game_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                """
                SELECT * FROM game_state WHERE league = ? AND game_id = ?
                ORDER BY ts DESC LIMIT 1
                """,
                (league, game_id),
            ).fetchone()

    # ------------------------------------------------------------ inspection

    def latest_rows(self) -> dict[str, dict[str, Any] | None]:
        out: dict[str, dict[str, Any] | None] = {}
        with self._lock:
            for table, order in (
                ("series", "last_seen_ts"),
                ("events", "last_seen_ts"),
                ("markets", "last_seen_ts"),
                ("snapshots", "ts"),
                ("trades", "ts"),
                ("odds", "ts"),
                ("game_state", "ts"),
            ):
                row = self._conn.execute(
                    f"SELECT * FROM {table} ORDER BY {order} DESC LIMIT 1"
                ).fetchone()
                out[table] = dict(row) if row else None
        return out

    def stats(self) -> dict[str, Any]:
        with self._lock:
            c = self._conn

            def count(sql: str, *args: Any) -> Any:
                return c.execute(sql, args).fetchone()[0]

            out: dict[str, Any] = {
                "series": count("SELECT COUNT(*) FROM series"),
                "events": count("SELECT COUNT(*) FROM events"),
                "markets": count("SELECT COUNT(*) FROM markets"),
                "settled": count("SELECT COUNT(*) FROM markets WHERE result IS NOT NULL"),
                "snapshots": count("SELECT COUNT(*) FROM snapshots"),
                "empty_books": count("SELECT COUNT(*) FROM snapshots WHERE yes_levels IS NULL"),
                "trades": count("SELECT COUNT(*) FROM trades"),
                "odds": count("SELECT COUNT(*) FROM odds"),
                "odds_books": count("SELECT COUNT(DISTINCT book) FROM odds"),
                "game_states": count("SELECT COUNT(*) FROM game_state"),
                "first_ts": count("SELECT MIN(ts) FROM snapshots"),
                "last_ts": count("SELECT MAX(ts) FROM snapshots"),
                "by_series": [
                    dict(r)
                    for r in c.execute(
                        """
                        SELECT series_ticker AS series, league, kind, COUNT(*) AS markets,
                               SUM(result IS NOT NULL) AS settled,
                               SUM(away IS NULL OR home IS NULL) AS unparsed
                        FROM markets GROUP BY series_ticker ORDER BY series_ticker
                        """
                    )
                ],
                "odds_by_league": {
                    r[0]: r[1]
                    for r in c.execute("SELECT league, COUNT(*) FROM odds GROUP BY league")
                },
                "states_by_league": {
                    r[0]: r[1]
                    for r in c.execute("SELECT league, COUNT(*) FROM game_state GROUP BY league")
                },
            }
        return out
