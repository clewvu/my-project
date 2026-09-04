import json

from kalshi_bot.spot_ws import (
    SOURCE,
    SpotWebSocket,
    Tick,
    TickBuffer,
    parse_ticks,
    subscribe_message,
)
from kalshi_bot.storage import MarketDataStore


def test_subscribe_messages():
    adv = json.loads(subscribe_message("advanced", ["BTC-USD"]))
    assert adv == {"type": "subscribe", "product_ids": ["BTC-USD"], "channel": "ticker"}
    ex = json.loads(subscribe_message("exchange", ["BTC-USD", "DOGE-USD"]))
    assert ex["channels"] == ["ticker"] and ex["product_ids"] == ["BTC-USD", "DOGE-USD"]


def test_parse_advanced_trade_message():
    msg = {
        "channel": "ticker",
        "timestamp": "2026-09-04T16:03:57.858606Z",
        "events": [
            {
                "type": "update",
                "tickers": [
                    {"type": "ticker", "product_id": "BTC-USD", "price": "79439.07"},
                    {"type": "ticker", "product_id": "DOGE-USD", "price": "0.2501"},
                ],
            }
        ],
    }
    ticks = parse_ticks(json.dumps(msg), local_ts=5.0)
    assert [(t.symbol, t.price) for t in ticks] == [("BTC-USD", 79439.07), ("DOGE-USD", 0.2501)]
    assert ticks[0].local_ts == 5.0 and abs(ticks[0].exchange_ts - 1788537837.858606) < 1e-3


def test_parse_exchange_feed_and_junk():
    msg = {
        "type": "ticker",
        "product_id": "BTC-USD",
        "price": "80000.5",
        "time": "2026-09-04T16:00:00.000000Z",
    }
    ticks = parse_ticks(json.dumps(msg))
    assert ticks[0].price == 80000.5 and ticks[0].exchange_ts is not None
    assert parse_ticks(json.dumps({"type": "subscriptions"})) == []
    assert parse_ticks(json.dumps({"channel": "heartbeats", "events": []})) == []
    assert parse_ticks("not json") == [] and parse_ticks(json.dumps([1, 2])) == []


def test_tick_buffer_rate_limits_and_keeps_latest():
    buf = TickBuffer(min_interval=0.2)
    buf.push(Tick("BTC-USD", 100.0, None, 10.0))  # first write
    buf.push(Tick("BTC-USD", 100.0, None, 10.05))  # unchanged: dropped
    buf.push(Tick("BTC-USD", 101.0, None, 10.1))  # changed but too soon: held
    buf.push(Tick("BTC-USD", 102.0, None, 10.3))  # changed and spaced: queued
    buf.push(Tick("BTC-USD", 103.0, None, 10.35))  # too soon: held as latest
    out = buf.drain()
    assert [(t.price, t.local_ts) for t in out] == [(100.0, 10.0), (102.0, 10.3), (103.0, 10.35)]
    assert buf.drain() == []  # latest already written
    assert abs(buf.age("BTC-USD", now=12.0) - 1.65) < 1e-9 and buf.age("NOPE") is None


def test_flush_writes_to_store():
    store = MarketDataStore()
    ws = SpotWebSocket(store, ["BTC-USD"], min_interval=0)
    ws.buffer.push(Tick("BTC-USD", 1.0, 0.5, 1.0))
    ws.buffer.push(Tick("BTC-USD", 2.0, 1.5, 2.0))
    assert ws.flush() == 2 and ws.flush() == 0
    rows = store._conn.execute("SELECT source, price, exchange_ts FROM spot ORDER BY ts").fetchall()
    assert [tuple(r) for r in rows] == [(SOURCE, 1.0, 0.5), (SOURCE, 2.0, 1.5)]
    assert ws.last_tick("BTC-USD").price == 2.0
    assert store.stats()["spot_by_source"] == {SOURCE: 2}


def test_bad_feed_rejected():
    import pytest

    with pytest.raises(ValueError):
        SpotWebSocket(MarketDataStore(), ["BTC-USD"], feed="nope")
