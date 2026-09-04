"""Whale-follow hypothesis test.

Hypothesis: large aggressive prints predict settlement beyond what the market
price already implies. This module treats it as something to falsify.

Method (see docs/research-brief.md, section 2):

1. Public prints are aggregated into *sweeps*: prints in the same market, on
   the same taker side, within ``SWEEP_WINDOW`` seconds of each other are one
   order that walked several levels. Notional is summed, price is
   volume-weighted.
2. A sweep is a whale if its notional (contracts x price on the taker's side)
   is at least the threshold.
3. For each whale sweep we take the latest book snapshot at or before the
   print for the market's implied probability of the whale's side, and the
   first snapshot after it for the price a copier would pay.
4. Primary statistic: ``excess = mean(win) - mean(implied)``. If the whale
   knows nothing the market does not, excess is zero. The confidence interval
   is a bootstrap that resamples *markets*, not prints, because every print in
   a window shares one settlement.
5. Copy P&L as a taker (next ask, exact fee) and as a maker (rest one tick
   inside the spread, filled only if a later print trades through, no fee).
6. Spot conditioning: does the whale add anything once you know whether spot
   is above the strike at that moment?
7. Splits by time to close, series, aggressor book side, and a threshold
   ladder; a time-ordered 70/30 validation split; a sufficiency gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .analysis import SPOT_SYMBOLS, SPOT_TOLERANCE_S, prefer_websocket_spot
from .fees import fee_per_contract
from .models import TICK

SWEEP_WINDOW_S = 0.25
THRESHOLDS = (250.0, 1000.0, 5000.0)
TTC_BINS = (0, 60, 120, 300, 600, 900, float("inf"))
MIN_SWEEPS_FOR_VERDICT = 200
VIABLE_NET_PER_CONTRACT = 0.01
BOOTSTRAP_ROUNDS = 2000


@dataclass
class WhaleData:
    sweeps: pd.DataFrame  # all sweeps, settled markets only
    n_markets: int
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- loading


def load(db_path: str, series: list[str] | None = None) -> WhaleData:
    import sqlite3

    con = sqlite3.connect(db_path)
    try:
        markets = pd.read_sql(
            "SELECT ticker, series_ticker, strike, close_ts, result FROM markets "
            "WHERE result IN ('yes', 'no')",
            con,
        )
        trades = pd.read_sql(
            "SELECT trade_id, ticker, ts, yes_price, no_price, count, taker_side, raw FROM trades",
            con,
        )
        snaps = pd.read_sql(
            "SELECT ts, ticker, yes_bid, yes_ask, no_bid, no_ask FROM snapshots", con
        )
        spot = prefer_websocket_spot(pd.read_sql("SELECT ts, source, symbol, price FROM spot", con))
    finally:
        con.close()
    if series:
        markets = markets[markets["series_ticker"].isin(series)]
    trades = trades[trades["ticker"].isin(markets["ticker"])]
    trades = trades.dropna(subset=["ts", "count", "taker_side"])
    trades["taker_book_side"] = trades["raw"].map(_book_side)
    trades = trades.drop(columns=["raw"])

    sweeps = aggregate_sweeps(trades)
    sweeps = sweeps.merge(markets, on="ticker", how="inner")
    sweeps["won_yes"] = (sweeps["result"] == "yes").astype(float)
    sweeps["secs_to_close"] = sweeps["close_ts"] - sweeps["ts"]
    # prints stamped after close are settlement artefacts, not tradeable signals
    sweeps = sweeps[sweeps["secs_to_close"] >= 0]
    sweeps = _attach_books(sweeps, snaps)
    sweeps = _attach_spot(sweeps, spot)
    sweeps = _attach_maker_fills(sweeps, trades)
    sweeps = _score(sweeps)
    return WhaleData(sweeps=sweeps.reset_index(drop=True), n_markets=int(markets.shape[0]))


def _book_side(raw: object) -> str | None:
    try:
        return json.loads(raw).get("taker_book_side") if isinstance(raw, str) else None
    except ValueError:
        return None


def aggregate_sweeps(trades: pd.DataFrame, window: float = SWEEP_WINDOW_S) -> pd.DataFrame:
    """Group prints from one aggressive order (same market, side, within ``window`` s)."""
    if trades.empty:
        return pd.DataFrame(
            columns=[
                "ticker",
                "ts",
                "taker_side",
                "taker_book_side",
                "count",
                "price",
                "notional",
                "prints",
                "rel_size",
            ]
        )
    t = trades.sort_values(["ticker", "taker_side", "ts"]).copy()
    t["side_price"] = np.where(t["taker_side"] == "yes", t["yes_price"], t["no_price"])
    gap = t.groupby(["ticker", "taker_side"])["ts"].diff()
    t["sweep_id"] = ((gap.isna()) | (gap > window)).cumsum()
    t["value"] = t["count"] * t["side_price"]
    g = t.groupby("sweep_id", sort=False)
    out = pd.DataFrame(
        {
            "ticker": g["ticker"].first(),
            "ts": g["ts"].first(),
            "taker_side": g["taker_side"].first(),
            "taker_book_side": g["taker_book_side"].first(),
            "count": g["count"].sum(),
            "notional": g["value"].sum(),
            "prints": g.size(),
        }
    )
    out["price"] = out["notional"] / out["count"]
    median = out.groupby("ticker")["count"].transform("median")
    out["rel_size"] = out["count"] / median.replace(0, np.nan)
    return out.reset_index(drop=True)


def _attach_books(sweeps: pd.DataFrame, snaps: pd.DataFrame) -> pd.DataFrame:
    sweeps = sweeps.sort_values("ts")
    snaps = snaps.sort_values("ts")
    before = pd.merge_asof(
        sweeps,
        snaps.rename(columns=lambda c: c if c in ("ts", "ticker") else f"pre_{c}"),
        on="ts",
        by="ticker",
        direction="backward",
        tolerance=60.0,
    )
    after = pd.merge_asof(
        sweeps[["ts", "ticker"]],
        snaps.rename(columns=lambda c: c if c in ("ts", "ticker") else f"post_{c}"),
        on="ts",
        by="ticker",
        direction="forward",
        tolerance=60.0,
    )
    for col in ("post_yes_bid", "post_yes_ask", "post_no_bid", "post_no_ask"):
        before[col] = after[col].to_numpy()
    return before


def _attach_spot(sweeps: pd.DataFrame, spot: pd.DataFrame) -> pd.DataFrame:
    sweeps = sweeps.copy()
    sweeps["spot"] = np.nan
    if spot.empty:
        return sweeps
    parts = []
    for series, g in sweeps.groupby("series_ticker", sort=False):
        symbol = SPOT_SYMBOLS.get(str(series))
        s = spot[spot["symbol"] == symbol][["ts", "price"]].sort_values("ts")
        g = g.drop(columns=["spot"]).sort_values("ts")
        if s.empty:
            g["spot"] = np.nan
        else:
            g = pd.merge_asof(
                g,
                s.rename(columns={"price": "spot"}),
                on="ts",
                direction="backward",
                tolerance=SPOT_TOLERANCE_S,
            )
        parts.append(g)
    return pd.concat(parts, ignore_index=True)


def _attach_maker_fills(sweeps: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    """Lowest price each side trades at *after* each sweep, for the maker fill model."""
    sweeps = sweeps.copy()
    sweeps["future_min_yes"] = np.nan
    sweeps["future_min_no"] = np.nan
    if trades.empty:
        return sweeps
    t = trades.sort_values("ts")
    for ticker, g in sweeps.groupby("ticker", sort=False):
        tt = t[t["ticker"] == ticker]
        if tt.empty:
            continue
        ts = tt["ts"].to_numpy()
        # suffix minima: min over trades strictly after index i
        ymin = np.minimum.accumulate(tt["yes_price"].fillna(np.inf).to_numpy()[::-1])[::-1]
        nmin = np.minimum.accumulate(tt["no_price"].fillna(np.inf).to_numpy()[::-1])[::-1]
        idx = np.searchsorted(ts, g["ts"].to_numpy(), side="right")
        fy = np.where(idx < len(ts), ymin[np.minimum(idx, len(ts) - 1)], np.inf)
        fn = np.where(idx < len(ts), nmin[np.minimum(idx, len(ts) - 1)], np.inf)
        sweeps.loc[g.index, "future_min_yes"] = fy
        sweeps.loc[g.index, "future_min_no"] = fn
    return sweeps


def _score(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    yes = df["taker_side"] == "yes"
    df["win"] = np.where(yes, df["won_yes"], 1 - df["won_yes"]).astype(float)
    pre_mid = (df["pre_yes_bid"] + df["pre_yes_ask"]) / 2
    df["implied"] = np.where(yes, pre_mid, 1 - pre_mid)
    # taker copy at the first post-print ask on the whale's side
    df["copy_ask"] = np.where(yes, df["post_yes_ask"], df["post_no_ask"])
    df["taker_fee"] = df["copy_ask"].map(lambda p: fee_per_contract(p) if pd.notna(p) else np.nan)
    df["taker_gross"] = df["win"] * (1 - df["copy_ask"]) - (1 - df["win"]) * df["copy_ask"]
    df["taker_net"] = df["taker_gross"] - df["taker_fee"]
    # maker copy: rest one tick inside the spread on our side, filled if traded through
    post_bid = np.where(yes, df["post_yes_bid"], df["post_no_bid"])
    df["rest_price"] = np.round(post_bid + TICK, 4)
    future_min = np.where(yes, df["future_min_yes"], df["future_min_no"])
    df["maker_filled"] = (future_min <= df["rest_price"]).astype(float)
    df["maker_net"] = np.where(
        df["maker_filled"] == 1,
        df["win"] * (1 - df["rest_price"]) - (1 - df["win"]) * df["rest_price"],
        np.nan,
    )
    # spot conditioning
    spot_up = df["spot"] >= df["strike"]
    df["spot_side"] = np.where(df["spot"].isna(), None, np.where(spot_up, "yes", "no"))
    df["agrees_with_spot"] = np.where(
        df["spot"].isna(), np.nan, (df["spot_side"] == df["taker_side"]).astype(float)
    )
    df["spot_win"] = np.where(
        df["spot"].isna(), np.nan, (df["spot_side"] == df["result"]).astype(float)
    )
    df["spot_implied"] = np.where(
        df["spot"].isna(), np.nan, np.where(spot_up, pre_mid, 1 - pre_mid)
    )
    return df


# ---------------------------------------------------------------- statistics


def cluster_bootstrap(
    df: pd.DataFrame,
    value: str,
    cluster: str = "ticker",
    rounds: int = BOOTSTRAP_ROUNDS,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Mean of ``value`` with a 95% CI from resampling clusters with replacement."""
    d = df.dropna(subset=[value])
    if d.empty:
        return (np.nan, np.nan, np.nan)
    groups = [g[value].to_numpy() for _, g in d.groupby(cluster)]
    sums = np.array([g.sum() for g in groups])
    counts = np.array([len(g) for g in groups])
    point = sums.sum() / counts.sum()
    if len(groups) < 2:
        return (point, np.nan, np.nan)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(groups), size=(rounds, len(groups)))
    means = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return (float(point), float(lo), float(hi))


def summarize(df: pd.DataFrame, label: str = "all") -> dict[str, float | str]:
    d = df.dropna(subset=["implied"]).copy()
    d["excess"] = d["win"] - d["implied"]
    ex = cluster_bootstrap(d, "excess")
    tk = cluster_bootstrap(d, "taker_net")
    mk = cluster_bootstrap(d, "maker_net")
    return {
        "slice": label,
        "sweeps": int(len(d)),
        "markets": int(d["ticker"].nunique()),
        "win_rate": float(d["win"].mean()) if len(d) else np.nan,
        "implied": float(d["implied"].mean()) if len(d) else np.nan,
        "excess": ex[0],
        "excess_lo": ex[1],
        "excess_hi": ex[2],
        "taker_net": tk[0],
        "taker_lo": tk[1],
        "taker_hi": tk[2],
        "maker_fill": float(d["maker_filled"].mean()) if len(d) else np.nan,
        "maker_net": mk[0],
        "maker_lo": mk[1],
        "maker_hi": mk[2],
    }


def whales(data: WhaleData, threshold: float) -> pd.DataFrame:
    return data.sweeps[data.sweeps["notional"] >= threshold]


def threshold_ladder(data: WhaleData, thresholds: tuple[float, ...] = THRESHOLDS) -> pd.DataFrame:
    return pd.DataFrame([summarize(whales(data, t), f">= ${t:,.0f}") for t in thresholds])


def split_by(df: pd.DataFrame, column: str, bins: tuple[float, ...] | None = None) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    if bins is not None:
        key = pd.cut(df[column], bins=list(bins), right=False)
    else:
        key = df[column]
    rows = [summarize(g, str(k)) for k, g in df.groupby(key, observed=True, dropna=True)]
    return pd.DataFrame(rows)


def spot_conditioning(df: pd.DataFrame) -> pd.DataFrame:
    d = df.dropna(subset=["spot", "implied"]).copy()
    if d.empty:
        return pd.DataFrame()
    rows = []
    for label, g in (
        ("whale agrees with spot", d[d["agrees_with_spot"] == 1]),
        ("whale disagrees with spot", d[d["agrees_with_spot"] == 0]),
    ):
        rows.append(summarize(g, label))
    # what you'd get by ignoring the whale and trading spot at the same moments
    s = d.copy()
    s["win"] = s["spot_win"]
    s["implied"] = s["spot_implied"]
    s["taker_net"] = np.nan
    s["maker_net"] = np.nan
    rows.append(summarize(s, "spot side at same moments (no whale)"))
    return pd.DataFrame(rows)


def time_split(df: pd.DataFrame, train_frac: float = 0.7) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    order = df.groupby("ticker")["close_ts"].first().sort_values()
    cut = int(len(order) * train_frac)
    train_tickers = set(order.index[:cut])
    return pd.DataFrame(
        [
            summarize(df[df["ticker"].isin(train_tickers)], f"first {train_frac:.0%} of markets"),
            summarize(
                df[~df["ticker"].isin(train_tickers)], f"last {1 - train_frac:.0%} of markets"
            ),
        ]
    )


def verdict(data: WhaleData, threshold: float) -> str:
    w = whales(data, threshold).dropna(subset=["implied"])
    if len(w) < MIN_SWEEPS_FOR_VERDICT:
        return (
            f"INCONCLUSIVE: {len(w)} whale sweeps at >= ${threshold:,.0f}; "
            f"the gate is {MIN_SWEEPS_FOR_VERDICT}. Keep recording."
        )
    ts = time_split(w)
    test = ts.iloc[1]
    if test["sweeps"] < MIN_SWEEPS_FOR_VERDICT * 0.3:
        return f"INCONCLUSIVE: only {test['sweeps']} sweeps in the held-out 30%. Keep recording."
    if test["taker_net"] >= VIABLE_NET_PER_CONTRACT and test["taker_lo"] > 0:
        return (
            f"VIABLE (taker copy): held-out net {test['taker_net']:+.3f}/contract, "
            f"95% CI [{test['taker_lo']:+.3f}, {test['taker_hi']:+.3f}]. "
            "Treat as provisional until a second batch confirms."
        )
    if test["excess_lo"] > 0:
        return (
            f"NOT VIABLE AS TRADED, BUT INFORMATIVE: whales beat the implied probability "
            f"(excess {test['excess']:+.3f}, CI [{test['excess_lo']:+.3f}, "
            f"{test['excess_hi']:+.3f}]) yet copying them nets "
            f"{test['taker_net']:+.3f}/contract after fees."
        )
    return (
        f"NOT VIABLE: held-out excess {test['excess']:+.3f} "
        f"(CI [{test['excess_lo']:+.3f}, {test['excess_hi']:+.3f}]), "
        f"taker net {test['taker_net']:+.3f}/contract."
    )


# ---------------------------------------------------------------- report


def report(data: WhaleData, threshold: float = 1000.0) -> str:
    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.float_format", lambda v: f"{v:,.3f}")
    out: list[str] = []
    s = data.sweeps
    out.append("== data")
    out.append(
        f"settled markets: {data.n_markets}   sweeps: {len(s):,}   "
        f"with book context: {int(s['implied'].notna().sum()):,}   "
        f"with spot: {int(s['spot'].notna().sum()):,}"
    )
    if len(s):
        q = s["notional"].quantile([0.5, 0.9, 0.99, 1.0])
        out.append(
            "sweep notional $: median {:,.0f}  p90 {:,.0f}  p99 {:,.0f}  max {:,.0f}".format(*q)
        )
        out.append(f"sweeps >= ${threshold:,.0f}: {int((s['notional'] >= threshold).sum()):,}")
    if s.empty:
        return "\n".join(out)

    w = whales(data, threshold)
    out.append("\n== verdict (pre-registered, see docs/research-brief.md)")
    out.append(verdict(data, threshold))
    out.append(
        "\nexcess = win rate minus market-implied probability of the whale's side at the print;"
        "\nCIs are bootstrapped over markets, not prints; nets are per contract after fees."
    )

    out.append("\n== threshold ladder")
    out.append(threshold_ladder(data).to_string(index=False))

    out.append(f"\n== time-ordered validation split at >= ${threshold:,.0f}")
    ts = time_split(w)
    out.append(ts.to_string(index=False) if not ts.empty else "(no data)")

    out.append(f"\n== spot conditioning at >= ${threshold:,.0f}")
    sc = spot_conditioning(w)
    out.append(sc.to_string(index=False) if not sc.empty else "(no spot data)")

    out.append(f"\n== by seconds to close at >= ${threshold:,.0f}")
    out.append(split_by(w, "secs_to_close", TTC_BINS).to_string(index=False))

    out.append(f"\n== by series at >= ${threshold:,.0f}")
    out.append(split_by(w, "series_ticker").to_string(index=False))

    out.append(f"\n== by aggressor book side at >= ${threshold:,.0f}  (ask = taker bought)")
    bs = split_by(w.dropna(subset=["taker_book_side"]), "taker_book_side")
    out.append(bs.to_string(index=False) if not bs.empty else "(no data)")

    out.append("\n== relative size (sweep count / market median), all sweeps")
    out.append(split_by(s, "rel_size", (0, 5, 20, 50, 200, float("inf"))).to_string(index=False))
    return "\n".join(out)
