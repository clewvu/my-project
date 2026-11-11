import math
import random

import numpy as np
import pytest

pd = pytest.importorskip("pandas")

from kalshi_bot import learn  # noqa: E402
from kalshi_bot.models import Market  # noqa: E402
from kalshi_bot.storage import MarketDataStore  # noqa: E402
from kalshi_bot.strategy import FairValueParams, calibrate  # noqa: E402


def build(path, n=600, seed=1, overconfident=True):
    """Decision log where the model is systematically overconfident: true p is pulled
    toward 0.5. A calibrated candidate should beat the raw incumbent out of sample."""
    rng = random.Random(seed)
    store = MarketDataStore(path)
    t0 = 1_700_000_000.0
    for i in range(n):
        ticker = f"KXBTC15M-{i}"
        ts = t0 + i * 900 + 300
        p_model = min(0.97, max(0.03, rng.gauss(0.5, 0.25)))
        true_p = calibrate(p_model, 0.0, 0.5) if overconfident else p_model
        result = "yes" if rng.random() < true_p else "no"
        # market prices near the truth with a 2c spread
        yes_ask = min(0.99, max(0.01, round(true_p + 0.01 + rng.gauss(0, 0.02), 2)))
        no_ask = min(0.99, max(0.01, round(1 - true_p + 0.01 + rng.gauss(0, 0.02), 2)))
        store.upsert_market(
            Market.from_dict({"ticker": ticker, "status": "active", "close_time": ts + 600}), now=ts
        )
        store.insert_decision(
            {
                "ts": ts,
                "mode": "paper",
                "series": "KXBTC15M",
                "ticker": ticker,
                "secs_to_close": 600,
                "fair_p": p_model,
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "action": "skip",
                "reason": "synthetic",
                "features": {"fair_p_model": p_model, "fair_p": p_model},
            }
        )
        store.mark_settled(
            Market.from_dict(
                {"ticker": ticker, "status": "settled", "result": result, "close_time": ts + 600}
            ),
            now=ts + 700,
        )
    return store


def test_calibrate_identity_and_shrink():
    assert calibrate(0.8, 0.0, 1.0) == pytest.approx(0.8)
    assert 0.5 < calibrate(0.8, 0.0, 0.5) < 0.8
    assert calibrate(0.5, 0.0, 0.5) == pytest.approx(0.5)


def test_fit_calibration_recovers_shrinkage():
    rng = np.random.default_rng(0)
    p = np.clip(rng.normal(0.5, 0.25, 4000), 0.02, 0.98)
    true = np.array([calibrate(x, 0.0, 0.5) for x in p])
    y = (rng.random(4000) < true).astype(float)
    a, b = learn.fit_calibration(p, y)
    assert abs(a) < 0.15 and 0.4 < b < 0.6
    assert learn.fit_calibration(p[:10], y[:10]) == (0.0, 1.0)


def test_dataset_backfills_results_and_dedupes(tmp_path):
    store = build(tmp_path / "d.sqlite", n=50)
    df = learn.load_dataset(store, "paper")
    assert len(df) == 50 and df["result"].isin(["yes", "no"]).all()
    assert "fair_p_model" in df and df["y"].between(0, 1).all()
    assert learn.load_dataset(store, "live").empty


def test_replay_pnl_math():
    df = pd.DataFrame(
        {
            "fair_p_model": [0.9, 0.1, 0.5],
            "yes_ask": [0.6, 0.6, 0.5],
            "no_ask": [0.45, 0.45, 0.5],
            "result": ["yes", "no", "yes"],
        }
    )
    pnl = learn.replay(df, FairValueParams(margin=0.02))
    # row 0: buy yes at 0.6, wins 0.4 minus fee; row 1: buy no at 0.45, wins 0.55 minus fee
    assert pnl[0] == pytest.approx(0.4 - 0.07 * 0.6 * 0.4)
    assert pnl[1] == pytest.approx(0.55 - 0.07 * 0.45 * 0.55)
    assert pnl[2] == 0.0  # no edge


def test_evaluate_detects_shrinkage_and_is_conservative(tmp_path):
    store = build(tmp_path / "e.sqlite", n=800)
    df = learn.load_dataset(store, "paper")
    results, (a, b), n_test = learn.evaluate(df, FairValueParams(margin=0.03))
    assert results[0].label == "incumbent" and n_test == 240
    assert 0.3 < b < 0.75  # overconfidence detected on the training part
    assert len(results) == 1 + 7 * len(learn.MARGINS)
    assert all(r.n_test == 240 for r in results)
    # the incumbent's paired difference with itself is exactly zero
    assert results[0].diff_vs_incumbent == 0.0
    best, reason = learn.choose(results)
    # with a fairly priced synthetic market nothing should clear the gate
    assert best is None or (best.diff_lo > 0 and best.net_total > 0)
    assert isinstance(reason, str) and reason


def _res(label, trades=50, net=1.0, lo=0.01, hi=0.05, n_test=300):
    return learn.EvalResult(
        label,
        {"margin": 0.03},
        n_test,
        trades,
        0.6,
        net,
        net / n_test,
        net / max(trades, 1),
        (lo + hi) / 2,
        lo,
        hi,
    )


def test_gate_logic():
    inc = _res("incumbent", net=-1.0, lo=0.0, hi=0.0)
    good = _res("calibrated margin=0.03", net=3.0, lo=0.004, hi=0.02)
    noisy = _res("raw margin=0.02", net=5.0, lo=-0.01, hi=0.05)
    thin = _res("calibrated margin=0.08", net=1.0, lo=0.01, hi=0.05, trades=5)
    losing = _res("raw margin=0.06", net=-0.5, lo=0.001, hi=0.01)
    best, reason = learn.choose([inc, noisy, thin, losing, good])
    assert best is good and "calibrated margin=0.03" in reason
    assert learn.choose([inc, noisy, thin, losing])[0] is None
    small = [_res("incumbent", n_test=50), _res("x", n_test=50)]
    assert "held-out sample" in learn.choose(small)[1]


def test_gate_refuses_small_samples_and_noise(tmp_path):
    store = build(tmp_path / "s.sqlite", n=120)  # held-out 36 < MIN_TEST
    df = learn.load_dataset(store, "paper")
    results, _, _ = learn.evaluate(df, FairValueParams())
    best, reason = learn.choose(results)
    assert best is None and "held-out sample" in reason
    assert learn.choose([])[0] is None


def test_run_promote_and_rollback(tmp_path, monkeypatch):
    store = build(tmp_path / "r.sqlite", n=400)
    winner = _res("calibrated margin=0.03", net=3.0, lo=0.004, hi=0.02)
    winner.params = {"margin": 0.03, "cal_a": -0.05, "cal_b": 0.5}
    monkeypatch.setattr(learn, "choose", lambda results, min_test=100: (winner, "forced"))
    text = learn.run(store, "paper", promote=False)
    assert "== promotion gate" in text and "saved candidate config" in text
    assert store.active_config("paper") is None
    text = learn.run(store, "paper", promote=True)
    assert "ACTIVATED" in text
    active = store.active_config("paper")
    assert active is not None and active["source"] == "learn"
    assert active["params"]["cal_b"] == 0.5
    hist = store.config_history("paper")
    assert len(hist) == 2 and sum(c["active"] for c in hist) == 1
    msg = learn.rollback(store, "paper")
    assert "rolled back" in msg
    assert store.active_config("paper")["id"] == hist[0]["id"]
    msg = learn.rollback(store, "paper")
    assert "defaults" in msg and store.active_config("paper")["params"]["cal_b"] == 1.0


def test_empty_run(tmp_path):
    store = MarketDataStore(tmp_path / "x.sqlite")
    assert "nothing to learn" in learn.run(store, "paper")
    assert "no active" in learn.rollback(store, "paper")
    assert math.isnan(float("nan"))


def test_replay_favourites_and_sigma_scale():
    df = pd.DataFrame(
        {
            "fair_p_model": [0.9, 0.9],
            "z": [1.2816, 1.2816],
            "yes_ask": [0.4, 0.6],
            "no_ask": [0.62, 0.42],
            "result": ["yes", "yes"],
        }
    )
    base = learn.replay(df, FairValueParams(margin=0.02)).to_numpy()
    assert (base > 0).all()  # buys YES both rows
    fav = learn.replay(df, FairValueParams(margin=0.02, favourites_only=True)).to_numpy()
    assert fav[0] == 0.0 and fav[1] > 0  # 0.40 underdog skipped
    sharp = learn.replay(df, FairValueParams(margin=0.02, sigma_scale=0.5)).to_numpy()
    soft = learn.replay(df, FairValueParams(margin=0.02, sigma_scale=10.0)).to_numpy()
    assert (sharp > 0).all()  # Phi(2.56) ~ 0.995: both rows trade
    assert soft[0] > 0 and soft[1] == 0.0  # Phi(0.128) ~ 0.55: only the 0.40 ask clears


def test_edge_verdict_and_gate_flag(tmp_path):
    inc = _res("incumbent", trades=40, net=3.0, lo=0.0, hi=0.0)
    inc.trade_lo, inc.trade_hi = 0.01, 0.12
    assert learn.edge_verdict(inc)[0] is True
    inc.trade_lo = -0.01
    assert "not distinguishable" in learn.edge_verdict(inc)[1]
    inc.net_total = -1.0
    assert "not positive" in learn.edge_verdict(inc)[1]
    inc.trades = 10
    assert "held-out trades" in learn.edge_verdict(inc)[1]
    assert learn.edge_verdict(None) == (False, "no evaluation yet")
    store = build(tmp_path / "g.sqlite", n=400)
    learn.run(store, "paper")
    assert store.get_meta("learn:paper:edge_ok") in ("0", "1")
    assert store.get_meta("learn:paper:edge_ok:reason")
