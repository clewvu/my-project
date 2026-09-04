"""Fair-value hypothesis test.

Hypothesis (docs/research-brief.md, section 3): a driftless diffusion model of
spot, fed with realised volatility, prices the 15-minute contract better than
the book does, often enough to pay the taker fee. Like the whale test, this
module treats it as something to falsify.

Model
-----
Settlement is YES when the one-minute average of the CF Benchmarks index over
the last minute before close is at least the strike K. Take spot S to follow a
driftless geometric Brownian motion with volatility sigma per square-root
second. The log of that average is then approximately normal with mean ln S
and variance sigma^2 * tau_eff, where, with t seconds to close,

    tau_eff = (t - 60) + 60 / 3     outside the settlement minute
    tau_eff = t / 3                 inside it

because the mean of a Brownian path over an interval carries one third of the
interval's variance. This is the "averaging adjustment" the brief left for
later; it is continuous at t = 60 and small until the last minutes. So

    p_up = Phi( ln(S / K) / (sigma * sqrt(tau_eff)) )

Volatility is the root mean square of 5-second log returns of spot over the
last 30 or 60 minutes (both are tested; the choice is fitted on the training
split only).

Trade rule
----------
At every snapshot from the market's open until ``MIN_TTC_S`` seconds before
close (the risk framework's no-entry window), buy one contract of the side
whose fair value exceeds its ask by more than the taker fee plus a margin. At
most one entry per market, at the first snapshot that qualifies. The fill is
the ask on the *next* snapshot, about five seconds later, which is the same
convention as the whale copy trade. Hold to settlement. A maker variant rests
one tick inside the spread and is filled only if a later print trades through.

Statistics
----------
Same as the whale test: ``excess = win - implied`` where implied is the
market's mid for our side at the signal; confidence intervals bootstrap over
*windows* (close time), because a BTC and a DOGE market closing at the same
time share the same crypto move; time-ordered 70/30 split with the model's
free parameters (vol window, margin) fitted on the first 70%; the same
sufficiency gate and viability threshold. Both series are pooled and reported
separately.

The known risk is the basis between Coinbase spot and the settlement index.
``basis`` measures it from ``expiration_value``.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import analysis
from .analysis import SPOT_SYMBOLS, Dataset, at_horizon
from .fees import TAKER_RATE
from .models import TICK
from .whale import _attach_maker_fills, cluster_bootstrap

VOL_WINDOWS = (1800, 3600)  # seconds of spot history behind sigma
VOL_STEP_S = 5.0  # sampling grid for log returns
VOL_MIN_FRACTION = 0.5  # of the window that must have returns
SIGMA_TOLERANCE_S = 60.0  # how stale a sigma may be when attached to a snapshot
MARGINS = (0.0, 0.01, 0.02, 0.03, 0.05)  # edge required beyond the fee, dollars
MIN_TTC_S = 120.0  # no entries closer to close than this
FILL_LAG = 1  # fill at the ask this many snapshots after the signal
MAX_PRICE = 0.95
SETTLEMENT_WINDOW_S = 60.0
MIN_TRADES_FOR_VERDICT = 200
MIN_TRAIN_TRADES = 30  # a configuration needs this many training trades to be selectable
VIABLE_NET_PER_CONTRACT = 0.01
TRAIN_FRACTION = 0.7
HORIZONS = (600, 300, 180, 120)
GAP_BINS = (-1.0, -0.10, -0.05, -0.02, 0.0, 0.02, 0.05, 0.10, 1.0)
TTC_BINS = (120, 300, 600, 900, float("inf"))
SECONDS_PER_YEAR = 365 * 86400

TRADE_COLUMNS = [
    "ticker",
    "series_ticker",
    "window",
    "close_ts",
    "signal_ts",
    "ts",
    "secs_to_close",
    "spot",
    "strike",
    "sigma",
    "p_model",
    "side",
    "edge",
    "implied",
    "ask",
    "fee",
    "win",
    "gross",
    "net",
    "rest_price",
]

_erf = np.vectorize(math.erf, otypes=[float])


# ---------------------------------------------------------------- model


def norm_cdf(x: object) -> np.ndarray:
    """Standard normal CDF, elementwise, without scipy."""
    return 0.5 * (1.0 + _erf(np.asarray(x, dtype=float) / math.sqrt(2.0)))


def effective_tau(secs_to_close: object) -> np.ndarray:
    """Variance-equivalent horizon (seconds) for the settlement average, floored at 1 s."""
    t = np.asarray(secs_to_close, dtype=float)
    outside = np.maximum(t - SETTLEMENT_WINDOW_S, 0.0) + SETTLEMENT_WINDOW_S / 3.0
    inside = np.maximum(t, 0.0) / 3.0
    return np.maximum(np.where(t >= SETTLEMENT_WINDOW_S, outside, inside), 1.0)


def fair_value(spot: object, strike: object, sigma: object, secs_to_close: object) -> np.ndarray:
    """Probability that the settlement average is at or above the strike."""
    tau = effective_tau(secs_to_close)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.log(np.asarray(spot, dtype=float) / np.asarray(strike, dtype=float))
        z = z / (np.asarray(sigma, dtype=float) * np.sqrt(tau))
    return norm_cdf(z)


def realized_vol(
    spot: pd.DataFrame,
    symbol: str,
    window_s: float,
    step_s: float = VOL_STEP_S,
    min_fraction: float = VOL_MIN_FRACTION,
) -> pd.DataFrame:
    """Rolling RMS of ``step_s``-second log returns, per square-root second.

    Spot is sampled onto a regular grid with the last observation at each grid
    point (gaps longer than three steps become NaN, not stale values). Returns
    a frame of ``ts`` and ``sigma`` wherever at least ``min_fraction`` of the
    window had returns.
    """
    s = spot[spot["symbol"] == symbol][["ts", "price"]].dropna().sort_values("ts")
    s = s[s["price"] > 0]
    empty = pd.DataFrame({"ts": pd.Series(dtype=float), "sigma": pd.Series(dtype=float)})
    if len(s) < 3:
        return empty
    start = math.floor(s["ts"].iloc[0] / step_s) * step_s
    grid = pd.DataFrame({"ts": np.arange(start, s["ts"].iloc[-1] + step_s, step_s)})
    g = pd.merge_asof(grid, s, on="ts", direction="backward", tolerance=3 * step_s)
    r2 = np.log(g["price"]).diff() ** 2
    n = max(2, int(round(window_s / step_s)))
    var = r2.rolling(n, min_periods=max(2, int(n * min_fraction))).mean()
    g["sigma"] = np.sqrt(var / step_s)
    return g[["ts", "sigma"]].dropna().reset_index(drop=True)


# ---------------------------------------------------------------- loading


@dataclass
class FairValueData:
    snapshots: pd.DataFrame  # settled markets only, with sigma_* and p_* columns
    markets: pd.DataFrame
    spot: pd.DataFrame
    trades: pd.DataFrame | None = None  # public prints, for the maker fill model
    vol_windows: tuple[int, ...] = VOL_WINDOWS
    notes: list[str] = field(default_factory=list)

    @property
    def settled_markets(self) -> pd.DataFrame:
        return self.markets[self.markets["won_yes"].notna()]


def load(
    db_path: str, series: list[str] | None = None, vol_windows: tuple[int, ...] = VOL_WINDOWS
) -> FairValueData:
    ds = analysis.load(db_path, series=series)
    con = sqlite3.connect(db_path)
    try:
        trades = pd.read_sql("SELECT ticker, ts, yes_price, no_price FROM trades", con)
    finally:
        con.close()
    trades = trades[trades["ticker"].isin(ds.markets["ticker"])].dropna(subset=["ts"])
    return prepare(ds, trades=trades, vol_windows=vol_windows)


def prepare(
    ds: Dataset, trades: pd.DataFrame | None = None, vol_windows: tuple[int, ...] = VOL_WINDOWS
) -> FairValueData:
    """Attach realised volatility and model probabilities to every settled snapshot."""
    snaps = ds.settled.copy()
    snaps = attach_sigma(snaps, ds.spot, vol_windows)
    for w in vol_windows:
        snaps[f"p_{w}"] = fair_value(
            snaps["spot"], snaps["strike"], snaps[f"sigma_{w}"], snaps["secs_to_close"]
        )
    snaps["window"] = snaps["close_ts"]
    snaps = snaps.sort_values(["ticker", "ts"]).reset_index(drop=True)
    markets = ds.markets.copy()
    if "expiration_value" not in markets.columns:
        markets["expiration_value"] = np.nan
    return FairValueData(
        snapshots=snaps, markets=markets, spot=ds.spot, trades=trades, vol_windows=vol_windows
    )


def attach_sigma(
    snaps: pd.DataFrame, spot: pd.DataFrame, vol_windows: tuple[int, ...]
) -> pd.DataFrame:
    snaps = snaps.copy()
    for w in vol_windows:
        snaps[f"sigma_{w}"] = np.nan
    if snaps.empty or spot.empty:
        return snaps
    parts = []
    for series, g in snaps.groupby("series_ticker", sort=False):
        symbol = SPOT_SYMBOLS.get(str(series))
        g = g.sort_values("ts")
        if symbol is None:
            parts.append(g)
            continue
        for w in vol_windows:
            vol = realized_vol(spot, symbol, w)
            g = g.drop(columns=[f"sigma_{w}"])
            if vol.empty:
                g[f"sigma_{w}"] = np.nan
                continue
            g = pd.merge_asof(
                g,
                vol.rename(columns={"sigma": f"sigma_{w}"}),
                on="ts",
                direction="backward",
                tolerance=SIGMA_TOLERANCE_S,
            )
        parts.append(g)
    return pd.concat(parts, ignore_index=True)


# ---------------------------------------------------------------- backtest


def backtest(
    fv: FairValueData,
    vol_window: int = VOL_WINDOWS[0],
    margin: float = 0.02,
    min_ttc: float = MIN_TTC_S,
    fill_lag: int = FILL_LAG,
    max_price: float = MAX_PRICE,
    rate: float = TAKER_RATE,
) -> pd.DataFrame:
    """One row per market that traded: the first snapshot where the rule fired.

    ``edge`` is fair value minus ask minus fee on the better side at the
    signal; the fill is that side's ask ``fill_lag`` snapshots later.
    """
    s = fv.snapshots
    if s.empty or f"p_{vol_window}" not in s.columns:
        return pd.DataFrame(columns=TRADE_COLUMNS)
    p = s[f"p_{vol_window}"]
    edge_yes = p - s["yes_ask"] - rate * s["yes_ask"] * (1 - s["yes_ask"])
    edge_no = (1 - p) - s["no_ask"] - rate * s["no_ask"] * (1 - s["no_ask"])
    yes_better = (edge_yes >= edge_no) | edge_no.isna()
    edge = edge_yes.where(yes_better, edge_no)
    ask_now = s["yes_ask"].where(yes_better, s["no_ask"])
    eligible = (s["secs_to_close"] >= min_ttc) & p.notna() & (ask_now <= max_price)
    fire = eligible & (edge >= margin)

    g = s.groupby("ticker", sort=False)
    lag = max(0, int(fill_lag))
    df = s.assign(
        side=np.where(yes_better, "yes", "no"),
        edge=edge,
        p_model=p,
        sigma=s[f"sigma_{vol_window}"],
        fill_ts=g["ts"].shift(-lag),
        fill_yes_ask=g["yes_ask"].shift(-lag),
        fill_no_ask=g["no_ask"].shift(-lag),
        fill_yes_bid=g["yes_bid"].shift(-lag),
        fill_no_bid=g["no_bid"].shift(-lag),
    )[fire]
    first = df.groupby("ticker", sort=False).head(1)
    if first.empty:
        return pd.DataFrame(columns=TRADE_COLUMNS)

    yes = first["side"] == "yes"
    t = pd.DataFrame(
        {
            "ticker": first["ticker"],
            "series_ticker": first["series_ticker"],
            "window": first["window"],
            "close_ts": first["close_ts"],
            "signal_ts": first["ts"],
            "ts": first["fill_ts"],
            "secs_to_close": first["secs_to_close"],
            "spot": first["spot"],
            "strike": first["strike"],
            "sigma": first["sigma"],
            "p_model": first["p_model"].where(yes, 1 - first["p_model"]),
            "side": first["side"],
            "edge": first["edge"],
            "implied": first["yes_mid"].where(yes, 1 - first["yes_mid"]),
            "ask": first["fill_yes_ask"].where(yes, first["fill_no_ask"]),
            "win": np.where(yes, first["won_yes"], 1 - first["won_yes"]).astype(float),
            "rest_price": np.round(
                first["fill_yes_bid"].where(yes, first["fill_no_bid"]) + TICK, 4
            ),
        }
    ).dropna(subset=["ask", "ts"])
    t["fee"] = rate * t["ask"] * (1 - t["ask"])
    t["gross"] = t["win"] * (1 - t["ask"]) - (1 - t["win"]) * t["ask"]
    t["net"] = t["gross"] - t["fee"]
    t = t[TRADE_COLUMNS].reset_index(drop=True)
    return _attach_maker(t, fv.trades)


def _attach_maker(t: pd.DataFrame, trades: pd.DataFrame | None) -> pd.DataFrame:
    t = t.copy()
    if trades is None or trades.empty or t.empty:
        t["maker_filled"] = np.nan
        t["maker_net"] = np.nan
        return t
    t = _attach_maker_fills(t, trades)
    yes = t["side"] == "yes"
    future_min = np.where(yes, t["future_min_yes"], t["future_min_no"])
    t["maker_filled"] = (future_min <= t["rest_price"]).astype(float)
    t["maker_net"] = np.where(
        t["maker_filled"] == 1,
        t["win"] * (1 - t["rest_price"]) - (1 - t["win"]) * t["rest_price"],
        np.nan,
    )
    return t.drop(columns=["future_min_yes", "future_min_no"])


# ---------------------------------------------------------------- statistics


def summarize(trades: pd.DataFrame, label: str = "all") -> dict[str, float | str]:
    d = trades.dropna(subset=["implied", "win"]).copy()
    d["excess"] = d["win"] - d["implied"]
    ex = cluster_bootstrap(d, "excess", cluster="window")
    tk = cluster_bootstrap(d, "net", cluster="window")
    mk = cluster_bootstrap(d, "maker_net", cluster="window") if "maker_net" in d else (np.nan,) * 3
    n = len(d)
    return {
        "slice": label,
        "trades": int(n),
        "windows": int(d["window"].nunique()),
        "win_rate": float(d["win"].mean()) if n else np.nan,
        "avg_ask": float(d["ask"].mean()) if n else np.nan,
        "implied": float(d["implied"].mean()) if n else np.nan,
        "model_p": float(d["p_model"].mean()) if n else np.nan,
        "excess": ex[0],
        "excess_lo": ex[1],
        "excess_hi": ex[2],
        "taker_net": tk[0],
        "taker_lo": tk[1],
        "taker_hi": tk[2],
        "maker_fill": float(d["maker_filled"].mean()) if n and "maker_filled" in d else np.nan,
        "maker_net": mk[0],
        "maker_lo": mk[1],
        "maker_hi": mk[2],
    }


def split_tickers(fv: FairValueData, train_frac: float = TRAIN_FRACTION) -> tuple[set, set]:
    """Time-ordered split of settled markets by close time, fixed before any fitting."""
    m = fv.settled_markets.sort_values("close_ts")
    cut = int(len(m) * train_frac)
    tickers = list(m["ticker"])
    return set(tickers[:cut]), set(tickers[cut:])


def grid(
    fv: FairValueData,
    tickers: set | None = None,
    vol_windows: tuple[int, ...] | None = None,
    margins: tuple[float, ...] = MARGINS,
    min_ttc: float = MIN_TTC_S,
) -> pd.DataFrame:
    rows = []
    for w in vol_windows or fv.vol_windows:
        for m in margins:
            t = backtest(fv, w, m, min_ttc=min_ttc)
            if tickers is not None:
                t = t[t["ticker"].isin(tickers)]
            rows.append({"vol_window": w, "margin": m, **summarize(t, f"w={w} m={m:.2f}")})
    out = pd.DataFrame(rows)
    return out.drop(columns=["slice"]) if not out.empty else out


def select_config(
    fv: FairValueData, train: set, min_ttc: float = MIN_TTC_S
) -> tuple[int, float] | None:
    """Pick (vol_window, margin) on the training split by the lower CI bound of taker net.

    The lower bound rather than the point estimate, so a high margin that
    fired a handful of times cannot win on luck. None when no configuration
    produced ``MIN_TRAIN_TRADES`` training trades.
    """
    g = grid(fv, tickers=train, min_ttc=min_ttc)
    if g.empty:
        return None
    g = g[g["trades"] >= MIN_TRAIN_TRADES].dropna(subset=["taker_lo"])
    if g.empty:
        return None
    best = g.sort_values(["taker_lo", "taker_net"], ascending=False).iloc[0]
    return int(best["vol_window"]), float(best["margin"])


def time_split(
    fv: FairValueData, trades: pd.DataFrame, train_frac: float = TRAIN_FRACTION
) -> pd.DataFrame:
    train, test = split_tickers(fv, train_frac)
    return pd.DataFrame(
        [
            summarize(trades[trades["ticker"].isin(train)], f"first {train_frac:.0%} of markets"),
            summarize(trades[trades["ticker"].isin(test)], f"last {1 - train_frac:.0%} of markets"),
        ]
    )


def verdict(fv: FairValueData, min_ttc: float = MIN_TTC_S) -> tuple[str, tuple[int, float] | None]:
    """Pre-registered verdict and the configuration it was judged on."""
    train, test = split_tickers(fv)
    cfg = select_config(fv, train, min_ttc=min_ttc)
    if cfg is None:
        return (
            f"INCONCLUSIVE: no (vol window, margin) produced {MIN_TRAIN_TRADES} trades on the "
            f"training 70% of {len(train)} markets. Keep recording.",
            None,
        )
    w, m = cfg
    t = backtest(fv, w, m, min_ttc=min_ttc)
    held = t[t["ticker"].isin(test)]
    label = f"vol window {w}s, margin {m:.2f}"
    if len(t) < MIN_TRADES_FOR_VERDICT:
        return (
            f"INCONCLUSIVE: {len(t)} trades at {label}; the gate is "
            f"{MIN_TRADES_FOR_VERDICT}. Keep recording.",
            cfg,
        )
    if len(held) < round(MIN_TRADES_FOR_VERDICT * (1 - TRAIN_FRACTION)):
        return f"INCONCLUSIVE: only {len(held)} trades in the held-out 30%. Keep recording.", cfg
    s = summarize(held)
    if s["taker_net"] >= VIABLE_NET_PER_CONTRACT and s["taker_lo"] > 0:
        return (
            f"VIABLE (taker) at {label}: held-out net {s['taker_net']:+.3f}/contract, "
            f"95% CI [{s['taker_lo']:+.3f}, {s['taker_hi']:+.3f}] over {s['trades']} trades. "
            "Treat as provisional until a second batch confirms.",
            cfg,
        )
    if s["excess_lo"] > 0:
        return (
            f"NOT VIABLE AS TRADED, BUT INFORMATIVE at {label}: the model beats the implied "
            f"probability (excess {s['excess']:+.3f}, CI [{s['excess_lo']:+.3f}, "
            f"{s['excess_hi']:+.3f}]) yet trading it nets {s['taker_net']:+.3f}/contract "
            "after fees.",
            cfg,
        )
    return (
        f"NOT VIABLE at {label}: held-out excess {s['excess']:+.3f} "
        f"(CI [{s['excess_lo']:+.3f}, {s['excess_hi']:+.3f}]), "
        f"taker net {s['taker_net']:+.3f}/contract.",
        cfg,
    )


# ---------------------------------------------------------------- diagnostics


def sigma_summary(fv: FairValueData) -> pd.DataFrame:
    """Annualised realised volatility by series, and how many snapshots have a model value."""
    rows = []
    for series, g in fv.snapshots.groupby("series_ticker"):
        for w in fv.vol_windows:
            ann = g[f"sigma_{w}"] * math.sqrt(SECONDS_PER_YEAR)
            rows.append(
                {
                    "series": series,
                    "vol_window": w,
                    "snapshots": len(g),
                    "with_model": int(g[f"p_{w}"].notna().sum()),
                    "ann_vol_p10": ann.quantile(0.1),
                    "ann_vol_median": ann.median(),
                    "ann_vol_p90": ann.quantile(0.9),
                }
            )
    return pd.DataFrame(rows)


def model_brier(fv: FairValueData, horizons: tuple[int, ...] = HORIZONS) -> pd.DataFrame:
    """Brier score of the model against the market's mid, by horizon."""
    rows = []
    for h in horizons:
        df = at_horizon(fv.snapshots, h).dropna(subset=["yes_mid"])
        if df.empty:
            continue
        row: dict[str, float] = {"horizon_s": h, "n": len(df)}
        row["market"] = float(((df["yes_mid"] - df["won_yes"]) ** 2).mean())
        for w in fv.vol_windows:
            d = df.dropna(subset=[f"p_{w}"])
            if d.empty:
                continue
            row[f"model_{w}"] = float(((d[f"p_{w}"] - d["won_yes"]) ** 2).mean())
            row[f"market_on_same_{w}"] = float(((d["yes_mid"] - d["won_yes"]) ** 2).mean())
        rows.append(row)
    return pd.DataFrame(rows)


def gap_signal(fv: FairValueData, horizon: float, vol_window: int) -> pd.DataFrame:
    """Does model-minus-market predict settlement beyond the market?

    Bucketed by the gap; ``excess`` is realised YES rate minus the market's
    mid. Under the null (the book already knows everything the model knows)
    excess is flat at zero across buckets; a model with information shows
    positive excess for positive gaps and negative for negative ones.
    """
    df = at_horizon(fv.snapshots, horizon).dropna(subset=["yes_mid", f"p_{vol_window}"])
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["gap"] = df[f"p_{vol_window}"] - df["yes_mid"]
    bucket = pd.cut(df["gap"], bins=list(GAP_BINS), right=False).rename("gap_bucket")
    out = (
        df.groupby(bucket, observed=True)
        .agg(
            n=("gap", "size"),
            mean_gap=("gap", "mean"),
            implied=("yes_mid", "mean"),
            realised=("won_yes", "mean"),
        )
        .reset_index()
    )
    out["excess"] = out["realised"] - out["implied"]
    return out


def basis(fv: FairValueData) -> pd.DataFrame:
    """Coinbase spot versus the settlement index, per series.

    ``avg_bps`` compares the settlement value with the mean Coinbase price over
    the final minute; ``last_bps`` with the last Coinbase print before close.
    ``sign_mismatch`` counts markets where the Coinbase minute-average was on
    the other side of the strike from the result: the cases where the basis
    alone decided the outcome.
    """
    m = fv.settled_markets.dropna(subset=["expiration_value", "close_ts", "strike"])
    if m.empty or fv.spot.empty:
        return pd.DataFrame()
    rows = []
    for series, g in m.groupby("series_ticker"):
        symbol = SPOT_SYMBOLS.get(str(series))
        s = fv.spot[fv.spot["symbol"] == symbol].sort_values("ts")
        if s.empty:
            continue
        ts = s["ts"].to_numpy()
        px = s["price"].to_numpy()
        avg_bps, last_bps, mismatch = [], [], 0
        for _, mk in g.iterrows():
            lo = np.searchsorted(ts, mk["close_ts"] - SETTLEMENT_WINDOW_S, side="left")
            hi = np.searchsorted(ts, mk["close_ts"], side="right")
            if hi <= lo or hi == 0:
                continue
            avg = float(px[lo:hi].mean())
            last = float(px[hi - 1])
            avg_bps.append((mk["expiration_value"] - avg) / avg * 1e4)
            last_bps.append((mk["expiration_value"] - last) / last * 1e4)
            if (avg >= mk["strike"]) != (mk["won_yes"] == 1.0):
                mismatch += 1
        if not avg_bps:
            continue
        a = pd.Series(avg_bps)
        rows.append(
            {
                "series": series,
                "n": len(a),
                "avg_bps_mean": a.mean(),
                "avg_bps_std": a.std(),
                "avg_bps_median_abs": a.abs().median(),
                "last_bps_mean": pd.Series(last_bps).mean(),
                "sign_mismatch": mismatch,
            }
        )
    return pd.DataFrame(rows)


def split_by(
    trades: pd.DataFrame, column: str, bins: tuple[float, ...] | None = None
) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    key = pd.cut(trades[column], bins=list(bins), right=False) if bins else trades[column]
    rows = [summarize(g, str(k)) for k, g in trades.groupby(key, observed=True, dropna=True)]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- report


def report(fv: FairValueData, min_ttc: float = MIN_TTC_S, show_trades: int = 0) -> str:
    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.float_format", lambda v: f"{v:,.3f}")
    out: list[str] = []
    s = fv.snapshots
    n_settled = len(fv.settled_markets)
    out.append("== data")
    out.append(
        f"settled markets: {n_settled}   snapshots: {len(s):,}   "
        f"with spot: {int(s['spot'].notna().sum()):,}   "
        f"with model: {int(s[f'p_{fv.vol_windows[0]}'].notna().sum()) if len(s) else 0:,}"
    )
    if n_settled < 20:
        out.append(
            f"\nOnly {n_settled} settled markets; keep recording. "
            "Statistics below are not meaningful under ~100."
        )
    if n_settled == 0 or s.empty:
        return "\n".join(out)

    out.append("\n== realised volatility (annualised) and model coverage")
    out.append(sigma_summary(fv).to_string(index=False))

    out.append("\n== basis: settlement index vs Coinbase (the known risk)")
    b = basis(fv)
    out.append(b.to_string(index=False) if not b.empty else "(no expiration_value yet)")

    out.append("\n== brier score by horizon: model vs market mid (lower is better)")
    mb = model_brier(fv)
    out.append(mb.to_string(index=False) if not mb.empty else "(no data)")

    w0 = fv.vol_windows[0]
    for h in (300, 120):
        out.append(f"\n== gap signal at T-{h}s, vol window {w0}s (model minus market mid)")
        gs = gap_signal(fv, h, w0)
        out.append(gs.to_string(index=False) if not gs.empty else "(no data)")

    out.append("\n== verdict (pre-registered, see docs/research-brief.md section 3)")
    text, cfg = verdict(fv, min_ttc=min_ttc)
    out.append(text)
    out.append(
        "\nexcess = win rate minus market-implied probability of our side at the signal;"
        "\nCIs are bootstrapped over 15-minute windows; nets are per contract after fees;"
        f"\nfills at the next snapshot's ask; no entries under {min_ttc:.0f}s to close."
    )

    train, test = split_tickers(fv)
    out.append(f"\n== training grid (first 70%: {len(train)} markets), used to pick the config")
    g = grid(fv, tickers=train, min_ttc=min_ttc)
    cols = [
        "vol_window",
        "margin",
        "trades",
        "win_rate",
        "avg_ask",
        "excess",
        "taker_net",
        "taker_lo",
        "taker_hi",
        "maker_fill",
        "maker_net",
    ]
    out.append(g[cols].to_string(index=False) if not g.empty else "(no data)")

    if cfg is not None:
        w, m = cfg
        t = backtest(fv, w, m, min_ttc=min_ttc)
        out.append(f"\n== time-ordered validation split at vol window {w}s, margin {m:.2f}")
        out.append(time_split(fv, t).to_string(index=False))
        out.append("\n== by series (held-out 30% only)")
        held = t[t["ticker"].isin(test)]
        bs = split_by(held, "series_ticker")
        out.append(bs.to_string(index=False) if not bs.empty else "(no data)")
        out.append("\n== by seconds to close at the signal (all trades)")
        out.append(split_by(t, "secs_to_close", TTC_BINS).to_string(index=False))
        if show_trades:
            out.append(f"\n== last {show_trades} trades at the selected config")
            cols_t = [
                "ticker",
                "secs_to_close",
                "side",
                "spot",
                "strike",
                "p_model",
                "implied",
                "ask",
                "win",
                "net",
            ]
            out.append(t.tail(show_trades)[cols_t].to_string(index=False))
    return "\n".join(out)
