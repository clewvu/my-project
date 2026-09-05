import json
import math
import random

from kalshi_bot import strategy as st
from kalshi_bot.models import Market

T0 = 1_800_000_000.0


def gbm(symbol, hist, seconds, sigma_sec, step=5.0, start=T0 - 3600, price=100.0, seed=1):
    rng = random.Random(seed)
    t = start
    while t < start + seconds:
        hist.push(symbol, t, price)
        price *= math.exp(rng.gauss(0, sigma_sec * math.sqrt(step)))
        t += step
    return price


def market(ttc=600.0, yes_ask=0.50, no_ask=0.52, strike=100.0, now=T0, series="KXBTC15M"):
    return Market.from_dict(
        {
            "ticker": f"{series}-1",
            "series_ticker": series,
            "status": "open",
            "floor_strike": strike,
            "close_time": now + ttc,
            "yes_ask_dollars": f"{yes_ask:.3f}",
            "no_ask_dollars": f"{no_ask:.3f}",
            "yes_bid_dollars": f"{yes_ask - 0.01:.3f}",
            "no_bid_dollars": f"{no_ask - 0.01:.3f}",
        }
    )


class FakeFeed:
    def __init__(self, prices):
        self.prices = prices
        self.calls = 0

    def fetch(self):
        self.calls += 1
        return dict(self.prices)


# ---------------------------------------------------------------- math and history


def test_scalar_math_matches_the_research_module():
    assert st.norm_cdf(0.0) == 0.5
    assert abs(st.norm_cdf(1.959964) - 0.975) < 1e-6
    assert st.effective_tau(900) == 860 and st.effective_tau(60) == 20 and st.effective_tau(0) == 1
    assert st.fair_value(100, 100, 1e-4, 300) == 0.5
    assert st.fair_value(101, 100, 1e-4, 300) > 0.9
    assert st.fair_value(101, 100, 0.0, 300) == 1.0 and st.fair_value(99, 100, 0.0, 300) == 0.0
    assert math.isnan(st.fair_value(0, 100, 1e-4, 300))


def test_spot_history_sigma_recovers_true_vol():
    hist = st.SpotHistory()
    gbm("BTC-USD", hist, 3600, 8e-5)
    sigma = hist.sigma("BTC-USD", 1800, T0)
    assert sigma is not None and abs(sigma - 8e-5) / 8e-5 < 0.2
    assert hist.sigma("DOGE-USD", 1800, T0) is None
    short = st.SpotHistory()
    gbm("BTC-USD", short, 300, 8e-5, start=T0 - 300)
    assert short.sigma("BTC-USD", 1800, T0) is None  # under half the window
    assert hist.latest("BTC-USD")[0] <= T0


def test_spot_history_ignores_out_of_order_and_trims():
    hist = st.SpotHistory(keep_s=100)
    hist.push("X", 10, 1.0)
    hist.push("X", 5, 2.0)  # older than the last point: ignored
    hist.push("X", 200, 3.0)
    assert hist.latest("X") == (200, 3.0)
    assert len(hist._points["X"]) == 1  # the point at t=10 fell out of the 100 s keep window


def test_bootstrap_from_recorder_db(tmp_path):
    from kalshi_bot.storage import MarketDataStore

    store = MarketDataStore(tmp_path / "md.sqlite")
    store.insert_spots(
        [(T0 - 100 + i, "coinbase_ws", "BTC-USD", 100.0 + i, None) for i in range(50)]
    )
    store.close()
    hist = st.SpotHistory()
    assert hist.bootstrap_from_db(tmp_path / "md.sqlite", "BTC-USD", T0 - 80) == 30
    assert hist.bootstrap_from_db(tmp_path / "missing.sqlite", "BTC-USD", 0) == 0
    assert hist.latest("BTC-USD") == (T0 - 51, 149.0)


# ---------------------------------------------------------------- strategies


def test_alternating_strategy():
    s = st.AlternatingStrategy(first_side="yes", max_price=0.60)
    m = market()
    sig = s.signal(m, None, T0)
    assert isinstance(sig, st.Signal) and sig.side == "yes" and sig.price == 0.50
    assert s.signal(m, "yes", T0).side == "no"
    assert isinstance(s.signal(market(yes_ask=0.80), None, T0), st.Skip)


def test_fairvalue_strategy_trades_only_with_edge():
    hist = st.SpotHistory()
    last = gbm("BTC-USD", hist, 3600, 8e-5)
    feed = FakeFeed({"BTC-USD": last})
    s = st.FairValueStrategy(feed, margin=0.02, history=hist, max_price=0.95)
    s.prepare(T0)
    assert feed.calls == 1 and hist.latest("BTC-USD")[0] == T0
    # strike far below spot: YES is nearly certain; a 50c ask is a big edge
    sig = s.signal(market(strike=last * 0.99, yes_ask=0.50, no_ask=0.52, now=T0), None, T0)
    assert isinstance(sig, st.Signal) and sig.side == "yes" and sig.edge > 0.3
    assert sig.inputs["p_yes"] > 0.95 and "sigma" in sig.inputs
    # strike far above spot: NO side
    sig = s.signal(market(strike=last * 1.01, yes_ask=0.50, no_ask=0.52, now=T0), None, T0)
    assert isinstance(sig, st.Signal) and sig.side == "no"
    # fairly priced market: skip
    out = s.signal(market(strike=last, yes_ask=0.51, no_ask=0.51, now=T0), None, T0)
    assert isinstance(out, st.Skip) and "below margin" in out.reason
    # price cap
    s.max_price = 0.40
    out = s.signal(market(strike=last * 0.99, yes_ask=0.50, no_ask=0.52, now=T0), None, T0)
    assert isinstance(out, st.Skip) and "max_price" in out.reason


def test_fairvalue_exit_sells_when_the_market_overpays():
    hist = st.SpotHistory()
    last = gbm("BTC-USD", hist, 3600, 8e-5)
    s = st.FairValueStrategy(FakeFeed({}), history=hist, exit_margin=0.02)
    # we hold YES bought at 0.50; spot now sits well below the strike, so the
    # model values the position near zero, and a 0.30 bid is a gift
    m = market(strike=last * 1.01, yes_ask=0.32, no_ask=0.70, now=T0)
    ex = s.exit(m, "yes", 0.50, T0)
    assert isinstance(ex, st.Exit) and ex.price == 0.31 and ex.inputs["sell_surplus"] > 0.02
    # position the model still values highly: hold
    m2 = market(strike=last * 0.99, yes_ask=0.90, no_ask=0.12, now=T0)
    assert s.exit(m2, "yes", 0.50, T0) is None
    # NO side symmetric: spot far above strike makes NO worthless, sell if bid is there
    ex2 = s.exit(m2, "no", 0.50, T0)
    assert isinstance(ex2, st.Exit) and ex2.price == 0.11
    # no quote / stale data: hold
    assert s.exit(market(strike=last, now=T0 + 100), "yes", 0.5, T0 + 100) is None


def test_alternating_exit_take_profit_and_stop_loss():
    s = st.AlternatingStrategy(take_profit=0.10, stop_loss=0.15)
    assert s.exit(market(yes_ask=0.66), "yes", 0.50, T0).reason.startswith("take profit")
    assert s.exit(market(yes_ask=0.60), "yes", 0.50, T0) is None
    assert s.exit(market(yes_ask=0.35), "yes", 0.50, T0).reason.startswith("stop loss")
    assert st.AlternatingStrategy().exit(market(yes_ask=0.90), "yes", 0.50, T0) is None


def test_params_file_reloads_live(tmp_path):
    from kalshi_bot.learn import Params

    hist = st.SpotHistory()
    last = gbm("BTC-USD", hist, 3600, 8e-5)
    path = tmp_path / "params.json"
    Params(margin=0.05, vol_window=3600, calib_a=0.2, calib_b=1.3, size_scale=0.5).save(path)
    clock = {"t": T0}
    s = st.FairValueStrategy(
        FakeFeed({"BTC-USD": last}),
        history=hist,
        params_path=path,
        params_reload_s=60,
        clock=lambda: clock["t"],
    )
    assert s.margin == 0.05 and s.vol_window_s == 3600 and s.size_scale == 0.5
    assert s.calib_a == 0.2 and s.calib_b == 1.3
    ev = s.evaluate(market(strike=last, now=T0), T0)
    assert "p_raw" in ev and ev["p_yes"] != ev["p_raw"]
    # halt flag makes every signal a skip, and clears when the file changes back
    Params(halt=True, note="drift z=-3.4").save(path)
    import os

    os.utime(path, (T0 + 100, T0 + 100))
    clock["t"] = T0 + 120
    s.prepare(clock["t"])
    assert (
        s.halt
        and "halted by the learning loop" in s.signal(market(now=T0 + 120), None, T0 + 120).reason
    )
    Params(halt=False).save(path)
    os.utime(path, (T0 + 200, T0 + 200))
    clock["t"] = T0 + 240
    s.prepare(clock["t"])
    assert not s.halt and s.margin == 0.02 and s.size_scale == 1.0
    # missing file: strategy keeps its constructor values
    s2 = st.FairValueStrategy(FakeFeed({}), history=hist, params_path=tmp_path / "none.json")
    assert s2.margin == 0.02 and s2.reload_params(force=True) is False


def test_fairvalue_strategy_guards():
    hist = st.SpotHistory()
    last = gbm("BTC-USD", hist, 3600, 8e-5)
    s = st.FairValueStrategy(FakeFeed({}), margin=0.0, history=hist, spot_stale_s=10)
    # stale spot
    out = s.signal(market(strike=last * 0.99, now=T0 + 60), None, T0 + 60)
    assert isinstance(out, st.Skip) and "old" in out.reason
    # unknown series -> no spot
    out = s.signal(market(strike=last, series="KXFOO15M", now=T0), None, T0)
    assert isinstance(out, st.Skip) and out.reason == "no spot"
    # too little history for sigma
    fresh = st.SpotHistory()
    fresh.push("BTC-USD", T0, 100.0)
    s2 = st.FairValueStrategy(FakeFeed({}), history=fresh)
    out = s2.signal(market(now=T0), None, T0)
    assert isinstance(out, st.Skip) and "volatility" in out.reason

    # a failing feed does not raise
    class Broken:
        def fetch(self):
            raise RuntimeError("down")

    st.FairValueStrategy(Broken(), history=hist).prepare(T0)


def test_decision_log_writes_trades_and_reason_changes(tmp_path):
    log = st.DecisionLog(tmp_path / "d.jsonl")
    m = market()
    skip = st.Skip("no spot", inputs={"ticker": "x", "spot": None})
    log.record(now=T0, strategy="fairvalue", series="KXBTC15M", market=m, outcome=skip)
    log.record(now=T0 + 5, strategy="fairvalue", series="KXBTC15M", market=m, outcome=skip)
    sig = st.Signal(side="yes", price=0.5, reason="edge", edge=0.05, inputs={"p_yes": 0.6})
    log.record(
        now=T0 + 10,
        strategy="fairvalue",
        series="KXBTC15M",
        market=m,
        outcome=sig,
        count=3,
        order_id="o1",
    )
    rows = [json.loads(line) for line in (tmp_path / "d.jsonl").read_text().splitlines()]
    assert [r["action"] for r in rows] == ["skip", "trade"]  # repeated skip reason not logged
    assert rows[1]["side"] == "yes" and rows[1]["count"] == 3 and rows[1]["inputs"]["p_yes"] == 0.6
    assert "ticker" not in rows[0]["inputs"]
    st.DecisionLog(None).record(now=T0, strategy="x", series="y", market=m, outcome=skip)


def test_build_strategy(tmp_path):
    assert st.build_strategy("alternate").name == "alternate"
    fv = st.build_strategy(
        "fairvalue", spot_feed=FakeFeed({}), series=("KXBTC15M",), spot_db=tmp_path / "none.sqlite"
    )
    assert fv.name == "fairvalue"
    try:
        st.build_strategy("magic")
    except ValueError as exc:
        assert "unknown strategy" in str(exc)
    else:
        raise AssertionError("expected ValueError")
