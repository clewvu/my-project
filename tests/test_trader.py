from kalshi_bot.client import KalshiAPIError
from kalshi_bot.models import Balance, Market, Orderbook
from kalshi_bot.risk import RiskLimits
from kalshi_bot.spot_ws import Tick
from kalshi_bot.storage import MarketDataStore
from kalshi_bot.strategy import FairValueParams, FairValueStrategy
from kalshi_bot.trader import Trader
from kalshi_bot.universe import Asset, Universe

UNI = Universe([Asset("KXBTC15M", "BTC", "crypto", "Crypto.BTC/USD", "BTC-USD")])
CLOSE = 1_700_000_900.0


class FakeFeed:
    def __init__(self):
        self.buffer = type("B", (), {"latest": {}})()
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        pass


class FakeClient:
    dry_run = True
    allow_live = False

    def __init__(self, mid=0.5, result=None):
        self.mid = mid
        self.result = result
        self.calls = []

    def get_markets(self, *, series_ticker, status, max_pages):
        return [
            Market.from_dict(
                {
                    "ticker": "KXBTC15M-1",
                    "status": "active",
                    "floor_strike": 100.0,
                    "open_time": CLOSE - 900,
                    "close_time": CLOSE,
                    "result": self.result,
                }
            )
        ]

    def get_orderbook(self, ticker, depth):
        m = self.mid
        return Orderbook.from_dict(
            ticker,
            {
                "orderbook_fp": {
                    "yes_dollars": [[f"{m - 0.01:.3f}", "100"]],
                    "no_dollars": [[f"{1 - m - 0.01:.3f}", "100"]],
                }
            },
        )

    def get_market(self, ticker):
        self.calls.append(("market", ticker))
        return self.get_markets(series_ticker="", status="", max_pages=1)[0]


def make_trader(client, tmp_path, **limits):
    store = MarketDataStore(tmp_path / "t.sqlite")
    feed = FakeFeed()
    trader = Trader(
        client,
        store,
        UNI,
        FairValueStrategy(FairValueParams(margin=0.02, count=2, exec_mode="taker")),
        RiskLimits(**limits),
        mode="paper",
        spot_ws=feed,
    )
    return trader, store, feed


def seed_vol(feed, trader, now, spot):
    # feed history: 30 minutes of small moves, ending at `spot`
    key = trader._hist_key("coinbase_ws", "BTC-USD")
    p = spot
    for i in range(200, 0, -1):
        trader.history.push(key, now - 10 * i, p * (1 + 0.0001 * ((i % 2) * 2 - 1)))
    feed.buffer.latest["BTC-USD"] = Tick("BTC-USD", spot, now, now)


def test_paper_order_then_settlement(tmp_path):
    client = FakeClient(mid=0.5)
    trader, store, feed = make_trader(client, tmp_path, max_contracts_per_order=5)
    now = CLOSE - 600
    seed_vol(feed, trader, now, spot=100.3)  # well above strike -> buy YES at 0.51
    s = trader.tick(now)
    assert s.markets == 1 and s.orders == 1 and s.rejected == 0
    pos = store.positions("paper", "open")
    assert len(pos) == 1 and pos[0]["side"] == "yes" and pos[0]["price"] == 0.51
    assert store.decisions("paper")[0]["action"] == "order"
    # second tick: no duplicate position in the same market
    s = trader.tick(now + 5)
    assert s.orders == 0 and s.rejected == 1
    assert "already have" in store.decisions("paper")[0]["reason"]
    # settle after close with a YES result
    client.result = "yes"
    s = trader.tick(CLOSE + 60)
    assert s.settled == 1
    settled = store.positions("paper", "settled")[0]
    assert settled["result"] == "yes" and abs(settled["pnl"] - (2 * 0.49 - settled["fee"])) < 1e-9
    assert trader.risk.state.day_pnl > 0
    summary = store.pnl_summary("paper")
    assert summary["settled"] == 1 and summary["wins"] == 1


def test_no_entry_window_and_stale_spot(tmp_path):
    client = FakeClient(mid=0.5)
    trader, store, feed = make_trader(client, tmp_path)
    now = CLOSE - 60  # inside no-entry window
    seed_vol(feed, trader, now, spot=100.3)
    s = trader.tick(now)
    assert s.rejected == 1 and "no-entry" in store.decisions("paper")[0]["reason"]
    trader2, store2, feed2 = make_trader(FakeClient(mid=0.5), tmp_path / "b")
    now = CLOSE - 600
    seed_vol(feed2, trader2, now - 100, spot=100.3)  # spot 100 s old
    s = trader2.tick(now)
    assert s.skips == 1 and "stale" in store2.decisions("paper")[0]["reason"]


def test_live_mode_requires_unlocked_client(tmp_path):
    import pytest

    store = MarketDataStore(tmp_path / "l.sqlite")
    with pytest.raises(ValueError, match="live mode"):
        Trader(FakeClient(), store, UNI, FairValueStrategy(), RiskLimits(), mode="live")


def test_errors_do_not_crash_tick(tmp_path):
    class Broken(FakeClient):
        def get_orderbook(self, ticker, depth):
            raise KalshiAPIError(500, "GET", "/x", "boom")

    trader, store, feed = make_trader(Broken(), tmp_path)
    s = trader.tick(CLOSE - 600)
    assert s.errors == 1 and s.orders == 0


def test_trader_uses_active_config_and_tags_decisions(tmp_path):
    client = FakeClient(mid=0.5)
    store = MarketDataStore(tmp_path / "c.sqlite")
    cid = store.save_config("paper", {"margin": 0.5, "cal_b": 0.5}, source="learn", activate=True)
    feed = FakeFeed()
    trader = Trader(
        client,
        store,
        UNI,
        FairValueStrategy(FairValueParams(margin=0.02, exec_mode="taker")),
        RiskLimits(),
        mode="paper",
        spot_ws=feed,
    )
    assert trader.strategy.params.margin == 0.5 and trader.strategy.params.cal_b == 0.5
    now = CLOSE - 600
    seed_vol(feed, trader, now, spot=100.3)
    s = trader.tick(now)
    assert s.orders == 0 and s.skips == 1  # margin 0.5 is unreachable
    d = store.decisions("paper")[0]
    assert d["config_id"] == cid and "below margin" in d["reason"]


def test_settlement_fills_decision_results(tmp_path):
    client = FakeClient(mid=0.5)
    trader, store, feed = make_trader(client, tmp_path)
    now = CLOSE - 600
    seed_vol(feed, trader, now, spot=100.3)
    trader.tick(now)
    client.result = "no"
    trader.tick(CLOSE + 60)
    assert all(d["result"] == "no" for d in store.decisions("paper"))
    assert trader.risk.state.recent and trader.risk.state.recent[-1][1] > 0.5


def make_maker_trader(client, tmp_path, exec_mode="maker", **limits):
    store = MarketDataStore(tmp_path / "m.sqlite")
    feed = FakeFeed()
    params = FairValueParams(margin=0.02, count=2, exec_mode=exec_mode, rest_seconds=20)
    trader = Trader(
        client,
        store,
        UNI,
        FairValueStrategy(params),
        RiskLimits(**limits),
        mode="paper",
        spot_ws=feed,
    )
    return trader, store, feed


def test_paper_maker_fill_from_prints(tmp_path):
    from kalshi_bot.models import Trade

    client = FakeClient(mid=0.5)
    trader, store, feed = make_maker_trader(client, tmp_path)
    now = CLOSE - 600
    seed_vol(feed, trader, now, spot=100.3)
    s = trader.tick(now)
    assert s.orders == 1
    pos = store.positions("paper", "resting")[0]
    assert pos["price"] == 0.50 and pos["fee"] == 0.0
    # nothing printed yet: still resting, and no duplicate order
    s = trader.tick(now + 5)
    assert s.orders == 0 and store.positions("paper", "resting")
    # a print at our bid fills us
    store.insert_trades(
        "KXBTC15M-1",
        [
            Trade.from_dict(
                {
                    "trade_id": "t1",
                    "ticker": "KXBTC15M-1",
                    "created_time": now + 7,
                    "yes_price_dollars": "0.5000",
                    "no_price_dollars": "0.5000",
                    "count_fp": "5",
                }
            )
        ],
    )
    trader.tick(now + 10)
    opened = store.positions("paper", "open")
    assert len(opened) == 1 and opened[0]["count"] == 2 and opened[0]["fee"] == 0.0
    client.result = "yes"
    trader.tick(CLOSE + 60)
    settled = store.positions("paper", "settled")[0]
    assert abs(settled["pnl"] - 2 * 0.5) < 1e-9  # no fee on the maker side


def test_paper_maker_expires_then_hybrid_takes(tmp_path):
    client = FakeClient(mid=0.5)
    trader, store, feed = make_maker_trader(client, tmp_path, exec_mode="hybrid")
    now = CLOSE - 600
    seed_vol(feed, trader, now, spot=100.3)
    trader.tick(now)
    assert store.positions("paper", "resting")
    trader.tick(now + 25)  # expired, no prints
    assert store.positions("paper", "unfilled") and not store.positions("paper", "resting")
    assert "KXBTC15M-1" in trader._take_next
    feed.buffer.latest["BTC-USD"] = Tick("BTC-USD", 100.3, now + 25, now + 25)
    trader.history.push(trader._hist_key("coinbase_ws", "BTC-USD"), now + 25, 100.3)
    s = trader.tick(now + 26)
    assert s.orders == 1
    pos = store.positions("paper", "open")[0]
    assert pos["price"] == 0.51 and pos["fee"] > 0  # took the ask, paid the fee
    assert "KXBTC15M-1" not in trader._take_next


def test_edge_gate_shadows_live_orders(tmp_path):
    client = FakeClient(mid=0.5)
    store = MarketDataStore(tmp_path / "g.sqlite")
    feed = FakeFeed()
    params = FairValueParams(margin=0.02, count=2, exec_mode="taker")
    trader = Trader(
        client,
        store,
        UNI,
        FairValueStrategy(params),
        RiskLimits(),
        mode="paper",
        spot_ws=feed,
        require_edge=True,
    )
    trader.require_edge = True  # paper never gates; force the live behaviour for the test
    now = CLOSE - 600
    seed_vol(feed, trader, now, spot=100.3)
    s = trader.tick(now)
    assert s.orders == 0
    d = store.decisions("paper")[0]
    assert d["action"] == "shadow" and "edge gate closed" in d["reason"]
    store.set_meta({"learn:live:edge_ok": "1", "learn:live:edge_ok:reason": "test"})
    s = trader.tick(now + 2)
    assert s.orders == 1


class LiveClient(FakeClient):
    dry_run = False
    allow_live = True

    def __init__(self, bucket):
        super().__init__(mid=0.5)
        self.bucket = bucket
        self.orders = []
        self.balance_reads = 0

    def get_markets(self, *, series_ticker, status, max_pages):
        m = super().get_markets(series_ticker=series_ticker, status=status, max_pages=max_pages)[0]
        return [Market.from_dict({**m.raw, "exchange_index": 2})]

    def get_balance(self):
        self.balance_reads += 1
        return Balance.from_dict(
            {
                "balance_dollars": "111.2104",
                "balance_breakdown": [
                    {"balance": "110.6977", "exchange_index": 0},
                    {"balance": f"{self.bucket:.4f}", "exchange_index": 2},
                ],
            }
        )

    def create_order(self, ticker, **kwargs):
        self.orders.append((ticker, kwargs))
        return type("O", (), {"order_id": "oid-1"})()


def make_live_trader(client, tmp_path):
    store = MarketDataStore(tmp_path / "live.sqlite")
    feed = FakeFeed()
    trader = Trader(
        client,
        store,
        UNI,
        FairValueStrategy(FairValueParams(margin=0.02, count=2, exec_mode="taker")),
        RiskLimits(),
        mode="live",
        spot_ws=feed,
    )
    return trader, store, feed


def test_live_order_skipped_when_exchange_bucket_cannot_cover_it(tmp_path, caplog):
    # account total is $111.21 but only $0.50 sits in the bucket this market draws on
    client = LiveClient(bucket=0.5027)
    trader, store, feed = make_live_trader(client, tmp_path)
    now = CLOSE - 600
    seed_vol(feed, trader, now, spot=100.3)  # buy YES x2 @ 0.51: $1.02 + fee
    with caplog.at_level("WARNING"):
        s = trader.tick(now)
        s2 = trader.tick(now + 2)
    assert client.orders == []  # nothing sent to the exchange
    assert s.orders == 0 and s.rejected == 1 and s.errors == 0 and s2.rejected == 1
    d = store.decisions("live")[0]
    assert d["action"] == "rejected"
    assert "insufficient spendable balance" in d["reason"]
    assert "$0.50 available in exchange bucket 2" in d["reason"]
    assert "account total $111.21" in d["reason"]
    assert store.positions("live", "open") == []
    assert client.balance_reads == 1  # cached between ticks
    assert sum("NOT SENT" in r.message for r in caplog.records) == 1  # logged once per market


def test_live_order_sent_when_exchange_bucket_covers_it(tmp_path):
    client = LiveClient(bucket=50.0)
    trader, store, feed = make_live_trader(client, tmp_path)
    now = CLOSE - 600
    seed_vol(feed, trader, now, spot=100.3)
    s = trader.tick(now)
    assert s.orders == 1 and len(client.orders) == 1
    assert store.decisions("live")[0]["action"] == "order"


def test_live_order_sent_when_balance_cannot_be_read(tmp_path):
    class NoBalance(LiveClient):
        def get_balance(self):
            raise KalshiAPIError(503, "GET", "/portfolio/balance", "unavailable")

    client = NoBalance(bucket=0.0)
    trader, _, feed = make_live_trader(client, tmp_path)
    now = CLOSE - 600
    seed_vol(feed, trader, now, spot=100.3)
    assert trader.tick(now).orders == 1 and len(client.orders) == 1
