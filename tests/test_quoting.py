import sys
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")

from kalshi_bot import fairvalue, quoting  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from test_fairvalue import in_memory  # noqa: E402


def _with_prints(fv, kind, seed=0, per_snapshot=2):
    """Synthetic public prints between snapshots.

    ``noise``: takers sell either side 3c under the book mid at random, so a
    maker quoting under fair value is paid by uninformed flow. ``informed``:
    takers only ever sell the side that goes on to lose, so every fill is
    adverse.
    """
    rng = np.random.default_rng(seed)
    rows = []
    s = fv.snapshots
    for ticker, g in s.groupby("ticker", sort=False):
        g = g.sort_values("ts")
        won_yes = g["won_yes"].iloc[0]
        ts = g["ts"].to_numpy()
        mid = ((g["yes_bid"] + g["yes_ask"]) / 2).to_numpy()
        for i in range(len(ts) - 1):
            for j in range(per_snapshot):
                t = ts[i] + (ts[i + 1] - ts[i]) * (j + 1) / (per_snapshot + 1)
                if kind == "noise":
                    taker = "no" if rng.uniform() < 0.5 else "yes"
                else:  # informed: sell the loser
                    taker = "no" if won_yes == 0 else "yes"
                # taker "no" buys NO = sells YES; print lands 3c under the YES mid
                yes_price = mid[i] - 0.03 if taker == "no" else mid[i] + 0.03
                yes_price = min(0.99, max(0.01, round(yes_price, 2)))
                rows.append(
                    {
                        "ticker": ticker,
                        "ts": t,
                        "yes_price": yes_price,
                        "no_price": round(1 - yes_price, 2),
                        "taker_side": taker,
                    }
                )
    return fairvalue.FairValueData(
        snapshots=fv.snapshots,
        markets=fv.markets,
        spot=fv.spot,
        trades=pd.DataFrame(rows),
        vol_windows=fv.vol_windows,
    )


@pytest.fixture(scope="module")
def efficient():
    return in_memory(120, 5, "efficient")


def test_noise_flow_pays_the_maker(efficient):
    fv = _with_prints(efficient, "noise")
    res = quoting.backtest(fv, spread=0.02, fill="touch")
    assert len(res.fills) > 50
    s = quoting.summarize(res)
    assert s["net"] > 0 and s["fill_rate"] > 0.3
    assert s["adverse"] == pytest.approx(0, abs=0.1)  # fills are not systematically wrong
    assert set(res.fills["side"]) == {"yes", "no"}
    assert (res.fills["fee"] == 0).all()  # crypto series: no maker fee
    assert res.markets["locked"].mean() > 0  # both sides fill in some markets
    # every fill price sits at least the spread under the model's fair value
    assert (res.fills["fair"] - res.fills["price"] >= 0.02 - 1e-9).all()


def test_informed_flow_picks_the_maker_off(efficient):
    fv = _with_prints(efficient, "informed")
    res = quoting.backtest(fv, spread=0.02, fill="touch")
    assert len(res.fills) > 50
    s = quoting.summarize(res)
    assert s["win_rate"] == 0.0 and s["net"] < 0 and s["adverse"] > 0.3
    assert res.markets["locked"].sum() == 0


def test_fill_models_and_edges(efficient):
    fv = _with_prints(efficient, "noise")
    touch = quoting.backtest(fv, spread=0.02, fill="touch")
    cross = quoting.backtest(fv, spread=0.02, fill="cross")
    assert len(cross.fills) <= len(touch.fills)
    wide = quoting.backtest(fv, spread=0.05, fill="touch")
    assert len(wide.fills) < len(touch.fills)  # a wider quote is hit less
    late = quoting.backtest(fv, spread=0.02, fill="touch", min_ttc=600)
    assert (late.fills["secs_to_close"] >= 600).all()
    assert quoting.backtest(efficient, spread=0.02).fills.empty  # no prints
    with pytest.raises(ValueError):
        quoting.backtest(fv, fill="maybe")
    no_side = fairvalue.FairValueData(
        snapshots=fv.snapshots,
        markets=fv.markets,
        spot=fv.spot,
        trades=fv.trades.drop(columns=["taker_side"]),
        vol_windows=fv.vol_windows,
    )
    with pytest.raises(ValueError):
        quoting.backtest(no_side)


def test_grid_verdict_and_report(efficient):
    fv = _with_prints(efficient, "noise")
    g = quoting.grid(fv, spreads=(0.01, 0.03), fill="touch")
    assert len(g) == 2 * len(fv.vol_windows) and {"spread", "net", "fills"} <= set(g.columns)
    v = quoting.verdict(fv, fill="touch")
    assert v.startswith(("VIABLE", "NOT VIABLE", "INCONCLUSIVE"))
    text = quoting.report(fv, fill="touch")
    assert "spread grid" in text and "verdict" in text and "adverse selection" in text
    assert not quoting.by_ttc(quoting.backtest(fv, fill="touch").fills).empty
    bad = quoting.verdict(_with_prints(efficient, "informed"), fill="touch")
    assert bad.startswith(("NOT VIABLE", "INCONCLUSIVE"))
    assert "no trade prints" in quoting.report(efficient)


def test_quote_test_cli_parses():
    import kalshi_bot.cli as cli

    args = cli.build_parser().parse_args(["quote-test", "--fill", "touch", "--min-ttc", "300"])
    assert args.func is cli.cmd_quote_test and args.fill == "touch" and args.min_ttc == 300
