import json
import sys
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")

from kalshi_bot import learn  # noqa: E402
from kalshi_bot.demo_loop import LoopState  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from test_fairvalue import in_memory  # noqa: E402

T0 = 1_800_000_000.0


# ---------------------------------------------------------------- calibration


def test_fit_calibration_recovers_known_shift():
    rng = np.random.default_rng(0)
    p = rng.uniform(0.05, 0.95, 4000)
    # truth: outcomes drawn from a sharpened, shifted version of p
    true = learn.apply_calibration(p, 0.4, 1.6)
    y = (rng.uniform(size=4000) < true).astype(float)
    a, b = learn.fit_calibration(p, y)
    assert abs(a - 0.4) < 0.15 and abs(b - 1.6) < 0.25
    assert learn.fit_calibration(p[:10], y[:10]) == (0.0, 1.0)  # too few
    assert learn.fit_calibration(p, np.ones_like(y)) == (0.0, 1.0)  # degenerate
    assert learn.apply_calibration(0.5, 0.0, 1.0) == 0.5
    assert learn.apply_calibration(0.5, 1.0, 1.0) > 0.7


def test_params_roundtrip(tmp_path):
    p = learn.Params(margin=0.03, calib_a=0.1, size_scale=0.5, halt=True, note="x")
    p.save(tmp_path / "params.json")
    q = learn.Params.load(tmp_path / "params.json")
    assert q == p
    assert learn.Params.load(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text("{nope")
    assert learn.Params.load(tmp_path / "bad.json") is None
    assert abs(q.calibrate(0.5) - learn.apply_calibration(0.5, 0.1, 1.0)) < 1e-12


# ---------------------------------------------------------------- retraining


@pytest.fixture(scope="module")
def stale_fv():
    return in_memory(400, 3, "stale")


@pytest.fixture(scope="module")
def efficient_fv():
    return in_memory(150, 5, "efficient")


def test_retrain_promotes_on_a_mispriced_book(stale_fv):
    promoted, record = learn.retrain(None, None, fv=stale_fv)
    assert record["candidate"]["passes_gate"], record
    assert promoted is not None and promoted.source == "retrain"
    assert promoted.vol_window in (1800.0, 3600.0) and promoted.margin >= 0
    assert promoted.halt is False and promoted.size_scale == 1.0
    assert record["outcome"] == "promoted"


def test_retrain_keeps_incumbent_when_candidate_is_not_better(stale_fv):
    promoted, record = learn.retrain(None, None, fv=stale_fv)
    assert promoted is not None
    # the incumbent is the same configuration, so the candidate cannot beat it
    again, record2 = learn.retrain(None, promoted, fv=stale_fv)
    assert again is None and "does not beat" in record2["outcome"]
    assert record2["incumbent_held_out"] is not None


def test_retrain_declines_on_an_efficient_book(efficient_fv):
    promoted, record = learn.retrain(None, None, fv=efficient_fv)
    assert promoted is None
    assert record["outcome"] != "promoted"


def test_retrain_needs_data(tmp_path):
    from kalshi_bot.storage import MarketDataStore

    MarketDataStore(tmp_path / "empty.sqlite").close()
    promoted, record = learn.retrain(tmp_path / "empty.sqlite", None)
    assert promoted is None and "too little data" in record["outcome"]


# ---------------------------------------------------------------- drift


def _live_files(tmp_path, n, win_rate, p_side=0.65):
    dec = tmp_path / "decisions.jsonl"
    state = tmp_path / "live.json"
    rng = np.random.default_rng(1)
    s = LoopState()
    with dec.open("w") as fh:
        for i in range(n):
            t = f"KXBTC15M-{i}"
            fh.write(
                json.dumps(
                    {
                        "action": "trade",
                        "strategy": "fairvalue",
                        "ticker": t,
                        "side": "yes",
                        "inputs": {"p_yes": p_side},
                    }
                )
                + "\n"
            )
            won = bool(rng.uniform() < win_rate)
            s.history.append({"ticker": t, "won": won, "net": 0.4 if won else -0.6})
    s.save(state)
    return dec, state


def test_drift_check_and_apply(tmp_path):
    dec, state = _live_files(tmp_path, 10, 0.65)
    assert learn.drift_check(learn.live_trades(dec, state))["status"] == "insufficient"

    dec, state = _live_files(tmp_path, 200, 0.65)
    ok = learn.drift_check(learn.live_trades(dec, state))
    assert ok["status"] == "ok" and abs(ok["z"]) < 2.5

    dec, state = _live_files(tmp_path, 200, 0.40)  # far below the model's 0.65
    bad = learn.drift_check(learn.live_trades(dec, state))
    assert bad["status"] == "halt" and bad["z"] < -3

    p = learn.Params()
    p = learn.apply_drift(p, bad)
    assert p.halt and p.size_scale == 0.5
    p = learn.apply_drift(p, {"status": "shrink", "z": -2.2})
    assert not p.halt and p.size_scale == 0.25  # floor
    p = learn.apply_drift(p, {"status": "ok"})
    assert not p.halt and p.size_scale == pytest.approx(0.375)
    p = learn.apply_drift(learn.Params(size_scale=0.9), {"status": "ok"})
    assert p.size_scale == 1.0


def test_live_trades_handles_missing_files(tmp_path):
    assert learn.live_trades(tmp_path / "a", tmp_path / "b").empty


# ---------------------------------------------------------------- full cycle


def test_run_cycle_writes_params_and_history(stale_fv, tmp_path):
    params_path = tmp_path / "params.json"
    hist_path = tmp_path / "history.jsonl"
    dec, state = _live_files(tmp_path, 200, 0.65)
    p = learn.run_cycle(
        db_path=None,
        params_path=params_path,
        history_path=hist_path,
        decisions_path=dec,
        live_state_path=state,
        fv=stale_fv,
    )
    assert params_path.exists() and p.source == "retrain" and not p.halt
    rows = [json.loads(line) for line in hist_path.read_text().splitlines()]
    assert rows[-1]["outcome"] == "promoted" and rows[-1]["drift"]["status"] == "ok"
    assert "active" in rows[-1]
    text = learn.describe(p, rows[-1])
    assert "promoted" in text and "live drift" in text
    # a second cycle keeps the incumbent and records why
    p2 = learn.run_cycle(db_path=None, params_path=params_path, history_path=hist_path, fv=stale_fv)
    assert p2.margin == p.margin and p2.source == "retrain"
    rows = [json.loads(line) for line in hist_path.read_text().splitlines()]
    assert len(rows) == 2 and "does not beat" in rows[-1]["outcome"]
