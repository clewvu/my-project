"""SQLite storage for recorded market data.

Tables
------
markets    one row per market ever seen; updated with status/result on settlement
snapshots  top-of-book + depth every tick, per open market
trades     public trade prints (deduplicated by trade id)
spot       external spot prices (e.g. Coinbase BTC-USD) for research

All timestamps are unix epoch seconds (REAL). Prices are integer cents.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Market, Orderbook

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    ticker          TEXT PRIMARY KEY,
    series_ticker   TEXT,
    event_ticker    TEXT,
    title           TEXT,
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
    yes_bid         INTEGER,
    yes_ask         INTEGER,
    no_bid          INTEGER,
    no_ask          INTEGER,
    last_price      INTEGER,
    volume          INTEGER,
    yes_depth       INTEGER,
    no_depth        INTEGER,
    yes_levels      TEXT,
    no_levels       TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ticker_ts ON snapshots(ticker, ts);

CREATE TABLE IF NOT EXISTS trades (
    trade_id        TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    ts              REAL,
    yes_price       INTEGER,
    no_price        INTEGER,
    count           INTEGER,
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
        with self._conn:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)

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
                    (ticker, series_ticker, event_ticker, title, open_ts, close_ts,
                     expiration_ts, status, result, first_seen_ts, last_seen_ts, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    status = excluded.status,
                    result = COALESCE(excluded.result, markets.result),
                    close_ts = COALESCE(excluded.close_ts, markets.close_ts),
                    last_seen_ts = excluded.last_seen_ts,
                    raw = excluded.raw
                """,
                (
                    market.ticker,
                    market.series_ticker,
                    market.event_ticker,
                    market.title,
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
        secs = (
            market.seconds_to_close(datetime.fromtimestamp(now, tz=UTC))
            if market.close_time
            else None
        )
        yes_levels = [(lv.price, lv.count) for lv in book.yes] if book else None
        no_levels = [(lv.price, lv.count) for lv in book.no] if book else None
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO snapshots
                    (ts, ticker, secs_to_close, yes_bid, yes_ask, no_bid, no_ask,
                     last_price, volume, yes_depth, no_depth, yes_levels, no_levels)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    market.ticker,
                    secs,
                    book.best_yes_bid if book else market.yes_bid,
                    book.best_yes_ask if book else market.yes_ask,
                    book.best_no_bid if book else market.no_bid,
                    book.best_no_ask if book else market.no_ask,
                    market.last_price,
                    market.volume,
                    book.depth("yes") if book else None,
                    book.depth("no") if book else None,
                    _json(yes_levels) if yes_levels is not None else None,
                    _json(no_levels) if no_levels is not None else None,
                ),
            )

    # ------------------------------------------------------------ trades

    def last_trade_ts(self, ticker: str) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) AS ts FROM trades WHERE ticker = ?", (ticker,)
            ).fetchone()
        return row["ts"] if row and row["ts"] is not None else None

    def insert_trades(self, ticker: str, trades: list[dict[str, Any]]) -> int:
        """Insert public trades; returns how many were new."""
        rows = []
        for t in trades:
            trade_id = t.get("trade_id") or t.get("id")
            if not trade_id:
                continue
            rows.append(
                (
                    str(trade_id),
                    ticker,
                    _parse_ts(t.get("created_time")),
                    _int_or_none(t.get("yes_price")),
                    _int_or_none(t.get("no_price")),
                    _int_or_none(t.get("count")),
                    t.get("taker_side"),
                    _json(t),
                )
            )
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


def _parse_ts(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None
