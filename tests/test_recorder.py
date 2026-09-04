import httpx

from kalshi_bot.client import KalshiAPIError
from kalshi_bot.models import Market, Orderbook
from kalshi_bot.recorder import Recorder
from kalshi_bot.spot import SpotFeed
from kalshi_bot.storage import MarketDataStore


def market(ticker, series, close="2026-09-04T15:15:00Z", result=None, status="open"):
    return Market.from_dict(
        {
            "ticker": ticker,
            "series_ticker": series,
            "title": ticker,
            "status": status,
            "close_time": close,
            "result": result,
            "yes_bid": 45,
            "yes_ask": 55,
            "volume": 1,
        }
    )


class FakeClient:
    """Stands in for KalshiClient; records calls and can be told to fail."""

    def __init__(self):
        self.open = {
            "KXBTC15M": [market("BTC-1", "KXBTC15M")],
            "KXDOGE15M": [market("DOGE-1", "KXDOGE15M")],
        }
        self.settled = {}
        self.fail_orderbook_for = set()
        self.calls = []

    def get_markets(self, *, series_ticker, status, max_pages):
        self.calls.append(("markets", series_ticker))
        return list(self.open.get(series_ticker, []))

    def get_orderbook(self, ticker, depth):
        self.calls.append(("book", ticker))
        if ticker in self.fail_orderbook_for:
            raise KalshiAPIError(500, "GET", f"/markets/{ticker}/orderbook", "boom")
        return Orderbook.from_dict(ticker, {"orderbook": {"yes": [[45, 10]], "no": [[45, 10]]}})

    def get_trades(self, ticker, *, limit, min_ts):
        self.calls.append(("trades", ticker, min_ts))
        return [
            {
                "trade_id": f"{ticker}-t1",
                "created_time": "2026-09-04T15:05:00Z",
                "yes_price": 50,
                "no_price": 50,
                "count": 1,
                "taker_side": "yes",
            }
        ]

    def get_market(self, ticker):
        self.calls.append(("market", ticker))
        return self.settled.get(ticker) or market(ticker, "KXBTC15M")


def test_tick_records_everything_and_survives_errors():
    client, store = FakeClient(), MarketDataStore()
    client.fail_orderbook_for.add("DOGE-1")
    rec = Recorder(client, store, interval=1)
    res = rec.tick(now=1000.0)
    assert res.markets == 2 and res.snapshots == 2 and res.new_trades == 2
    assert len(res.errors) == 1 and "DOGE-1" in res.errors[0]
    # second tick: trades already stored, min_ts passed through, nothing new
    res2 = rec.tick(now=1005.0)
    assert res2.new_trades == 0
    assert any(c[0] == "trades" and c[2] is not None for c in client.calls[-4:])
    assert store.stats()["snapshots"] == 4


def test_settlement_is_checked_after_close_and_only_every_interval():
    client, store = FakeClient(), MarketDataStore()
    rec = Recorder(client, store, series=["KXBTC15M"], interval=1, settle_interval=60)
    close_ts = market("BTC-1", "KXBTC15M").close_time.timestamp()
    rec.tick(now=close_ts - 300)
    client.open["KXBTC15M"] = []  # market closed, disappears from open list
    client.settled["BTC-1"] = market("BTC-1", "KXBTC15M", status="settled", result="no")
    res = rec.tick(now=close_ts + 10)  # settle check runs (60s since last) but grace not elapsed
    assert res.settled == 0 and ("market", "BTC-1") not in client.calls
    res = rec.tick(now=close_ts + 40)  # < settle_interval since last check: skipped
    assert res.settled == 0 and ("market", "BTC-1") not in client.calls
    res = rec.tick(now=close_ts + 100)
    assert res.settled == 1
    assert store.get_market_row("BTC-1")["result"] == "no"
    assert store.pending_settlements(close_ts + 1000) == []


def test_spot_feed_parses_and_tolerates_failures():
    def handler(request):
        if "BTC-USD" in request.url.path:
            return httpx.Response(200, json={"data": {"amount": "65123.45", "currency": "USD"}})
        return httpx.Response(500)

    feed = SpotFeed(["BTC-USD", "DOGE-USD"], transport=httpx.MockTransport(handler))
    assert feed.fetch() == {"BTC-USD": 65123.45}

    client, store = FakeClient(), MarketDataStore()
    rec = Recorder(client, store, series=["KXBTC15M"], spot=feed)
    assert rec.tick(now=1.0).spot == 1
    assert store.stats()["spot"] == 1


def test_run_max_ticks_stops():
    client, store = FakeClient(), MarketDataStore()
    rec = Recorder(client, store, series=["KXBTC15M"], interval=1)
    rec.interval = 1.0
    rec._stop.wait = lambda t: None  # don't actually sleep
    assert rec.run(max_ticks=3, install_signals=False) == 3
    assert store.stats()["snapshots"] == 3
