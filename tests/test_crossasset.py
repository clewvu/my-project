import math
import random

import numpy as np
import pytest

pd = pytest.importorskip("pandas")

from kalshi_bot import crossasset  # noqa: E402
from kalshi_bot.models import Market, Orderbook  # noqa: E402
from kalshi_bot.storage import MarketDataStore  # noqa: E402
from kalshi_bot.universe import Asset, Hypothesis, Universe  # noqa: E402

UNI = Universe(
    [
        Asset("KXAAA15M", "AAA", "metal", "Metal.AAA/USD"),
        Asset("KXBBB15M", "BBB", "metal", "Metal.BBB/USD"),
        Asset("KXCCC15M", "CCC", "metal", "Metal.CCC/USD"),
    ],
    [
        Hypothesis("aaa_leads_bbb", "KXAAA15M", "KXBBB15M", +1),
        Hypothesis("aaa_vs_ccc", "KXAAA15M", "KXCCC15M", -1),
        Hypothesis("bbb_leads_aaa", "KXBBB15M", "KXAAA15M", +1),
    ],
)


def build_db(path, n_windows=80, seed=3, lag_s=20.0):
    """AAA is one continuous random walk across windows; BBB follows AAA with a lag and
    its market only prices its own spot (so AAA's lead is unpriced); CCC moves opposite
    to AAA with no lag. Each window's strike is the price at its open."""
    rng = random.Random(seed)
    store = MarketDataStore(path)
    t0 = 1_700_000_000.0
    step_s = 5
    steps_per_window = 900 // step_s
    k = int(lag_s / step_s)
    total = n_windows * steps_per_window + 1
    a = [100.0]
    for _ in range(total - 1):
        a.append(a[-1] * (1 + rng.gauss(0, 0.0003)))
    b = [50.0 * (a[max(0, i - k)] / 100.0) for i in range(total)]
    c = [20.0 * (2 - a[i] / 100.0) for i in range(total)]
    paths = {"KXAAA15M": a, "KXBBB15M": b, "KXCCC15M": c}
    for w in range(n_windows):
        open_ts, close_ts = t0 + w * 900, t0 + w * 900 + 900
        lo, hi = w * steps_per_window, (w + 1) * steps_per_window
        for series, px in paths.items():
            strike = px[lo]
            ticker = f"{series}-{w}"
            sym = UNI.by_series(series).pyth_symbol
            for i in range(lo, hi + 1):
                ts = t0 + step_s * i
                p = px[i]
                dist = (p - strike) / strike * 1e4
                mid = min(0.97, max(0.03, round(1 / (1 + math.exp(-dist / 4)), 2)))
                m = Market.from_dict(
                    {
                        "ticker": ticker,
                        "status": "active",
                        "floor_strike": strike,
                        "open_time": open_ts,
                        "close_time": close_ts,
                        "yes_bid_dollars": f"{mid - 0.01:.3f}",
                        "yes_ask_dollars": f"{mid + 0.01:.3f}",
                        "no_bid_dollars": f"{1 - mid - 0.01:.3f}",
                        "no_ask_dollars": f"{1 - mid + 0.01:.3f}",
                    }
                )
                book = Orderbook.from_dict(
                    ticker,
                    {
                        "orderbook_fp": {
                            "yes_dollars": [[f"{mid - 0.01:.3f}", "100"]],
                            "no_dollars": [[f"{1 - mid - 0.01:.3f}", "100"]],
                        }
                    },
                )
                store.upsert_market(m, now=ts)
                store.insert_snapshot(ts, m, book)
                if series == "KXAAA15M" or i > lo:  # one spot row per (symbol, ts)
                    store.insert_spot(ts, "pyth", sym, p, exchange_ts=ts)
            result = "yes" if px[hi] >= strike else "no"
            store.mark_settled(
                Market.from_dict(
                    {
                        "ticker": ticker,
                        "status": "settled",
                        "result": result,
                        "close_time": close_ts,
                        "expiration_value": px[hi],
                    }
                ),
                now=close_ts + 30,
            )
    store.close()


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "x.sqlite"
    build_db(path)
    return crossasset.load(str(path), universe=UNI)


def test_load_shapes(data):
    assert set(data.outcomes.columns) == {"KXAAA15M", "KXBBB15M", "KXCCC15M"}
    assert len(data.outcomes) == 80 and data.outcomes.notna().all().all()
    assert not data.implied[300].empty and len(data.spot["KXAAA15M"]) > 1000


def test_outcome_comovement_and_signs(data):
    cm = crossasset.outcome_comovement(data)
    pair = cm[(cm["a"] == "KXAAA15M") & (cm["b"] == "KXCCC15M")].iloc[0]
    assert pair["agree"] < 0.1 and pair["phi"] < -0.8  # opposite by construction
    signs = crossasset.hypothesis_signs(data).set_index("hypothesis")
    assert signs.loc["aaa_vs_ccc", "verdict"] == "sign confirmed"
    assert signs.loc["aaa_leads_bbb", "verdict"] == "sign confirmed"


def test_spot_lead_lag_finds_the_lag(data):
    ll = crossasset.spot_lead_lag(data).set_index("hypothesis")
    row = ll.loc["aaa_leads_bbb"]
    assert row["best_lag_s"] == 20 and row["reads"] == "leader leads"
    rev = ll.loc["bbb_leads_aaa"]
    assert rev["best_lag_s"] == -20 and rev["reads"] == "follower leads"


def test_predictor_test_detects_unpriced_leader(data):
    pt = crossasset.predictor_test(data, 60)
    row = pt[(pt["hypothesis"] == "aaa_leads_bbb") & (pt["feature"] == "leader_ret_60s")].iloc[0]
    # AAA's recent return predicts BBB beyond BBB's own price (BBB lags AAA)
    assert row["beta"] > 0 and row["beta_lo"] > 0 and row["informative"]
    assert row["windows"] == 80
    assert set(pt["feature"]) >= {"leader_market", "leader_ret_open", "leader_ret_60s"}


def test_divergence_backtest_runs(data):
    bt = crossasset.divergence_backtest(data, 60, min_gap=0.05)
    assert set(bt["hypothesis"]) <= {h.name for h in UNI.hypotheses}
    row = bt[bt["hypothesis"] == "aaa_leads_bbb"].iloc[0]
    if row["trades"] >= 10:
        assert -1 <= row["net_per_contract"] <= 1


def test_helpers():
    pt, lo, hi = crossasset.bootstrap_mean(np.array([1.0, 0.0, 1.0, 1.0, 0.0, 1.0]), rounds=200)
    assert lo <= pt <= hi
    assert math.isnan(crossasset.bootstrap_mean(np.array([]))[0])
    assert crossasset.phi(np.array([1, 1, 0, 0]), np.array([1, 1, 0, 0])) == 1.0
    assert math.isnan(crossasset.phi(np.array([1, 1]), np.array([1, 0])))
    X = np.column_stack([np.linspace(-3, 3, 200)])
    y = (X[:, 0] + np.random.default_rng(0).normal(0, 0.5, 200) > 0).astype(float)
    beta = crossasset.logistic_fit(X, y)
    assert beta[1] > 1


def test_report_renders(data):
    text = crossasset.report(data)
    for section in (
        "== coverage",
        "== common windows",
        "== outcome co-movement",
        "== declared hypotheses",
        "== spot lead-lag",
        "== predictor test",
        "== divergence backtest",
    ):
        assert section in text
