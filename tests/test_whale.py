import math
import random

import pytest

pd = pytest.importorskip("pandas")

from kalshi_bot import whale  # noqa: E402
from kalshi_bot.models import Market, Orderbook, Trade  # noqa: E402
from kalshi_bot.storage import MarketDataStore  # noqa: E402


def build_db(path, n_markets=40, seed=7, whale_knows=False):
    """Synthetic markets with prints. If ``whale_knows``, big prints are on the winning side."""
    rng = random.Random(seed)
    store = MarketDataStore(path)
    t0 = 1_700_000_000.0
    for i in range(n_markets):
        series = "KXBTC15M" if i % 2 == 0 else "KXDOGE15M"
        symbol = whale.SPOT_SYMBOLS[series]
        strike = 80_000.0 if series == "KXBTC15M" else 0.25
        open_ts, close_ts = t0 + i * 900, t0 + i * 900 + 900
        ticker = f"{series}-{i}"
        result = "yes" if rng.random() < 0.5 else "no"
        spot = strike * (1.0005 if result == "yes" else 0.9995)
        trades = []
        for k in range(31):
            ts = open_ts + 30 * k
            mid = 0.5 + (0.01 * k if result == "yes" else -0.01 * k)
            mid = min(0.95, max(0.05, mid))
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
            # a few small prints and, every 5th tick, a big one
            for j in range(3):
                side = "yes" if rng.random() < 0.5 else "no"
                count = 5.0
                if j == 0 and k % 5 == 0:
                    count = 3000.0
                    if whale_knows:
                        side = result
                yp = round(mid + 0.005, 3)
                trades.append(
                    Trade.from_dict(
                        {
                            "trade_id": f"{ticker}-{k}-{j}",
                            "ticker": ticker,
                            "created_time": ts + 0.1 * j + 1.0,
                            "yes_price_dollars": f"{yp:.4f}",
                            "no_price_dollars": f"{1 - yp:.4f}",
                            "count_fp": f"{count:.2f}",
                            "taker_side": side,
                            "taker_book_side": "ask",
                        }
                    )
                )
            # one late print near the bid so maker fills happen sometimes
            trades.append(
                Trade.from_dict(
                    {
                        "trade_id": f"{ticker}-{k}-low",
                        "ticker": ticker,
                        "created_time": ts + 5.0,
                        "yes_price_dollars": f"{mid - 0.004:.4f}",
                        "no_price_dollars": f"{1 - mid + 0.004:.4f}",
                        "count_fp": "1.00",
                        "taker_side": "no",
                        "taker_book_side": "bid",
                    }
                )
            )
        store.insert_trades(ticker, trades)
        store.mark_settled(
            Market.from_dict(
                {
                    "ticker": ticker,
                    "status": "settled",
                    "result": result,
                    "close_time": close_ts,
                    "expiration_value": spot,
                }
            ),
            now=close_ts + 60,
        )
    store.close()


@pytest.fixture(scope="module")
def informed(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "informed.sqlite"
    build_db(path, whale_knows=True)
    return whale.load(str(path))


@pytest.fixture(scope="module")
def uninformed(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "random.sqlite"
    build_db(path, whale_knows=False, seed=11)
    return whale.load(str(path))


def test_sweep_aggregation_groups_nearby_prints():
    trades = pd.DataFrame(
        {
            "ticker": ["T"] * 4,
            "ts": [1.0, 1.1, 1.2, 5.0],
            "taker_side": ["yes"] * 4,
            "taker_book_side": ["ask"] * 4,
            "yes_price": [0.5, 0.51, 0.52, 0.5],
            "no_price": [0.5, 0.49, 0.48, 0.5],
            "count": [100.0, 100.0, 100.0, 10.0],
        }
    )
    sw = whale.aggregate_sweeps(trades)
    assert len(sw) == 2
    big = sw.iloc[0]
    assert big["prints"] == 3 and big["count"] == 300 and abs(big["price"] - 0.51) < 1e-9
    assert abs(big["notional"] - 153.0) < 1e-9


def test_load_scores_every_sweep(informed):
    s = informed.sweeps
    assert informed.n_markets == 40 and len(s) > 0
    assert s["implied"].notna().all() and s["copy_ask"].notna().all()
    assert s["spot"].notna().all()
    assert set(s["win"].unique()) <= {0.0, 1.0}
    assert ((s["taker_net"] < s["taker_gross"]) | s["taker_net"].isna()).all()


def test_informed_whales_show_positive_excess(informed):
    w = whale.whales(informed, 1000.0)
    assert len(w) >= 200
    summary = whale.summarize(w)
    assert summary["win_rate"] == 1.0
    assert summary["excess"] > 0 and summary["excess_lo"] > 0
    assert "VIABLE" in whale.verdict(informed, 1000.0)


def test_uninformed_whales_have_no_edge(uninformed):
    w = whale.whales(uninformed, 1000.0)
    summary = whale.summarize(w)
    assert summary["excess_lo"] < 0 < summary["excess_hi"] or abs(summary["excess"]) < 0.1
    assert "NOT VIABLE" in whale.verdict(uninformed, 1000.0) or "INCONCLUSIVE" in whale.verdict(
        uninformed, 1000.0
    )


def test_gate_and_splits(informed):
    assert "INCONCLUSIVE" in whale.verdict(informed, 1e9)
    ladder = whale.threshold_ladder(informed)
    assert list(ladder["slice"]) == [">= $250", ">= $1,000", ">= $5,000"]
    w = whale.whales(informed, 1000.0)
    assert len(whale.time_split(w)) == 2
    sc = whale.spot_conditioning(w)
    assert "spot side at same moments (no whale)" in set(sc["slice"])
    by_ttc = whale.split_by(w, "secs_to_close", whale.TTC_BINS)
    assert by_ttc["sweeps"].sum() == len(w)


def test_cluster_bootstrap_widens_with_clustering():
    rows = []
    for t in range(10):
        v = 1.0 if t % 2 == 0 else 0.0
        rows += [{"ticker": f"T{t}", "x": v}] * 20
    df = pd.DataFrame(rows)
    point, lo, hi = whale.cluster_bootstrap(df, "x", rounds=500)
    assert abs(point - 0.5) < 1e-9
    assert hi - lo > 0.4  # 10 clusters of all-0 / all-1 -> wide interval
    assert math.isnan(whale.cluster_bootstrap(df.iloc[:0], "x")[0])


def test_report_renders(informed):
    text = whale.report(informed, threshold=1000.0)
    for section in (
        "== data",
        "== verdict",
        "== threshold ladder",
        "== spot conditioning",
        "== by seconds to close",
        "== by series",
        "== relative size",
    ):
        assert section in text
