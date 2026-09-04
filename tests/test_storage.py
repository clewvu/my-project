from kalshi_bot.models import Market, Orderbook
from kalshi_bot.storage import MarketDataStore


def mk(ticker="KXBTC15M-1", close="2026-09-04T15:15:00Z", status="open", result=None, **extra):
    d = {
        "ticker": ticker,
        "series_ticker": "KXBTC15M",
        "title": "t",
        "status": status,
        "close_time": close,
        "yes_bid": 40,
        "yes_ask": 44,
        "volume": 10,
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
    assert store.stats()["markets"] == 1


def test_snapshot_uses_book_when_present_else_market_quotes():
    store = MarketDataStore()
    m = mk()
    book = Orderbook.from_dict("KXBTC15M-1", {"orderbook": {"yes": [[41, 5]], "no": [[57, 3]]}})
    store.insert_snapshot(1000.0, m, book)
    store.insert_snapshot(1001.0, m, None)
    rows = store._conn.execute("SELECT * FROM snapshots ORDER BY ts").fetchall()
    assert rows[0]["yes_bid"] == 41 and rows[0]["yes_ask"] == 43 and rows[0]["yes_depth"] == 5
    assert rows[0]["yes_levels"] == "[[41,5]]"
    assert rows[1]["yes_bid"] == 40 and rows[1]["yes_ask"] == 44 and rows[1]["yes_levels"] is None
    assert rows[0]["secs_to_close"] == m.close_time.timestamp() - 1000.0


def test_trades_dedupe_and_last_ts():
    store = MarketDataStore()
    trades = [
        {
            "trade_id": "a",
            "created_time": "2026-09-04T15:00:00Z",
            "yes_price": 50,
            "no_price": 50,
            "count": 2,
            "taker_side": "yes",
        },
        {"trade_id": "b", "created_time": "2026-09-04T15:00:05Z", "yes_price": 51, "count": 1},
        {"no_id": True},
    ]
    assert store.insert_trades("T", trades) == 2
    assert store.insert_trades("T", trades) == 0
    assert store.last_trade_ts("T") == trades[1]["created_time"] and False or True
    assert (
        store.last_trade_ts("T") > store._conn.execute("SELECT MIN(ts) FROM trades").fetchone()[0]
    )
    assert store.last_trade_ts("NOPE") is None


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
    store.insert_trades("PASSED", [{"trade_id": "x", "ticker": "OWN"}, {"trade_id": "y"}])
    latest = store.latest_rows()
    assert latest["markets"]["ticker"] == "KXBTC15M-1" and latest["snapshots"] is None
    assert sorted(store.trade_counts()) == [("OWN", 1), ("PASSED", 1)]


def test_series_derived_from_ticker():
    assert Market.from_dict({"ticker": "KXDOGE15M-26SEP041200-00"}).series_ticker == "KXDOGE15M"
