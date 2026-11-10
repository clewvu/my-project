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
    strat = FairValueStrategy(FairValueParams(margin=0.02, count=2))
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


def test_dollar_sizing_buys_as_many_contracts_as_the_stake_covers():
    from kalshi_bot.models import Market, Orderbook

    market = Market.from_dict(
        {"ticker": "KXBTC15M-1", "status": "active", "floor_strike": 100.0, "close_time": 1900.0}
    )

    def book(yes_bid, no_bid):
        return Orderbook.from_dict(
            "KXBTC15M-1",
            {"orderbook_fp": {"yes_dollars": [[yes_bid, "100"]], "no_dollars": [[no_bid, "100"]]}},
        )

    strat = FairValueStrategy(FairValueParams(margin=0.02, dollars=5.0))
    # spot far above strike: fair ~1, YES ask = 1 - 0.60 = 0.40 -> floor(5 / 0.40) = 12
    d = strat.decide(market, book("0.380", "0.600"), 101.0, 1000.0, 1e-4, 1000.0)
    assert d.intent.side == "yes" and d.intent.count == 12
    assert d.intent.count * d.intent.price <= 5.0
    # YES ask 0.85 -> 5 contracts ($4.25)
    d = strat.decide(market, book("0.830", "0.150"), 101.0, 1000.0, 1e-4, 1000.0)
    assert d.intent.count == 5
