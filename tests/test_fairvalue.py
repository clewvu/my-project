import math

import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")

from kalshi_bot import analysis, fairvalue  # noqa: E402
from kalshi_bot.models import Market, Orderbook  # noqa: E402
from kalshi_bot.storage import MarketDataStore  # noqa: E402

SIGMA_SEC = 8e-5  # about 45% annualised
STEP = 5.0
SNAP_EVERY = 30.0
T0 = 1_700_000_000.0
HISTORY = 3600.0  # spot history before the first market, for the vol estimator


def build_world(n_markets=120, seed=3, book="stale"):
    """Continuous GBM spot per symbol; consecutive 15-minute markets per series.

    Returns raw ``markets``, ``snapshots`` and ``spot`` frames shaped like the
    SQLite tables. ``book="stale"``: the book always shows a 50c mid and
    ignores spot, so a correct model has an edge. ``book="efficient"``: the
    book is the true fair value (true sigma) rounded to a cent, so nothing
    beats it after fees.
    """
    rng = np.random.default_rng(seed)
    markets, snaps, spots = [], [], []
    for series in ("KXBTC15M", "KXDOGE15M"):
        symbol = analysis.SPOT_SYMBOLS[series]
        grid = np.arange(T0 - HISTORY, T0 + n_markets * 900 + STEP, STEP)
        base = 80_000.0 if series == "KXBTC15M" else 0.25
        logp = np.log(base) + np.cumsum(rng.normal(0, SIGMA_SEC * math.sqrt(STEP), len(grid)))
        price = np.exp(logp)
        spots.append(
            pd.DataFrame({"ts": grid, "source": "coinbase", "symbol": symbol, "price": price})
        )
        for i in range(n_markets):
            open_ts = T0 + i * 900
            close_ts = open_ts + 900
            ticker = f"{series}-{i}"
            strike = float(price[np.searchsorted(grid, open_ts)])
            lo = np.searchsorted(grid, close_ts - 60, side="left")
            hi = np.searchsorted(grid, close_ts, side="right")
            settle = float(price[lo:hi].mean())
            markets.append(
                {
                    "ticker": ticker,
                    "series_ticker": series,
                    "strike": strike,
                    "open_ts": open_ts,
                    "close_ts": close_ts,
                    "status": "settled",
                    "result": "yes" if settle >= strike else "no",
                    "expiration_value": settle,
                }
            )
            for k in range(int(900 / SNAP_EVERY) + 1):
                ts = open_ts + SNAP_EVERY * k
                spot = float(price[np.searchsorted(grid, ts)])
                if book == "stale":
                    mid = 0.5
                else:
                    p = float(fairvalue.fair_value(spot, strike, SIGMA_SEC, close_ts - ts))
                    mid = min(0.99, max(0.01, round(p, 2)))
                snaps.append(
                    {
                        "ts": ts,
                        "ticker": ticker,
                        "secs_to_close": close_ts - ts,
                        "yes_bid": round(mid - 0.005, 3),
                        "yes_ask": round(mid + 0.005, 3),
                        "no_bid": round(1 - mid - 0.005, 3),
                        "no_ask": round(1 - mid + 0.005, 3),
                        "last_price": mid,
                        "yes_depth": 100.0,
                        "no_depth": 100.0,
                    }
                )
    return pd.DataFrame(markets), pd.DataFrame(snaps), pd.concat(spots, ignore_index=True)


def in_memory(n_markets, seed, book):
    markets, snaps, spot = build_world(n_markets, seed, book)
    return fairvalue.prepare(analysis.assemble(markets, snaps, spot))


def write_db(path, markets, snaps, spot):
    store = MarketDataStore(path)
    store.insert_spots(
        [(float(r.ts), r.source, r.symbol, float(r.price), None) for r in spot.itertuples()]
    )
    for m in markets.itertuples():
        store.upsert_market(
            Market.from_dict(
                {
                    "ticker": m.ticker,
                    "series_ticker": m.series_ticker,
                    "status": "active",
                    "floor_strike": m.strike,
                    "open_time": m.open_ts,
                    "close_time": m.close_ts,
                }
            ),
            now=m.open_ts,
        )
    for r in snaps.itertuples():
        mid = r.last_price
        m = Market.from_dict({"ticker": r.ticker, "close_time": r.ts + r.secs_to_close})
        book = Orderbook.from_dict(
            r.ticker,
            {
                "orderbook_fp": {
                    "yes_dollars": [[f"{mid - 0.005:.3f}", "100"]],
                    "no_dollars": [[f"{1 - mid - 0.005:.3f}", "100"]],
                }
            },
        )
        store.insert_snapshot(r.ts, m, book)
    for m in markets.itertuples():
        store.mark_settled(
            Market.from_dict(
                {
                    "ticker": m.ticker,
                    "status": "settled",
                    "result": m.result,
                    "close_time": m.close_ts,
                    "expiration_value": m.expiration_value,
                }
            ),
            now=m.close_ts + 60,
        )
    store.close()


@pytest.fixture(scope="module")
def stale():
    return in_memory(400, 3, "stale")


@pytest.fixture(scope="module")
def efficient():
    return in_memory(150, 5, "efficient")


@pytest.fixture(scope="module")
def small_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "small.sqlite"
    write_db(path, *build_world(30, 9, "stale"))
    return str(path)


# ---------------------------------------------------------------- model


def test_norm_cdf():
    assert fairvalue.norm_cdf(0.0) == pytest.approx(0.5)
    assert fairvalue.norm_cdf(1.959964) == pytest.approx(0.975, abs=1e-6)
    out = fairvalue.norm_cdf([-1e9, float("nan"), 1e9])
    assert out[0] == 0.0 and math.isnan(out[1]) and out[2] == 1.0


def test_effective_tau():
    tau = fairvalue.effective_tau([900, 60, 30, 0, -5])
    assert list(tau) == [860.0, 20.0, 10.0, 1.0, 1.0]


def test_fair_value_shape():
    assert fairvalue.fair_value(100.0, 100.0, 1e-4, 300) == pytest.approx(0.5)
    up = float(fairvalue.fair_value(100.0 * math.exp(0.001), 100.0, 1e-4, 300))
    down = float(fairvalue.fair_value(100.0 * math.exp(-0.001), 100.0, 1e-4, 300))
    assert up > 0.5 > down and up == pytest.approx(1 - down, abs=1e-9)
    # more volatility or more time pulls toward a coin flip
    assert float(fairvalue.fair_value(100.1, 100.0, 1e-3, 300)) < up
    assert float(fairvalue.fair_value(100.1, 100.0, 1e-4, 800)) < up
    assert float(fairvalue.fair_value(100.1, 100.0, 1e-4, 61)) > up
    assert math.isnan(fairvalue.fair_value(float("nan"), 100.0, 1e-4, 300))
    assert float(fairvalue.fair_value(100.1, 100.0, 0.0, 300)) == 1.0


def test_realized_vol_recovers_sigma():
    rng = np.random.default_rng(0)
    ts = np.arange(0, 4 * 3600, STEP)
    logp = np.cumsum(rng.normal(0, SIGMA_SEC * math.sqrt(STEP), len(ts)))
    spot = pd.DataFrame(
        {"ts": ts, "source": "coinbase", "symbol": "BTC-USD", "price": np.exp(logp)}
    )
    vol = fairvalue.realized_vol(spot, "BTC-USD", 1800)
    assert vol["ts"].min() >= 1800 * fairvalue.VOL_MIN_FRACTION - STEP
    assert vol["sigma"].median() == pytest.approx(SIGMA_SEC, rel=0.15)
    assert fairvalue.realized_vol(spot, "DOGE-USD", 1800).empty
    # a gap longer than three steps produces no return across it, but the
    # estimate continues once enough of the window has returns again
    gappy = spot[(spot["ts"] < 1000) | (spot["ts"] > 1200)]
    v2 = fairvalue.realized_vol(gappy, "BTC-USD", 300)
    assert not v2.empty and v2["sigma"].median() == pytest.approx(SIGMA_SEC, rel=0.3)


# ---------------------------------------------------------------- data


def test_prepare_attaches_sigma_and_model(stale):
    s = stale.snapshots
    assert len(stale.settled_markets) == 800
    assert len(fairvalue.basis(stale)) == 2
    assert s["spot"].notna().all()
    for w in fairvalue.VOL_WINDOWS:
        assert s[f"sigma_{w}"].notna().mean() > 0.95
        assert s[f"p_{w}"].dropna().between(0, 1).all()
    assert s["sigma_1800"].median() == pytest.approx(SIGMA_SEC, rel=0.2)
    assert stale.trades is None


def test_load_from_sqlite(small_db):
    fv = fairvalue.load(small_db)
    assert len(fv.settled_markets) == 60
    assert fv.snapshots["p_1800"].notna().mean() > 0.9
    assert fv.trades is not None and fv.trades.empty
    assert fv.markets["expiration_value"].notna().all()
    only_btc = fairvalue.load(small_db, series=["KXBTC15M"])
    assert set(only_btc.snapshots["series_ticker"]) == {"KXBTC15M"}


def test_basis_is_zero_when_index_equals_coinbase(small_db):
    b = fairvalue.basis(fairvalue.load(small_db))
    assert set(b["series"]) == {"KXBTC15M", "KXDOGE15M"}
    assert (b["n"] == 30).all()
    assert (b["avg_bps_mean"].abs() < 1e-6).all()
    assert (b["sign_mismatch"] == 0).all()


# ---------------------------------------------------------------- backtest


def test_backtest_on_stale_book_finds_the_edge(stale):
    t = fairvalue.backtest(stale, 1800, margin=0.05)
    assert len(t) > 500
    assert t["ticker"].is_unique
    assert (t["ts"] > t["signal_ts"]).all()
    assert (t["secs_to_close"] >= fairvalue.MIN_TTC_S).all()
    assert set(t["side"]) == {"yes", "no"}
    assert (t["p_model"] > 0.5).all() and (t["edge"] >= 0.05).all()
    assert t["win"].mean() > 0.55
    assert (t["net"] == t["gross"] - t["fee"]).all()
    assert t["net"].mean() > 0.03
    assert t["maker_net"].isna().all()  # no prints in this world
    # the side the model favours agrees with spot vs strike
    above = t["spot"] >= t["strike"]
    assert ((t["side"] == "yes") == above).all()


def test_backtest_respects_ttc_and_lag(stale):
    t = fairvalue.backtest(stale, 1800, margin=0.05, min_ttc=600)
    assert (t["secs_to_close"] >= 600).all()
    same = fairvalue.backtest(stale, 1800, margin=0.05, fill_lag=0)
    assert (same["ts"] == same["signal_ts"]).all()
    assert fairvalue.backtest(stale, 1800, margin=5.0).empty
    empty = fairvalue.backtest(stale, 999, margin=0.0)
    assert list(empty.columns) == fairvalue.TRADE_COLUMNS


def test_verdict_viable_on_stale_book(stale):
    text, cfg = fairvalue.verdict(stale)
    assert text.startswith("VIABLE"), text
    assert cfg is not None and cfg[0] in fairvalue.VOL_WINDOWS


def test_efficient_book_yields_no_edge(efficient):
    t = fairvalue.backtest(efficient, 1800, margin=0.0)
    s = fairvalue.summarize(t)
    assert s["trades"] == 0 or s["taker_net"] < 0.05
    text, _ = fairvalue.verdict(efficient)
    assert text.startswith(("INCONCLUSIVE", "NOT VIABLE")), text


def test_gap_signal_and_brier(stale, efficient):
    gs = fairvalue.gap_signal(stale, 300, 1800)
    assert gs["n"].sum() == 800
    top = gs.iloc[-1]
    bottom = gs.iloc[0]
    assert top["mean_gap"] > 0 > bottom["mean_gap"]
    assert top["excess"] > 0 > bottom["excess"]
    mb = fairvalue.model_brier(stale)
    row = mb[mb["horizon_s"] == 300].iloc[0]
    assert row["model_1800"] < row["market"]
    mb2 = fairvalue.model_brier(efficient)
    row2 = mb2[mb2["horizon_s"] == 300].iloc[0]
    assert abs(row2["model_1800"] - row2["market"]) < 0.05


def test_split_and_summaries(stale):
    train, test = fairvalue.split_tickers(stale)
    assert len(train) == 560 and len(test) == 240 and not (train & test)
    t = fairvalue.backtest(stale, 1800, margin=0.05)
    ts = fairvalue.time_split(stale, t)
    assert ts["trades"].sum() == len(t)
    by_series = fairvalue.split_by(t, "series_ticker")
    assert by_series["trades"].sum() == len(t)
    by_ttc = fairvalue.split_by(t, "secs_to_close", fairvalue.TTC_BINS)
    assert by_ttc["trades"].sum() == len(t)
    empty = fairvalue.summarize(t.iloc[:0])
    assert empty["trades"] == 0 and math.isnan(empty["taker_net"])
    g = fairvalue.grid(stale, tickers=train)
    assert len(g) == len(fairvalue.VOL_WINDOWS) * len(fairvalue.MARGINS)
    assert fairvalue.select_config(stale, set()) is None


def test_maker_fill_uses_prints(stale):
    t = fairvalue.backtest(stale, 1800, margin=0.05).head(3)
    first = t.iloc[0]
    # a print through our resting level after the fill => maker filled
    prints = pd.DataFrame(
        {
            "ticker": [first["ticker"]],
            "ts": [first["ts"] + 1.0],
            "yes_price": [first["rest_price"] if first["side"] == "yes" else 0.99],
            "no_price": [first["rest_price"] if first["side"] == "no" else 0.99],
        }
    )
    out = fairvalue._attach_maker(t.drop(columns=["maker_filled", "maker_net"]), prints)
    assert out.iloc[0]["maker_filled"] == 1.0 and not math.isnan(out.iloc[0]["maker_net"])
    assert (out.iloc[1:]["maker_filled"] == 0.0).all()


# ---------------------------------------------------------------- report


def test_report_renders(stale):
    text = fairvalue.report(stale, show_trades=5)
    for section in (
        "== data",
        "== realised volatility",
        "== basis",
        "== brier score",
        "== gap signal",
        "== verdict",
        "== training grid",
        "== time-ordered validation split",
        "== by series",
        "== by seconds to close",
        "== last 5 trades",
    ):
        assert section in text, section


def test_report_with_nothing_settled(tmp_path):
    store = MarketDataStore(tmp_path / "empty.sqlite")
    store.upsert_market(Market.from_dict({"ticker": "KXBTC15M-1", "status": "active"}), now=1.0)
    store.close()
    text = fairvalue.report(fairvalue.load(str(tmp_path / "empty.sqlite")))
    assert "keep recording" in text and "== verdict" not in text
