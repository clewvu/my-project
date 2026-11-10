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
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Market, Orderbook, Trade

SCHEMA_VERSION = 5

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
    expiration_value REAL,
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
    price           REAL NOT NULL,
    exchange_ts     REAL
);
CREATE INDEX IF NOT EXISTS idx_spot_symbol_ts ON spot(symbol, ts);

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY,
    ts              REAL NOT NULL,
    mode            TEXT NOT NULL,
    series          TEXT,
    ticker          TEXT,
    secs_to_close   REAL,
    spot            REAL,
    spot_age        REAL,
    strike          REAL,
    sigma           REAL,
    fair_p          REAL,
    yes_ask         REAL,
    no_ask          REAL,
    side            TEXT,
    count           INTEGER,
    price           REAL,
    edge            REAL,
    action          TEXT NOT NULL,
    reason          TEXT,
    features        TEXT,
    result          TEXT,
    config_id       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY,
    mode            TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    series          TEXT,
    side            TEXT NOT NULL,
    count           REAL NOT NULL,
    price           REAL NOT NULL,
    fee             REAL NOT NULL,
    opened_ts       REAL NOT NULL,
    close_ts        REAL,
    order_id        TEXT,
    client_order_id TEXT,
    status          TEXT NOT NULL,
    result          TEXT,
    pnl             REAL,
    settled_ts      REAL
);
CREATE INDEX IF NOT EXISTS idx_positions_mode_status ON positions(mode, status);

CREATE TABLE IF NOT EXISTS strategy_configs (
    id              INTEGER PRIMARY KEY,
    mode            TEXT NOT NULL,
    created_ts      REAL NOT NULL,
    params          TEXT NOT NULL,
    source          TEXT NOT NULL,
    report          TEXT,
    active          INTEGER NOT NULL DEFAULT 0
);
"""

# Applied in order to bring an older database up to SCHEMA_VERSION.
MIGRATIONS: dict[int, list[str]] = {
    5: [
        "ALTER TABLE decisions ADD COLUMN result TEXT",
        "ALTER TABLE decisions ADD COLUMN config_id INTEGER",
    ],
    4: [],  # new tables only; created by SCHEMA
    3: [
        "ALTER TABLE markets ADD COLUMN expiration_value REAL",
        "ALTER TABLE spot ADD COLUMN exchange_ts REAL",
    ],
}


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
            if "meta" in existing:
                row = self._conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
                version = int(row[0]) if row else 0
                if version > SCHEMA_VERSION:
                    raise SchemaMismatch(
                        f"{self.path} has schema version {version}, newer than this code "
                        f"({SCHEMA_VERSION}). Update kalshi-bot."
                    )
                self._conn.executescript(SCHEMA)  # creates any missing tables first
                for target in range(version + 1, SCHEMA_VERSION + 1):
                    for statement in MIGRATIONS.get(target, []):
                        try:
                            self._conn.execute(statement)
                        except sqlite3.OperationalError as exc:
                            if "duplicate column" not in str(exc):
                                raise
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
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
                     expiration_value, open_ts, close_ts, expiration_ts, status, result,
                     first_seen_ts, last_seen_ts, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    status = excluded.status,
                    result = COALESCE(excluded.result, markets.result),
                    strike = COALESCE(excluded.strike, markets.strike),
                    expiration_value = COALESCE(excluded.expiration_value,
                                                markets.expiration_value),
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
                    market.expiration_value,
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
                UPDATE markets
                SET status = ?, result = ?, expiration_value = ?, settled_ts = ?,
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

    def pending_values(self, now: float, max_age: float = 3600.0) -> list[str]:
        """Settled tickers still missing the settlement index value (retried for an hour)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT ticker FROM markets
                WHERE result IS NOT NULL AND expiration_value IS NULL
                  AND settled_ts IS NOT NULL AND settled_ts >= ?
                ORDER BY settled_ts
                """,
                (now - max_age,),
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
                    round(book.depth("yes"), 2) if has_book else None,
                    round(book.depth("no"), 2) if has_book else None,
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

    def insert_spot(
        self, now: float, source: str, symbol: str, price: float, exchange_ts: float | None = None
    ) -> None:
        self.insert_spots([(now, source, symbol, price, exchange_ts)])

    def insert_spots(self, rows: list[tuple[float, str, str, float, float | None]]) -> None:
        """Batch insert of (ts, source, symbol, price, exchange_ts) rows."""
        if not rows:
            return
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO spot (ts, source, symbol, price, exchange_ts) VALUES (?, ?, ?, ?, ?)",
                rows,
            )

    # ------------------------------------------------------------ meta / risk state

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, values: dict[str, str]) -> None:
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", list(values.items())
            )

    # ------------------------------------------------------------ decisions / positions

    def insert_decision(self, row: dict[str, Any]) -> None:
        cols = [
            "ts",
            "mode",
            "series",
            "ticker",
            "secs_to_close",
            "spot",
            "spot_age",
            "strike",
            "sigma",
            "fair_p",
            "yes_ask",
            "no_ask",
            "side",
            "count",
            "price",
            "edge",
            "action",
            "reason",
            "features",
            "result",
            "config_id",
        ]
        values = [row.get(c) for c in cols]
        fi = cols.index("features")
        if isinstance(values[fi], dict | list):
            values[fi] = _json(values[fi])
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO decisions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                values,
            )

    def open_position(self, row: dict[str, Any]) -> int:
        cols = [
            "mode",
            "ticker",
            "series",
            "side",
            "count",
            "price",
            "fee",
            "opened_ts",
            "close_ts",
            "order_id",
            "client_order_id",
            "status",
        ]
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"INSERT INTO positions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [row.get(c) for c in cols],
            )
            return int(cur.lastrowid)

    def positions(self, mode: str, status: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM positions WHERE mode = ?"
        args: list[Any] = [mode]
        if status:
            q += " AND status = ?"
            args.append(status)
        with self._lock:
            rows = self._conn.execute(q + " ORDER BY opened_ts", args).fetchall()
        return [dict(r) for r in rows]

    def update_position(self, position_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE positions SET {sets} WHERE id = ?", [*fields.values(), position_id]
            )

    def backfill_decision_results(self) -> int:
        """Copy settlement results onto decisions whose market has since settled."""
        with self._lock, self._conn:
            before = self._conn.total_changes
            self._conn.execute(
                """
                UPDATE decisions SET result = (
                    SELECT m.result FROM markets m WHERE m.ticker = decisions.ticker
                )
                WHERE result IS NULL AND ticker IN (
                    SELECT ticker FROM markets WHERE result IN ('yes', 'no')
                )
                """
            )
            return self._conn.total_changes - before

    def set_decision_results(self, ticker: str, result: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE decisions SET result = ? WHERE ticker = ? AND result IS NULL",
                (result, ticker),
            )

    def all_decisions(self, mode: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM decisions WHERE mode = ? ORDER BY ts", (mode,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ strategy configs

    def active_config(self, mode: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM strategy_configs WHERE mode = ? AND active = 1 "
                "ORDER BY created_ts DESC LIMIT 1",
                (mode,),
            ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["params"] = json.loads(out["params"])
        return out

    def config_history(self, mode: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM strategy_configs WHERE mode = ? ORDER BY created_ts", (mode,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["params"] = json.loads(d["params"])
            out.append(d)
        return out

    def save_config(
        self,
        mode: str,
        params: dict[str, Any],
        source: str,
        report: str = "",
        activate: bool = False,
        now: float | None = None,
    ) -> int:
        now = time.time() if now is None else now
        with self._lock, self._conn:
            if activate:
                self._conn.execute("UPDATE strategy_configs SET active = 0 WHERE mode = ?", (mode,))
            cur = self._conn.execute(
                "INSERT INTO strategy_configs (mode, created_ts, params, source, report, active) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (mode, now, _json(params), source, report, 1 if activate else 0),
            )
            return int(cur.lastrowid)

    def activate_config(self, mode: str, config_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE strategy_configs SET active = 0 WHERE mode = ?", (mode,))
            self._conn.execute(
                "UPDATE strategy_configs SET active = 1 WHERE id = ? AND mode = ?",
                (config_id, mode),
            )

    def recent_settled(self, mode: str, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM positions WHERE mode = ? AND status = 'settled' "
                "ORDER BY settled_ts DESC LIMIT ?",
                (mode, limit),
            ).fetchall()
        return [dict(r) for r in rows][::-1]

    def decisions(self, mode: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM decisions WHERE mode = ? ORDER BY ts DESC LIMIT ?", (mode, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    def pnl_summary(self, mode: str) -> dict[str, Any]:
        with self._lock:
            c = self._conn
            row = c.execute(
                """
                SELECT COUNT(*) AS n, SUM(pnl) AS pnl, SUM(pnl > 0) AS wins,
                       SUM(fee) AS fees, SUM(count * price) AS staked
                FROM positions WHERE mode = ? AND status = 'settled'
                """,
                (mode,),
            ).fetchone()
            open_n = c.execute(
                "SELECT COUNT(*) FROM positions WHERE mode = ? AND status = 'open'", (mode,)
            ).fetchone()[0]
            dec = c.execute(
                "SELECT action, COUNT(*) FROM decisions WHERE mode = ? GROUP BY action", (mode,)
            ).fetchall()
        return {
            "settled": row["n"] or 0,
            "pnl": row["pnl"] or 0.0,
            "wins": row["wins"] or 0,
            "fees": row["fees"] or 0.0,
            "staked": row["staked"] or 0.0,
            "open": open_n,
            "decisions": {r[0]: r[1] for r in dec},
        }

    def trades_after(self, ticker: str, since: float) -> list[dict[str, Any]]:
        """Public prints for a market after ``since`` (used to simulate paper maker fills)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, yes_price, no_price, count, taker_side FROM trades "
                "WHERE ticker = ? AND ts > ? ORDER BY ts",
                (ticker, since),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_spot(self, source: str, symbol: str, since: float) -> list[tuple[float, float]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, price FROM spot "
                "WHERE source = ? AND symbol = ? AND ts >= ? ORDER BY ts",
                (source, symbol, since),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

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
                "spot_by_source": {
                    r[0]: r[1]
                    for r in c.execute("SELECT source, COUNT(*) FROM spot GROUP BY source")
                },
                "with_value": c.execute(
                    "SELECT COUNT(*) FROM markets WHERE expiration_value IS NOT NULL"
                ).fetchone()[0],
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
