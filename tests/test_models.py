from datetime import UTC

from kalshi_bot.models import Balance, Candle, Market, Orderbook, Position


def test_market_parses_cents_and_times():
    m = Market.from_dict(
        {
            "ticker": "KXBTC15M-26SEP041500",
            "event_ticker": "KXBTC15M-26SEP041500",
            "series_ticker": "KXBTC15M",
            "title": "BTC up?",
            "status": "open",
            "yes_bid": 48,
            "yes_ask": 52,
            "no_bid": 48,
            "no_ask": 52,
            "last_price": 50,
            "volume": 1234,
            "close_time": "2026-09-04T15:15:00Z",
        }
    )
    assert m.yes_mid == 50 and m.spread == 4
    assert m.close_time.tzinfo == UTC
    assert m.close_time.hour == 15 and m.close_time.minute == 15


def test_orderbook_array_shape_and_derived_asks():
    book = Orderbook.from_dict("T", {"orderbook": {"yes": [[40, 10], [45, 5]], "no": [[50, 7]]}})
    assert [lv.price for lv in book.yes] == [45, 40]  # best first
    assert book.best_yes_bid == 45
    assert book.best_no_bid == 50
    assert book.best_yes_ask == 50  # 100 - best NO bid
    assert book.best_no_ask == 55
    assert book.yes_mid == 47.5
    assert book.depth("yes") == 15 and book.depth("yes", max_levels=1) == 5


def test_orderbook_dict_shape_and_empty():
    book = Orderbook.from_dict(
        "T", {"orderbook": {"true": [{"price": 30, "count": 2}], "false": None}}
    )
    assert book.best_yes_bid == 30 and book.best_no_bid is None and book.best_yes_ask is None
    assert Orderbook.from_dict("T", {"orderbook": {}}).yes_mid is None


def test_balance_and_position():
    assert Balance.from_dict({"balance": 20000}).dollars == 200.0
    p = Position.from_dict({"ticker": "X", "position": -3, "total_cost": 150})
    assert p.side == "no"
    assert Position.from_dict({"ticker": "X", "position": 0}).side is None


def test_candle_nested_and_flat():
    nested = Candle.from_dict(
        {
            "start_ts": 1,
            "end_period_ts": 61,
            "price": {"open": 40, "high": 55, "low": 39, "close": 50},
            "volume": 3,
        }
    )
    flat = Candle.from_dict({"start_ts": 1, "end_ts": 61, "open": 40, "close": 50})
    assert nested.high == 55 and nested.end_ts == 61 and nested.volume == 3
    assert flat.open == 40 and flat.high is None
