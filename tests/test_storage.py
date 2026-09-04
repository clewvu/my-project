from datetime import UTC, datetime

import pytest

from kalshi_bot.models import Market, Orderbook, Trade
from kalshi_bot.storage import SCHEMA_VERSION, MarketDataStore, SchemaMismatch


def mk(ticker="KXBTC15M-1", close="2026-09-04T15:15:00Z", status="open", result=None, **extra):
    d = {
        "ticker": ticker,
        "title": "t",
        "status": status,
        "close_time": close,
        "yes_bid_dollars": "0.40",
        "yes_ask_dollars": "0.44",
        "volume_fp": "10.5",
        "floor_strike": 79000.5,
        "strike_type": "greater_or_equal",
        "result": result,
        **extra,
    }
    return Market.from_dict(d)


def test_upsert_market_is_idempotent_and_keeps_first_seen():
    store = MarketDataStore()
    store.upsert_market(mk(), now=100.0)
    store.upsert_market(mk(status="closed"), now=200.0)
    row = store.get_market_row("KXBTC15M-1")
    assert row["first_seen_ts"] == 100.0 and row["last_seen_ts"] == 200.0
    assert row["status"] == "closed" and row["result"] is None
    assert row["series_ticker"] == "KXBTC15M" and row["strike"] == 79000.5
    assert store.stats()["markets"] == 1


def test_snapshot_uses_book_when_present_else_market_quotes():
    store = MarketDataStore()
    m = mk()
    book = Orderbook.from_dict("KXBTC15M-1", {"orderbook": {"yes": [[41, 5]], "no": [[57, 3]]}})
    empty = Orderbook.from_dict("KXBTC15M-1", {"orderbook": {"nope": 1}})
    store.insert_snapshot(1000.0, m, book)
    store.insert_snapshot(1001.0, m, None)
    store.insert_snapshot(1002.0, m, empty)
    rows = store._conn.execute("SELECT * FROM snapshots ORDER BY ts").fetchall()
    assert rows[0]["yes_bid"] == 0.41 and rows[0]["yes_ask"] == 0.43 and rows[0]["yes_depth"] == 5
    assert rows[0]["yes_levels"] == "[[0.41,5.0]]"
    assert (
        rows[1]["yes_bid"] == 0.40 and rows[1]["yes_ask"] == 0.44 and rows[1]["yes_levels"] is None
    )
    assert rows[1]["book_raw"] is None
    assert rows[2]["yes_bid"] == 0.40 and rows[2]["book_raw"] == '{"orderbook":{"nope":1}}'
    assert rows[0]["secs_to_close"] == m.close_time.timestamp() - 1000.0
    assert rows[0]["volume"] == 10.5
    assert store.stats()["empty_books"] == 2


def test_trades_dedupe_and_last_ts():
    store = MarketDataStore()
    trades = [
        Trade.from_dict(t)
        for t in [
            {
                "trade_id": "a",
                "ticker": "T",
                "created_time": "2026-09-04T15:00:00Z",
                "yes_price_dollars": "0.5",
                "no_price_dollars": "0.5",
                "count_fp": "2",
                "taker_side": "yes",
            },
            {"trade_id": "b", "created_time": "2026-09-04T15:00:05Z", "yes_price": 51, "count": 1},
            {"no_id": True},
        ]
    ]
    assert store.insert_trades("T", trades) == 2
    assert store.insert_trades("T", trades) == 0
    expected = datetime(2026, 9, 4, 15, 0, 5, tzinfo=UTC).timestamp()
    assert store.last_trade_ts("T") == expected
    assert store.last_trade_ts("NOPE") is None
    row = store._conn.execute("SELECT * FROM trades WHERE trade_id='b'").fetchone()
    assert row["ticker"] == "T" and row["yes_price"] == 0.51 and row["count"] == 1.0


def test_pending_settlements_and_mark_settled():
    store = MarketDataStore()
    close = "2026-09-04T15:15:00Z"
    m = mk(close=close)
    close_ts = m.close_time.timestamp()
    store.upsert_market(m, now=close_ts - 600)
    assert store.pending_settlements(close_ts + 10) == []  # inside grace period
    assert store.pending_settlements(close_ts + 60) == ["KXBTC15M-1"]
    store.mark_settled(mk(close=close, status="settled", result="yes"), now=close_ts + 90)
    assert store.pending_settlements(close_ts + 120) == []
    st = store.stats()
    assert st["settled"] == 1 and st["by_series"][0]["yes_wins"] == 1


def test_spot_and_stats_span(tmp_path):
    with MarketDataStore(tmp_path / "x" / "md.sqlite") as store:
        store.insert_spot(1.0, "coinbase", "BTC-USD", 65000.5)
        store.insert_snapshot(5.0, mk(), None)
        store.insert_snapshot(9.0, mk(), None)
        st = store.stats()
    assert st["spot"] == 1 and st["first_ts"] == 5.0 and st["last_ts"] == 9.0
    assert (tmp_path / "x" / "md.sqlite").exists()


def test_latest_rows_and_trade_counts():
    store = MarketDataStore()
    assert all(v is None for v in store.latest_rows().values())
    store.upsert_market(mk(), now=1.0)
    store.insert_trades(
        "PASSED",
        [Trade.from_dict({"trade_id": "x", "ticker": "OWN"}), Trade.from_dict({"trade_id": "y"})],
    )
    latest = store.latest_rows()
    assert latest["markets"]["ticker"] == "KXBTC15M-1" and latest["snapshots"] is None
    assert sorted(store.trade_counts()) == [("OWN", 1), ("PASSED", 1)]


def test_schema_version_is_enforced(tmp_path):
    path = tmp_path / "md.sqlite"
    with MarketDataStore(path) as store:
        assert store._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[
            0
        ] == str(SCHEMA_VERSION)
    MarketDataStore(path).close()  # reopening the same version is fine

    import sqlite3

    legacy = tmp_path / "old.sqlite"
    con = sqlite3.connect(legacy)
    con.execute("CREATE TABLE snapshots (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    with pytest.raises(SchemaMismatch, match="older kalshi-bot"):
        MarketDataStore(legacy)


def test_migration_from_v2_adds_columns(tmp_path):
    import sqlite3

    path = tmp_path / "v2.sqlite"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('schema_version', '2');
        CREATE TABLE markets (ticker TEXT PRIMARY KEY, series_ticker TEXT, event_ticker TEXT,
            title TEXT, strike REAL, strike_type TEXT, open_ts REAL, close_ts REAL,
            expiration_ts REAL, status TEXT, result TEXT, first_seen_ts REAL NOT NULL,
            last_seen_ts REAL NOT NULL, settled_ts REAL, raw TEXT);
        CREATE TABLE spot (id INTEGER PRIMARY KEY, ts REAL NOT NULL, source TEXT NOT NULL,
            symbol TEXT NOT NULL, price REAL NOT NULL);
        INSERT INTO spot (ts, source, symbol, price) VALUES (1.0, 'coinbase', 'BTC-USD', 5.0);
    """)
    con.commit()
    con.close()
    with MarketDataStore(path) as store:
        version = store._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert version == str(SCHEMA_VERSION)
        cols = {r[1] for r in store._conn.execute("PRAGMA table_info(markets)")}
        assert "expiration_value" in cols
        assert store.stats()["spot"] == 1  # old rows survive
        store.insert_spot(2.0, "coinbase_ws", "BTC-USD", 6.0, exchange_ts=1.9)


def test_newer_schema_refused(tmp_path):
    path = tmp_path / "future.sqlite"
    with MarketDataStore(path) as store:
        store._conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        store._conn.commit()
    with pytest.raises(SchemaMismatch, match="newer"):
        MarketDataStore(path)


def test_expiration_value_and_pending_values():
    store = MarketDataStore()
    close = "2026-09-04T15:15:00Z"
    m = mk(close=close)
    close_ts = m.close_time.timestamp()
    store.upsert_market(m, now=close_ts - 600)
    # settled without a value yet
    store.mark_settled(mk(close=close, status="settled", result="no"), now=close_ts + 90)
    assert store.pending_values(close_ts + 100) == ["KXBTC15M-1"]
    assert store.pending_values(close_ts + 100 + 7200) == []  # gave up after an hour
    store.mark_settled(
        mk(close=close, status="settled", result="no", expiration_value="79,001.23"),
        now=close_ts + 150,
    )
    assert store.pending_values(close_ts + 200) == []
    row = store.get_market_row("KXBTC15M-1")
    assert row["expiration_value"] == 79001.23 and row["result"] == "no"
    assert store.stats()["with_value"] == 1
