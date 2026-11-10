import math

from kalshi_bot.models import Market, Orderbook
from kalshi_bot.strategy import FairValueParams, FairValueStrategy, SpotHistory, norm_cdf


def test_norm_cdf():
    assert norm_cdf(0) == 0.5 and abs(norm_cdf(1.96) - 0.975) < 1e-3


def test_spot_history_vol():
    h = SpotHistory()
    # deterministic alternating returns of +/- 10 bps every 10 s -> per-second sigma = 1e-3/sqrt(10)
    p = 100.0
    for i in range(400):
        h.push("k", 1000 + 10 * i, p)
        p *= math.exp(0.001 if i % 2 == 0 else -0.001)
    sigma = h.realized_vol("k", window=1800, grid=10)
    assert abs(sigma - 0.001 / math.sqrt(10)) / (0.001 / math.sqrt(10)) < 0.05
    assert h.latest("k")[1] == p / math.exp(-0.001)  # last pushed price
    assert h.realized_vol("missing") is None
    h.push("k", 1000, 1.0)  # out-of-order ignored
    assert h.latest("k")[0] == 1000 + 10 * 399


def market(strike=100.0, close=2000.0, mid=0.5):
    m = Market.from_dict(
        {"ticker": "T", "floor_strike": strike, "status": "active", "close_time": close}
    )
    book = Orderbook.from_dict(
        "T",
        {
            "orderbook_fp": {
                "yes_dollars": [[f"{mid - 0.01:.3f}", "100"]],
                "no_dollars": [[f"{1 - mid - 0.01:.3f}", "100"]],
            }
        },
    )
    return m, book


def test_strategy_buys_yes_when_spot_far_above_strike():
    strat = FairValueStrategy(FairValueParams(margin=0.02, count=2, exec_mode="taker"))
    m, book = market(strike=100.0, close=2000.0, mid=0.5)  # asks 0.51 / 0.51
    # spot 0.5% above strike, low vol -> p_up near 1
    d = strat.decide(m, book, spot=100.5, spot_ts=1400.0, sigma=1e-4, now=1400.0)
    assert d.action == "order" and d.intent.side == "yes" and d.intent.price == 0.51
    assert d.features["fair_p"] > 0.9 and d.intent.edge > 0.3
    d = strat.decide(m, book, spot=99.5, spot_ts=1400.0, sigma=1e-4, now=1400.0)
    assert d.action == "order" and d.intent.side == "no"


def test_strategy_skips_when_fair_matches_market_or_data_bad():
    strat = FairValueStrategy(FairValueParams(margin=0.03))
    m, book = market(strike=100.0, close=2000.0, mid=0.5)
    d = strat.decide(m, book, spot=100.0, spot_ts=1400.0, sigma=1e-3, now=1400.0)
    assert d.action == "skip" and "below margin" in d.reason
    assert strat.decide(m, book, None, None, 1e-3, 1400.0).reason == "missing strike or spot"
    assert "stale" in strat.decide(m, book, 100.0, 1300.0, 1e-3, 1400.0).reason
    assert "volatility" in strat.decide(m, book, 100.0, 1400.0, None, 1400.0).reason
    m2, book2 = market(mid=0.95)  # yes ask 0.96 > max_price; no ask 0.06 ok but fair is ~0
    d = strat.decide(m2, book2, spot=100.5, spot_ts=1400.0, sigma=1e-4, now=1400.0)
    assert d.action == "skip"


def test_kelly_count():
    from kalshi_bot.strategy import kelly_count

    # fair 0.7 at price 0.5, no fee: f* = 0.4; quarter Kelly on $200 = $20 -> 40 contracts, capped
    assert kelly_count(0.7, 0.5, 0.0, 0.25, 200.0, cap=10) == 10
    assert kelly_count(0.7, 0.5, 0.0, 0.25, 200.0, cap=100) == 40
    assert kelly_count(0.52, 0.5, 0.0175, 0.25, 200.0, cap=10) == 0  # fee eats the edge
    assert kelly_count(0.7, 0.5, 0.0, 0.0, 200.0, cap=10) == 0


def test_maker_posts_inside_spread_without_fee():
    strat = FairValueStrategy(FairValueParams(margin=0.02, exec_mode="maker"))
    m, book = market(strike=100.0, close=2000.0, mid=0.5)  # yes bid 0.49 / ask 0.51 (2c spread)
    d = strat.decide(m, book, spot=100.5, spot_ts=1400.0, sigma=1e-4, now=1400.0)
    assert d.action == "order" and d.intent.exec_mode == "maker"
    assert d.intent.price == 0.50  # one tick inside, between bid and ask
    assert d.intent.edge > 0.4  # no fee deducted
    d_t = strat.decide(
        m, book, spot=100.5, spot_ts=1400.0, sigma=1e-4, now=1400.0, force_taker=True
    )
    assert d_t.intent.exec_mode == "taker" and d_t.intent.price == 0.51
    assert d_t.intent.edge < d.intent.edge


def test_spread_filter_and_kelly_sizing():
    strat = FairValueStrategy(FairValueParams(margin=0.02, max_spread=0.01))
    m, book = market(mid=0.5)  # 2c spread
    assert "spread" in strat.decide(m, book, 100.5, 1400.0, 1e-4, 1400.0).reason
    strat = FairValueStrategy(
        FairValueParams(
            margin=0.02, kelly_fraction=0.25, bankroll=200.0, max_kelly_count=6, exec_mode="taker"
        )
    )
    d = strat.decide(m, book, 100.5, 1400.0, 1e-4, 1400.0)
    assert d.action == "order" and d.intent.count == 6 and d.features["kelly_count"] == 6
