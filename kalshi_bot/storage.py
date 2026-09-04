"""SQLite storage for recorded market data.

Tables
------
markets    one row per market ever seen; updated with status/result on settlement
snapshots  top-of-book + depth every tick, per open market, plus the raw book JSON
trades     public trade prints (deduplicated by trade id)
spot       external spot prices (e.g. Coinbase BTC-USD) for research

Timestamps are unix epoch seconds (REAL). Prices are dollars (REAL, 0.001
resolution). Counts are contracts (REAL, may be fractional).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Market, Orderbook, Trade

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS markets (
    ticker          TEXT PRIMARY KEY,
    series_ticker   TEXT,
    event_ticker    TEXT,
    title           TEXT,
    strike          REAL,
    strike_type     TEXT,
    open_ts         REAL,
    close_ts        REAL,
    expiration_ts   REAL,
    status          TEXT,
    result          TEXT,
    first_seen_ts   REAL NOT NULL,
    last_seen_ts    REAL NOT NULL,
    settled_ts      REAL,
    raw             TEXT
);
CREATE INDEX IF NOT EXISTS idx_markets_series_close ON markets(series_ticker, close_ts);

CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY,
    ts              REAL NOT NULL,
    ticker          TEXT NOT NULL,
    secs_to_close   REAL,
    yes_bid         REAL,
    yes_ask         REAL,
    no_bid          REAL,
    no_ask          REAL,
    yes_bid_size    REAL,
    yes_ask_size    REAL,
    last_price      REAL,
    volume          REAL,
    open_interest   REAL,
    yes_depth       REAL,
    no_depth        REAL,
    yes_levels      TEXT,
    no_levels       TEXT,
    book_raw        TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ticker_ts ON snapshots(ticker, ts);

CREATE TABLE IF NOT EXISTS trades (
    trade_id        TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    ts              REAL,
    yes_price       REAL,
    no_price        REAL,
    count           REAL,
    taker_side      TEXT,
    raw             TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_ticker_ts ON trades(ticker, ts);

CREATE TABLE IF NOT EXISTS spot (
    id              INTEGER PRIMARY KEY,
    ts              REAL NOT NULL,
    source          TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    price           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_symbol_ts ON spot(symbol, ts);
"""


class SchemaMismatch(Exception):
    pass


def _epoch(dt: datetime | None) -> float | None:
    return dt.timestamp() if dt else None


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


class MarketDataStore:
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
            existing = {
                r[0]
                for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if existing and "meta" not in existing:
                raise SchemaMismatch(
                    f"{self.path} was created by an older kalshi-bot with integer-cent prices. "
                    "Delete it (or pass --db with a new path) and record again."
                )
            self._conn.executescript(SCHEMA)
            row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row[0]) != SCHEMA_VERSION:
                raise SchemaMismatch(
                    f"{self.path} has schema version {row[0]}, this code expects "
                    f"{SCHEMA_VERSION}. Delete it or pass --db with a new path."
                )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> MarketDataStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ markets

    def upsert_market(self, market: Market, now: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO markets
                    (ticker, series_ticker, event_ticker, title, strike, strike_type,
                     open_ts, close_ts, expiration_ts, status, result,
                     first_seen_ts, last_seen_ts, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    status = excluded.status,
                    result = COALESCE(excluded.result, markets.result),
                    strike = COALESCE(excluded.strike, markets.strike),
                    close_ts = COALESCE(excluded.close_ts, markets.close_ts),
                    last_seen_ts = excluded.last_seen_ts,
                    raw = excluded.raw
                """,
                (
                    market.ticker,
                    market.series_ticker,
                    market.event_ticker,
                    market.title,
                    market.strike,
                    market.strike_type,
                    _epoch(market.open_time),
                    _epoch(market.close_time),
                    _epoch(market.expiration_time),
                    market.status,
                    market.result,
                    now,
                    now,
                    _json(market.raw),
                ),
            )

    def mark_settled(self, market: Market, now: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE markets SET status = ?, result = ?, settled_ts = ?, last_seen_ts = ?, raw = ?
                WHERE ticker = ?
                """,
                (market.status, market.result, now, now, _json(market.raw), market.ticker),
            )

    def pending_settlements(self, now: float, grace_seconds: float = 30.0) -> list[str]:
        """Tickers whose close time has passed but whose result we have not stored."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT ticker FROM markets
                WHERE result IS NULL AND close_ts IS NOT NULL AND close_ts + ? <= ?
                ORDER BY close_ts
                """,
                (grace_seconds, now),
            ).fetchall()
        return [r["ticker"] for r in rows]

    def get_market_row(self, ticker: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM markets WHERE ticker = ?", (ticker,)
            ).fetchone()

    # ------------------------------------------------------------ snapshots

    def insert_snapshot(self, now: float, market: Market, book: Orderbook | None) -> None:
        secs = market.seconds_to_close(datetime.fromtimestamp(now, tz=UTC))
        has_book = book is not None and not book.is_empty
        yes_levels = [(lv.price, lv.count) for lv in book.yes] if has_book else None
        no_levels = [(lv.price, lv.count) for lv in book.no] if has_book else None
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO snapshots
                    (ts, ticker, secs_to_close, yes_bid, yes_ask, no_bid, no_ask,
                     yes_bid_size, yes_ask_size, last_price, volume, open_interest,
                     yes_depth, no_depth, yes_levels, no_levels, book_raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    market.ticker,
                    secs,
                    book.best_yes_bid if has_book else market.yes_bid,
                    book.best_yes_ask if has_book else market.yes_ask,
                    book.best_no_bid if has_book else market.no_bid,
                    book.best_no_ask if has_book else market.no_ask,
                    market.yes_bid_size,
                    market.yes_ask_size,
                    market.last_price,
                    market.volume,
                    market.open_interest,
                    book.depth("yes") if has_book else None,
                    book.depth("no") if has_book else None,
                    _json(yes_levels) if yes_levels is not None else None,
                    _json(no_levels) if no_levels is not None else None,
                    _json(book.raw) if book is not None else None,
                ),
            )

    # ------------------------------------------------------------ trades

    def last_trade_ts(self, ticker: str) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) AS ts FROM trades WHERE ticker = ?", (ticker,)
            ).fetchone()
        return row["ts"] if row and row["ts"] is not None else None

    def insert_trades(self, ticker: str, trades: list[Trade]) -> int:
        """Insert public trades; returns how many were new."""
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

    # ------------------------------------------------------------ spot

    def insert_spot(self, now: float, source: str, symbol: str, price: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO spot (ts, source, symbol, price) VALUES (?, ?, ?, ?)",
                (now, source, symbol, price),
            )

    # ------------------------------------------------------------ inspection

    def latest_rows(self) -> dict[str, dict[str, Any] | None]:
        """Most recent row from each table, for eyeballing real API shapes."""
        out: dict[str, dict[str, Any] | None] = {}
        with self._lock:
            for table, order in (
                ("markets", "last_seen_ts"),
                ("snapshots", "ts"),
                ("trades", "ts"),
                ("spot", "ts"),
            ):
                row = self._conn.execute(
                    f"SELECT * FROM {table} ORDER BY {order} DESC LIMIT 1"
                ).fetchone()
                out[table] = dict(row) if row else None
        return out

    def trade_counts(self, limit: int = 10) -> list[tuple[str, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ticker, COUNT(*) AS n FROM trades GROUP BY ticker ORDER BY n DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(r["ticker"], r["n"]) for r in rows]

    # ------------------------------------------------------------ stats

    def stats(self) -> dict[str, Any]:
        with self._lock:
            c = self._conn
            out: dict[str, Any] = {
                "markets": c.execute("SELECT COUNT(*) FROM markets").fetchone()[0],
                "settled": c.execute(
                    "SELECT COUNT(*) FROM markets WHERE result IS NOT NULL"
                ).fetchone()[0],
                "snapshots": c.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
                "empty_books": c.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE yes_levels IS NULL"
                ).fetchone()[0],
                "trades": c.execute("SELECT COUNT(*) FROM trades").fetchone()[0],
                "spot": c.execute("SELECT COUNT(*) FROM spot").fetchone()[0],
                "first_ts": c.execute("SELECT MIN(ts) FROM snapshots").fetchone()[0],
                "last_ts": c.execute("SELECT MAX(ts) FROM snapshots").fetchone()[0],
                "by_series": [],
            }
            for row in c.execute(
                """
                SELECT series_ticker AS series, COUNT(*) AS markets,
                       SUM(result IS NOT NULL) AS settled,
                       SUM(result = 'yes') AS yes_wins
                FROM markets GROUP BY series_ticker ORDER BY series_ticker
                """
            ):
                out["by_series"].append(dict(row))
        return out
