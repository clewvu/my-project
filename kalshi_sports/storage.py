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

SCHEMA_VERSION = 3

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
    away_abbr      TEXT,
    home_abbr      TEXT,
    start_ts       REAL,
    start_exact    INTEGER,
    start_source   TEXT,
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
    away_abbr        TEXT,
    home_abbr        TEXT,
    side             TEXT,
    side_team        TEXT,
    side_name        TEXT,
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

-- trading (paper and live share these; ``mode`` tells them apart)
CREATE TABLE IF NOT EXISTS limits (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY,
    ts            REAL NOT NULL,
    mode          TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    event_ticker  TEXT,
    league        TEXT,
    side_team     TEXT,
    action        TEXT NOT NULL,       -- buy_yes, buy_no, skip
    reason        TEXT,
    yes_bid       REAL,
    yes_ask       REAL,
    no_ask        REAL,
    p_consensus   REAL,
    n_books       INTEGER,
    sharp_books   INTEGER,
    dispersion    REAL,
    odds_age_s    REAL,
    secs_to_start REAL,
    edge          REAL,
    margin        REAL,
    dollars       REAL,
    contracts     INTEGER,
    order_id      TEXT,
    raw           TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS positions (
    id               INTEGER PRIMARY KEY,
    mode             TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    event_ticker     TEXT,
    league           TEXT,
    side             TEXT NOT NULL,     -- yes | no
    side_team        TEXT,
    contracts        INTEGER NOT NULL,
    price            REAL NOT NULL,     -- paid per contract on ``side``
    fee              REAL NOT NULL,
    dollars          REAL NOT NULL,     -- contracts * price + fee
    order_id         TEXT,
    opened_ts        REAL NOT NULL,
    start_ts         REAL,
    p_entry          REAL,              -- consensus probability of ``side`` at entry
    edge_entry       REAL,
    kalshi_close     REAL,              -- Kalshi mid for ``side`` at game start
    consensus_close  REAL,              -- consensus probability of ``side`` at game start
    clv_kalshi       REAL,
    clv_consensus    REAL,
    status           TEXT NOT NULL,     -- open | settled | void
    result           TEXT,
    net              REAL,
    closed_ts        REAL
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
"""

MIGRATIONS: dict[int, list[str]] = {
    3: [],  # new tables only; created by SCHEMA
    2: [
        "ALTER TABLE markets ADD COLUMN away_abbr TEXT",
        "ALTER TABLE markets ADD COLUMN home_abbr TEXT",
        "ALTER TABLE markets ADD COLUMN side_team TEXT",
        "ALTER TABLE markets ADD COLUMN side_name TEXT",
        "ALTER TABLE events ADD COLUMN away_abbr TEXT",
        "ALTER TABLE events ADD COLUMN home_abbr TEXT",
        "ALTER TABLE events ADD COLUMN start_exact INTEGER",
        "ALTER TABLE events ADD COLUMN start_source TEXT",
    ],
}


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
        # the recorder and the trader write the same file; wait out each other's locks
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
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
            if version >= 1:
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
                                    away_abbr, home_abbr, start_ts, start_exact, start_source,
                                    title, status, first_seen_ts, last_seen_ts, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_ticker) DO UPDATE SET
                    league = COALESCE(excluded.league, events.league),
                    game_date = COALESCE(excluded.game_date, events.game_date),
                    away = COALESCE(excluded.away, events.away),
                    home = COALESCE(excluded.home, events.home),
                    away_abbr = COALESCE(excluded.away_abbr, events.away_abbr),
                    home_abbr = COALESCE(excluded.home_abbr, events.home_abbr),
                    -- keep an exact start over an approximate one
                    start_ts = CASE WHEN events.start_exact = 1 THEN events.start_ts
                                    ELSE COALESCE(excluded.start_ts, events.start_ts) END,
                    start_exact = CASE WHEN events.start_exact = 1 THEN 1
                                       ELSE COALESCE(excluded.start_exact, events.start_exact) END,
                    start_source = CASE WHEN events.start_exact = 1 THEN events.start_source
                                        ELSE COALESCE(excluded.start_source,
                                                      events.start_source) END,
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
                    gm.away_abbr,
                    gm.home_abbr,
                    gm.start_ts,
                    1 if gm.start_exact else 0,
                    "kalshi" if gm.start_ts is not None else None,
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
                     away_abbr, home_abbr, side, side_team, side_name, line, title, subtitle,
                     rules, exchange_index, open_ts, close_ts, expiration_ts, status, result,
                     expiration_value, first_seen_ts, last_seen_ts, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    status = excluded.status,
                    result = COALESCE(excluded.result, markets.result),
                    expiration_value = COALESCE(excluded.expiration_value,
                                                markets.expiration_value),
                    close_ts = COALESCE(excluded.close_ts, markets.close_ts),
                    line = COALESCE(excluded.line, markets.line),
                    away = COALESCE(excluded.away, markets.away),
                    home = COALESCE(excluded.home, markets.home),
                    away_abbr = COALESCE(excluded.away_abbr, markets.away_abbr),
                    home_abbr = COALESCE(excluded.home_abbr, markets.home_abbr),
                    side_team = COALESCE(excluded.side_team, markets.side_team),
                    side_name = COALESCE(excluded.side_name, markets.side_name),
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
                    gm.away_abbr,
                    gm.home_abbr,
                    gm.side,
                    gm.side_team,
                    gm.side_name,
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

    def event_start(self, event_ticker: str | None) -> tuple[float | None, bool]:
        """(start_ts, exact) for an event; (None, False) if unknown."""
        if not event_ticker:
            return None, False
        with self._lock:
            row = self._conn.execute(
                "SELECT start_ts, start_exact FROM events WHERE event_ticker = ?", (event_ticker,)
            ).fetchone()
        if not row:
            return None, False
        return row["start_ts"], bool(row["start_exact"])

    def events_needing_start(self, league: str, since_date: str) -> list[sqlite3.Row]:
        """Events in a league on or after a date whose start is unknown or approximate."""
        with self._lock:
            return self._conn.execute(
                """
                SELECT event_ticker, game_date, away, home, away_abbr, home_abbr
                FROM events
                WHERE league = ? AND (start_exact IS NULL OR start_exact = 0)
                  AND game_date IS NOT NULL AND game_date >= ?
                """,
                (league, since_date),
            ).fetchall()

    def set_event_start(self, event_ticker: str, start_ts: float, source: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE events SET start_ts = ?, start_exact = 1, start_source = ?
                WHERE event_ticker = ?
                """,
                (start_ts, source, event_ticker),
            )

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

    # ------------------------------------------------------------ trading

    def limit_get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM limits WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def limit_set(self, key: str, value: Any) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO limits (key, value) VALUES (?, ?)", (key, str(value))
            )

    def insert_decision(self, row: dict[str, Any]) -> int:
        cols = [
            "ts",
            "mode",
            "ticker",
            "event_ticker",
            "league",
            "side_team",
            "action",
            "reason",
            "yes_bid",
            "yes_ask",
            "no_ask",
            "p_consensus",
            "n_books",
            "sharp_books",
            "dispersion",
            "odds_age_s",
            "secs_to_start",
            "edge",
            "margin",
            "dollars",
            "contracts",
            "order_id",
            "raw",
        ]
        values = [row.get(c) for c in cols]
        if isinstance(values[-1], dict | list):
            values[-1] = _json(values[-1])
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"INSERT INTO decisions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                values,
            )
            return int(cur.lastrowid)

    def open_position(self, row: dict[str, Any]) -> int:
        cols = [
            "mode",
            "ticker",
            "event_ticker",
            "league",
            "side",
            "side_team",
            "contracts",
            "price",
            "fee",
            "dollars",
            "order_id",
            "opened_ts",
            "start_ts",
            "p_entry",
            "edge_entry",
        ]
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"INSERT INTO positions ({', '.join(cols)}, status) "
                f"VALUES ({', '.join('?' * len(cols))}, 'open')",
                [row.get(c) for c in cols],
            )
            return int(cur.lastrowid)

    def positions(self, status: str | None = None, mode: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM positions", []
        conds = []
        if status:
            conds.append("status = ?")
            args.append(status)
        if mode:
            conds.append("mode = ?")
            args.append(mode)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        with self._lock:
            return self._conn.execute(sql + " ORDER BY opened_ts", args).fetchall()

    def event_exposure(self, event_ticker: str | None, mode: str) -> float:
        if not event_ticker:
            return 0.0
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(dollars), 0) FROM positions "
                "WHERE event_ticker = ? AND status = 'open' AND mode = ?",
                (event_ticker, mode),
            ).fetchone()
        return float(row[0])

    def set_close_marks(
        self, position_id: int, kalshi_close: float | None, consensus_close: float | None
    ) -> None:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT price, p_entry FROM positions WHERE id = ?", (position_id,)
            ).fetchone()
            if row is None:
                return
            clv_k = (kalshi_close - row["price"]) if kalshi_close is not None else None
            clv_c = (
                (consensus_close - row["p_entry"])
                if consensus_close is not None and row["p_entry"] is not None
                else None
            )
            self._conn.execute(
                "UPDATE positions SET kalshi_close = ?, consensus_close = ?, clv_kalshi = ?, "
                "clv_consensus = ? WHERE id = ?",
                (kalshi_close, consensus_close, clv_k, clv_c, position_id),
            )

    def settle_position(
        self, position_id: int, *, status: str, result: str | None, net: float, now: float
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE positions SET status = ?, result = ?, net = ?, closed_ts = ? WHERE id = ?",
                (status, result, net, now, position_id),
            )

    def market_result(self, ticker: str) -> tuple[str | None, str | None]:
        """(result, status) from the recorder's markets table."""
        with self._lock:
            row = self._conn.execute(
                "SELECT result, status FROM markets WHERE ticker = ?", (ticker,)
            ).fetchone()
        return (row["result"], row["status"]) if row else (None, None)

    def latest_snapshot(self, ticker: str, before_ts: float | None = None) -> sqlite3.Row | None:
        sql = "SELECT * FROM snapshots WHERE ticker = ?"
        args: list[Any] = [ticker]
        if before_ts is not None:
            sql += " AND ts <= ?"
            args.append(before_ts)
        with self._lock:
            return self._conn.execute(sql + " ORDER BY ts DESC LIMIT 1", args).fetchone()

    def candidate_markets(
        self, now: float, *, min_lead_s: float, max_lead_s: float, leagues: tuple[str, ...]
    ) -> list[sqlite3.Row]:
        """Open moneyline markets whose game starts within the lead window, with their
        latest quotes and the event's start time. Requires the recorder's data."""
        if not leagues:
            return []
        marks = ",".join("?" * len(leagues))
        with self._lock:
            return self._conn.execute(
                f"""
                SELECT m.ticker, m.event_ticker, m.league, m.game_date, m.away, m.home,
                       m.away_abbr, m.home_abbr, m.side_team, m.side_name, m.exchange_index,
                       e.start_ts, e.start_exact,
                       s.yes_bid, s.yes_ask, s.no_bid, s.no_ask, s.ts AS quote_ts
                FROM markets m
                JOIN events e ON e.event_ticker = m.event_ticker
                JOIN snapshots s ON s.id = (
                    SELECT id FROM snapshots WHERE ticker = m.ticker ORDER BY ts DESC LIMIT 1
                )
                WHERE m.kind = 'moneyline' AND m.result IS NULL AND m.league IN ({marks})
                  AND e.start_ts IS NOT NULL AND e.start_ts - ? BETWEEN ? AND ?
                ORDER BY e.start_ts
                """,
                (*leagues, now, min_lead_s, max_lead_s),
            ).fetchall()

    def trading_summary(self, mode: str) -> dict[str, Any]:
        with self._lock:
            c = self._conn
            open_rows = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(dollars), 0) FROM positions "
                "WHERE status='open' AND mode=?",
                (mode,),
            ).fetchone()
            settled = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(net), 0), COALESCE(SUM(net > 0), 0), "
                "AVG(clv_kalshi), AVG(clv_consensus), COALESCE(SUM(fee), 0) "
                "FROM positions WHERE status='settled' AND mode=?",
                (mode,),
            ).fetchone()
            decisions = c.execute(
                "SELECT COUNT(*), SUM(action != 'skip') FROM decisions WHERE mode=?", (mode,)
            ).fetchone()
        return {
            "open": int(open_rows[0]),
            "open_dollars": float(open_rows[1]),
            "settled": int(settled[0]),
            "net": float(settled[1]),
            "wins": int(settled[2]),
            "avg_clv_kalshi": settled[3],
            "avg_clv_consensus": settled[4],
            "fees": float(settled[5]),
            "decisions": int(decisions[0] or 0),
            "entries": int(decisions[1] or 0),
        }

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
                "book_snapshots": count(
                    "SELECT COUNT(*) FROM snapshots WHERE book_raw IS NOT NULL"
                ),
                "empty_books": count(
                    "SELECT COUNT(*) FROM snapshots "
                    "WHERE book_raw IS NOT NULL AND yes_levels IS NULL"
                ),
                "events_exact_start": count("SELECT COUNT(*) FROM events WHERE start_exact = 1"),
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
