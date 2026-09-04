import math
import random

import pytest

pd = pytest.importorskip("pandas")

from kalshi_bot import analysis  # noqa: E402
from kalshi_bot.models import Market, Orderbook  # noqa: E402
from kalshi_bot.storage import MarketDataStore  # noqa: E402


def build_db(path, n_markets=30, seed=1):
    """Synthetic 15-minute markets where the result follows the final spot and the
    book prices a logistic of spot distance with a 1c spread."""
    rng = random.Random(seed)
    store = MarketDataStore(path)
    t0 = 1_700_000_000.0
    for i in range(n_markets):
        series = "KXBTC15M" if i % 2 == 0 else "KXDOGE15M"
        symbol = analysis.SPOT_SYMBOLS[series]
        strike = 80_000.0 if series == "KXBTC15M" else 0.25
        open_ts = t0 + i * 900
        close_ts = open_ts + 900
        ticker = f"{series}-{i}"
        spot = strike
        final_spot = None
        for k in range(0, 31):  # every 30s from T-900 to T-0
            ts = open_ts + 30 * k
            if k <= 29:
                spot *= 1 + rng.gauss(0, 0.0004)
            final_spot = spot
            dist = (spot - strike) / strike * 1e4
            p = 1 / (1 + math.exp(-dist / 3))
            mid = min(0.99, max(0.01, round(p, 2)))
            m = Market.from_dict(
                {
                    "ticker": ticker,
                    "status": "active",
                    "floor_strike": strike,
                    "open_time": open_ts,
                    "close_time": close_ts,
                    "yes_bid_dollars": f"{mid - 0.005:.3f}",
                    "yes_ask_dollars": f"{mid + 0.005:.3f}",
                    "no_bid_dollars": f"{1 - mid - 0.005:.3f}",
                    "no_ask_dollars": f"{1 - mid + 0.005:.3f}",
                }
            )
            book = Orderbook.from_dict(
                ticker,
                {
                    "orderbook_fp": {
                        "yes_dollars": [[f"{mid - 0.005:.3f}", "100"]],
                        "no_dollars": [[f"{1 - mid - 0.005:.3f}", "100"]],
                    }
                },
            )
            store.upsert_market(m, now=ts)
            store.insert_snapshot(ts, m, book)
            store.insert_spot(ts, "coinbase", symbol, spot)
        result = "yes" if final_spot >= strike else "no"
        store.mark_settled(
            Market.from_dict(
                {"ticker": ticker, "status": "settled", "result": result, "close_time": close_ts}
            ),
            now=close_ts + 60,
        )
    store.close()


@pytest.fixture(scope="module")
def ds(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "md.sqlite"
    build_db(path)
    return analysis.load(str(path))


def test_load_joins_spot_and_outcomes(ds):
    assert len(ds.markets) == 30 and ds.markets["won_yes"].notna().all()
    assert ds.snapshots["spot"].notna().all()
    assert set(ds.snapshots["series_ticker"]) == {"KXBTC15M", "KXDOGE15M"}
    assert (ds.snapshots["yes_mid"].between(0, 1)).all()


def test_at_horizon_picks_latest_before_horizon(ds):
    df = analysis.at_horizon(ds.settled, 300)
    assert len(df) == 30
    assert (df["secs_to_close"] == 300).all()
    assert analysis.at_horizon(ds.settled, 5000).empty


def test_spot_signal_is_perfect_at_the_end(ds):
    sig = analysis.spot_signal(ds, 0)
    total = sig[sig["abs_dist_bps"] == "all"].iloc[0]
    assert total["n"] == 30 and total["spot_accuracy"] == 1.0
    earlier = analysis.spot_signal(ds, 600)
    assert earlier[earlier["abs_dist_bps"] == "all"].iloc[0]["spot_accuracy"] < 1.0


def test_calibration_and_brier(ds):
    cal = analysis.calibration(ds, 60)
    assert cal["n"].sum() == 30 and (cal["realised"].between(0, 1)).all()
    b = analysis.brier(ds, 0)
    assert b["n"] == 30 and b["spot_rule"] == 0.0 and 0 <= b["market"] <= 0.25
    assert analysis.brier(ds, 5000) == {}


def test_backtest_accounts_for_fees(ds):
    r = analysis.backtest(ds, 0, max_price=0.999)
    assert r["n"] == 30 and r["win_rate"] == 1.0
    assert r["net_per_contract"] == pytest.approx(r["gross_per_contract"] - r["fee_per_contract"])
    assert r["fee_per_contract"] > 0
    assert analysis.backtest(ds, 0, max_price=0.001) == {"n": 0.0}
    grid = analysis.backtest_grid(ds, horizons=(300, 60))
    assert {"horizon_s", "max_price", "min_dist_bps", "net_per_contract"} <= set(grid.columns)


def test_lead_lag_runs(ds):
    ll = analysis.lead_lag(ds, lag_seconds=60)
    assert set(ll["series"]) == {"KXBTC15M", "KXDOGE15M"}
    assert ll["spot_then_mid"].abs().le(1).all()


def test_report_text(ds):
    text = analysis.report(ds)
    for section in (
        "== coverage",
        "== brier",
        "== calibration",
        "== spot-vs-strike",
        "== lead-lag",
        "== backtest",
    ):
        assert section in text


def test_report_with_nothing_settled(tmp_path):
    store = MarketDataStore(tmp_path / "empty.sqlite")
    store.upsert_market(Market.from_dict({"ticker": "KXBTC15M-1", "status": "active"}), now=1.0)
    store.close()
    text = analysis.report(analysis.load(str(tmp_path / "empty.sqlite")))
    assert "keep recording" in text and "== brier" not in text


def test_prefer_websocket_spot():
    spot = pd.DataFrame(
        {
            "ts": [1.0, 5.0, 10.0, 6.0, 7.0, 15.0, 1.0],
            "source": ["coinbase"] * 3 + ["coinbase_ws"] * 2 + ["coinbase", "coinbase"],
            "symbol": ["BTC-USD"] * 6 + ["DOGE-USD"],
            "price": [1, 2, 3, 4, 5, 6, 7],
        }
    )
    out = analysis.prefer_websocket_spot(spot)
    btc = out[out["symbol"] == "BTC-USD"]
    # REST rows inside the websocket span (ts 6..7) are dropped: none here, but ts=5 and 10 kept
    assert sorted(btc["ts"].tolist()) == [1.0, 5.0, 6.0, 7.0, 10.0, 15.0]
    assert out[out["symbol"] == "DOGE-USD"]["price"].tolist() == [7]
    inside = pd.DataFrame(
        {"ts": [6.5], "source": ["coinbase"], "symbol": ["BTC-USD"], "price": [9]}
    )
    out2 = analysis.prefer_websocket_spot(pd.concat([spot, inside]))
    assert 9 not in out2["price"].tolist()
