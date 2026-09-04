from datetime import UTC

from kalshi_bot.models import Balance, Candle, Market, Orderbook, Position, Trade, dollars

PROD_MARKET = {
    "ticker": "KXBTC15M-26SEP041200-00",
    "event_ticker": "KXBTC15M-26SEP041200",
    "title": "BTC price up in next 15 mins?",
    "status": "active",
    "open_time": "2026-09-04T15:45:00Z",
    "close_time": "2026-09-04T16:00:00Z",
    "expiration_time": "2026-09-11T16:00:00Z",
    "floor_strike": 79529.72,
    "strike_type": "greater_or_equal",
    "yes_bid_dollars": "0.1700",
    "yes_ask_dollars": "0.1800",
    "no_bid_dollars": "0.8200",
    "no_ask_dollars": "0.8300",
    "yes_bid_size_fp": "4058.73",
    "yes_ask_size_fp": "7492.49",
    "last_price_dollars": "0.1800",
    "volume_fp": "1348352.50",
    "open_interest_fp": "418279.35",
    "result": "",
}


def test_dollars_helper():
    assert dollars("0.0910") == 0.091
    assert dollars("0.1700") == 0.17
    assert dollars(9) == 0.09  # legacy integer cents
    assert dollars(None) is None and dollars("") is None


def test_market_parses_production_shape():
    m = Market.from_dict(PROD_MARKET)
    assert m.series_ticker == "KXBTC15M"
    assert (m.yes_bid, m.yes_ask, m.no_bid, m.no_ask) == (0.17, 0.18, 0.82, 0.83)
    assert m.yes_mid == 0.175 and m.spread == 0.01
    assert m.yes_bid_size == 4058.73 and m.volume == 1348352.5
    assert m.strike == 79529.72 and m.strike_type == "greater_or_equal"
    assert m.result is None
    assert m.close_time.tzinfo == UTC and m.close_time.hour == 16
    assert m.seconds_to_close(m.open_time) == 900


def test_market_legacy_cents_shape():
    m = Market.from_dict({"ticker": "T", "yes_bid": 48, "yes_ask": 52, "volume": 1234})
    assert (m.yes_bid, m.yes_ask, m.volume) == (0.48, 0.52, 1234.0)
    assert m.strike is None


def test_orderbook_legacy_cents_arrays():
    book = Orderbook.from_dict("T", {"orderbook": {"yes": [[40, 10], [45, 5]], "no": [[50, 7]]}})
    assert [lv.price for lv in book.yes] == [0.45, 0.40]  # best first
    assert book.best_yes_bid == 0.45 and book.best_no_bid == 0.50
    assert book.best_yes_ask == 0.50 and book.best_no_ask == 0.55
    assert book.yes_mid == 0.475
    assert book.depth("yes") == 15 and book.depth("yes", max_levels=1) == 5


def test_orderbook_dollar_shapes():
    fp = {"orderbook_fp": {"yes": [["0.1700", "4058.73"]], "no": [["0.8200", "12.5"]]}}
    book = Orderbook.from_dict("T", fp)
    assert book.best_yes_bid == 0.17 and book.yes[0].count == 4058.73
    assert book.best_yes_ask == 0.18 and not book.is_empty

    dol = {
        "orderbook": {
            "yes_dollars": [["0.0910", 3]],
            "no_dollars": [{"price_dollars": "0.5", "count_fp": "1"}],
        }
    }
    book = Orderbook.from_dict("T", dol)
    assert book.best_yes_bid == 0.091 and book.best_no_bid == 0.5


def test_orderbook_empty_and_unknown_keeps_raw():
    book = Orderbook.from_dict("T", {"orderbook": {"weird": []}})
    assert book.is_empty and book.yes_mid is None and book.best_yes_ask is None
    assert book.raw == {"orderbook": {"weird": []}}


def test_trade_production_shape():
    t = Trade.from_dict(
        {
            "trade_id": "abc",
            "ticker": "KXDOGE15M-1",
            "count_fp": "1.00",
            "created_time": "2026-09-04T15:55:45.866854Z",
            "no_price_dollars": "0.9090",
            "yes_price_dollars": "0.0910",
            "taker_side": "no",
        }
    )
    assert t.yes_price == 0.091 and t.no_price == 0.909 and t.count == 1.0
    assert t.taker_side == "no" and t.created_time.microsecond == 866854


def test_balance_and_position():
    assert Balance.from_dict({"balance": 20000}).balance == 200.0
    assert Balance.from_dict({"balance_dollars": "200.5000"}).balance == 200.5
    p = Position.from_dict({"ticker": "X", "position_fp": "-3.5", "total_traded_dollars": "1.5"})
    assert p.side == "no" and p.position == -3.5 and p.total_cost == 1.5
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
    flat = Candle.from_dict({"start_ts": 1, "end_ts": 61, "open_dollars": "0.40", "close": 50})
    assert nested.high == 0.55 and nested.end_ts == 61 and nested.volume == 3
    assert flat.open == 0.40 and flat.high is None and flat.close == 0.5


def test_fill_reads_v2_shape():
    from kalshi_bot.models import Fill, Order

    f = Fill.from_dict(
        {
            "fill_id": "f1",
            "order_id": "o1",
            "ticker": "T",
            "outcome_side": "no",
            "book_side": "ask",
            "count_fp": "3.00",
            "yes_price_dollars": "0.550000",
            "no_price_dollars": "0.450000",
            "fee_cost": "0.02",
            "is_taker": True,
        }
    )
    assert f.side == "no" and f.price == 0.45 and f.count == 3 and f.fee == 0.02
    o = Order.from_dict(
        {
            "order_id": "o1",
            "ticker": "T",
            "outcome_side": "yes",
            "book_side": "bid",
            "status": "resting",
            "yes_price_dollars": "0.5000",
            "initial_count_fp": "2.00",
            "remaining_count_fp": "1.00",
        }
    )
    assert o.side == "yes" and o.count == 2 and o.remaining_count == 1 and o.price == 0.5
