from datetime import UTC, datetime

import httpx

from kalshi_bot.client import KalshiAPIError
from kalshi_bot.commodities import RAW_SOURCE, REBASED_SOURCE, ProxyFeed, Rebaser, parse_quote
from kalshi_bot.models import Balance, Market, Orderbook
from kalshi_bot.risk import RiskLimits
from kalshi_bot.spot_ws import Tick
from kalshi_bot.storage import MarketDataStore
from kalshi_bot.strategy import FairValueParams, FairValueStrategy
from kalshi_bot.trader import Trader
from kalshi_bot.universe import Asset, Universe

OPEN = 1_700_000_000.0
CLOSE = OPEN + 900


def quote(bid, ask, ts):
    return [
        {"ts": int(ts * 1000), "spreadProfilePrices": [{"bid": bid - 1, "ask": ask + 1}]},
        {"ts": int(ts * 1000), "spreadProfilePrices": [{"bid": bid, "ask": ask}]},
    ]


def test_parse_quote_takes_tightest_spread_and_rejects_junk():
    assert parse_quote(quote(4000.0, 4001.0, 10.0)) == (4000.5, 10.0)
    assert parse_quote([]) is None
    assert parse_quote({"error": "x"}) is None
    assert parse_quote([{"spreadProfilePrices": [{"bid": 5, "ask": 4}]}]) is None


def test_rebaser_anchors_on_the_quote_at_the_open_only():
    r = Rebaser()
    r.push("XAU/USD", OPEN - 2, 4000.0)
    r.push("XAU/USD", OPEN + 30, 4004.0)
    # strike is the settlement feed's price at the open: 1% above the proxy
    ratio = r.observe("XAU/USD", OPEN, CLOSE, 4040.0, OPEN + 40)
    assert abs(ratio - 1.01) < 1e-9
    assert r.observe("XAU/USD", OPEN, CLOSE, 4040.0, OPEN + 500) == ratio  # held for the window
    assert r.current("XAU/USD", OPEN + 100) == ratio
    assert r.current("XAU/USD", CLOSE + 1) is None
    # next window: no quote near its open -> not anchored, the old ratio is not reused
    assert r.observe("XAU/USD", CLOSE, CLOSE + 900, 4050.0, CLOSE + 40) is None
    # a symbol first seen mid-window cannot be anchored either
    r.push("XAG/USD", OPEN + 300, 50.0)
    assert r.observe("XAG/USD", OPEN, CLOSE, 51.0, OPEN + 301) is None


def test_rebaser_ratio_survives_a_restart(tmp_path):
    store = MarketDataStore(tmp_path / "r.sqlite")
    r = Rebaser(store)
    r.push("XAU/USD", OPEN, 4000.0)
    r.observe("XAU/USD", OPEN, CLOSE, 4040.0, OPEN + 5)
    again = Rebaser(store)
    assert abs(again.observe("XAU/USD", OPEN, CLOSE, 4040.0, OPEN + 700) - 1.01) < 1e-9


def test_proxy_feed_stores_raw_and_rebased_and_drops_stale_quotes(tmp_path):
    store = MarketDataStore(tmp_path / "p.sqlite")
    state = {"ts": OPEN}

    def handler(request):
        return httpx.Response(200, json=quote(4000.0, 4001.0, state["ts"]))

    feed = ProxyFeed(store, ["XAU/USD"], transport=httpx.MockTransport(handler))
    assert feed.poll_once(OPEN + 1) == 1  # raw only: nothing anchored yet
    assert feed.buffer.latest["XAU/USD"].price == 4000.5
    feed.rebaser.observe("XAU/USD", OPEN, CLOSE, 4040.5, OPEN + 2)
    state["ts"] = OPEN + 3
    assert feed.poll_once(OPEN + 4) == 2
    assert len(store.recent_spot(RAW_SOURCE, "XAU/USD", since=0)) == 2
    rebased = store.recent_spot(REBASED_SOURCE, "XAU/USD", since=0)
    assert len(rebased) == 1 and abs(rebased[0][1] - 4040.5) < 1e-6
    # a quote that stopped updating (market closed) is not passed on as fresh
    assert feed.poll_once(OPEN + 100) == 0
    assert feed.buffer.latest["XAU/USD"].local_ts == OPEN + 4


UNI = Universe(
    [
        Asset("KXGOLD15M", "GOLD", "metal", "Metal.Index.1OZGOLD/USD", proxy_symbol="XAU/USD"),
        Asset("KXWTI15M", "WTI", "energy", "Commodities.Index.PYTHOIL/USD"),
    ]
)


class FakeProxy:
    symbols = ["XAU/USD"]

    def __init__(self):
        self.buffer = type("B", (), {"latest": {}})()
        self.rebaser = Rebaser()

    def start(self):
        pass

    def stop(self):
        pass


class FakeClient:
    dry_run = True
    allow_live = False

    def __init__(self):
        self.series_asked = []

    def get_markets(self, *, series_ticker, status, max_pages):
        self.series_asked.append(series_ticker)
        return [
            Market.from_dict(
                {
                    "ticker": f"{series_ticker}-1",
                    "status": "active",
                    "floor_strike": 4040.0,
                    "open_time": datetime.fromtimestamp(OPEN, UTC).isoformat(),
                    "close_time": datetime.fromtimestamp(CLOSE, UTC).isoformat(),
                }
            )
        ]

    def get_orderbook(self, ticker, depth):
        return Orderbook.from_dict(
            ticker,
            {"orderbook_fp": {"yes_dollars": [["0.490", "100"]], "no_dollars": [["0.490", "100"]]}},
        )


def make_trader(tmp_path, client, **kw):
    store = MarketDataStore(tmp_path / "t.sqlite")
    proxy = FakeProxy()
    trader = Trader(
        client,
        store,
        UNI,
        FairValueStrategy(FairValueParams(margin=0.02, count=2)),
        RiskLimits(max_contracts_per_order=5),
        mode=kw.pop("mode", "paper"),
        proxy=proxy,
        allow_proxy=True,
        **kw,
    )
    return trader, store, proxy


def feed_history(trader, proxy, start, end, last):
    """Raw proxy quotes every 10 s around 4000 (1% under the Pyth level), ending at ``last``."""
    key = trader._hist_key(RAW_SOURCE, "XAU/USD")
    t, i = start, 0
    while t < end:
        price = 4000.0 * (1 + 0.0001 * ((i % 2) * 2 - 1))
        trader.history.push(key, t, price)
        proxy.rebaser.push("XAU/USD", t, 4000.0)
        t, i = t + 10, i + 1
    proxy.buffer.latest["XAU/USD"] = Tick("XAU/USD", last, end, end)
    proxy.rebaser.push("XAU/USD", end, last)


def test_gold_is_priced_from_the_rebased_proxy_and_oil_is_left_alone(tmp_path):
    client = FakeClient()
    trader, store, proxy = make_trader(tmp_path, client)
    now = OPEN + 300
    # raw 4012 * 1.01 = 4052.1, well above the 4040 strike -> buy YES at 0.51
    feed_history(trader, proxy, OPEN - 600, now, last=4012.0)
    s = trader.tick(now)
    assert client.series_asked == ["KXGOLD15M"]  # no feed for WTI: no API calls spent on it
    assert s.markets == 1 and s.orders == 1
    d = store.decisions("paper")[0]
    assert d["action"] == "order" and d["side"] == "yes"
    assert abs(d["spot"] - 4012.0 * 1.01) < 1e-6
    assert "swissquote_rebased:XAU/USD" in str(d["features"])


def test_proxy_needs_extra_edge(tmp_path):
    trader, store, proxy = make_trader(tmp_path, FakeClient(), proxy_margin=0.9)
    now = OPEN + 300
    feed_history(trader, proxy, OPEN - 600, now, last=4012.0)
    s = trader.tick(now)
    assert s.orders == 0 and "below margin 0.92" in store.decisions("paper")[0]["reason"]


def test_unanchored_window_is_skipped_with_the_reason(tmp_path):
    trader, store, proxy = make_trader(tmp_path, FakeClient())
    now = OPEN + 300
    feed_history(trader, proxy, OPEN + 200, now, last=4012.0)  # bot started mid-window
    s = trader.tick(now)
    assert s.orders == 0 and s.skips == 1
    assert "not anchored" in store.decisions("paper")[0]["reason"]


def test_failed_order_records_why_and_backs_off(tmp_path):
    class LiveClient(FakeClient):
        dry_run = False
        allow_live = True
        orders = 0

        def get_balance(self):  # looks affordable locally; the exchange still refuses
            return Balance.from_dict({"balance_dollars": "500.0000"})

        def create_order(self, ticker, **kw):
            LiveClient.orders += 1
            raise KalshiAPIError(400, "POST", "/portfolio/events/orders", "insufficient_balance")

    trader, store, proxy = make_trader(tmp_path, LiveClient(), mode="live")
    now = OPEN + 300
    feed_history(trader, proxy, OPEN - 600, now, last=4012.0)
    s = trader.tick(now)
    d = store.decisions("live")[0]
    assert s.errors == 1 and d["action"] == "failed" and "insufficient_balance" in d["reason"]
    proxy.buffer.latest["XAU/USD"] = Tick("XAU/USD", 4012.0, now + 5, now + 5)
    s = trader.tick(now + 5)
    assert LiveClient.orders == 1 and s.rejected == 1  # not retried every tick
    assert "retry in" in store.decisions("live")[0]["reason"]
