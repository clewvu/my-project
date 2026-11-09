"""Cross-asset co-movement and lead-lag analysis across 15-minute series.

The claim under test: outcomes of related markets (BTC/ETH/SOL, gold/silver,
oil vs metals) move together, so one series can predict another. Co-movement
is cheap to show and nearly useless on its own: if ETH and BTC finish the
same window up together, that is *contemporaneous* correlation, which
BTC's own price already reflects. A predictor must add information about B's
settlement beyond what B's own market price says at decision time.

So the report runs in order of increasing usefulness:

1. coverage: which series overlap in which windows.
2. outcome co-movement: pairwise agreement and phi correlation of settled
   outcomes, bootstrapped over windows. Confirms or refutes "they follow each
   other" and the sign of each hypothesis.
3. spot lead-lag: cross-correlation of short spot returns at lags. If the
   peak is at lag 0, nothing leads; if the leader's past returns correlate
   with the follower's future returns, there is something to exploit.
4. predictor test, per hypothesis and horizon: does the leader's state add
   information about the follower's outcome *given the follower's implied
   probability*? Bucket test (realised minus implied by leader state) and a
   logistic regression of outcome on logit(implied) + leader feature, with
   bootstrap CIs over windows.
5. divergence backtest: buy the follower's side favoured by the leader's
   market when the two markets disagree by more than a threshold, at the ask,
   fee-inclusive, held to settlement.

Windows are the unit of resampling throughout: every series settles on the
same quarter-hour grid, so a window is one draw of "the world".
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .analysis import at_horizon, spot_for_series
from .fees import fee_per_contract
from .universe import Hypothesis, Universe, load_universe

HORIZONS = (600, 300, 120, 60)
MIN_WINDOWS = 30
BOOTSTRAP_ROUNDS = 2000
GRID_S = 5.0
LAGS_S = tuple(range(-60, 61, 5))
RETURN_STEP_S = 10.0


@dataclass
class CrossData:
    universe: Universe
    markets: pd.DataFrame  # settled markets, one row per (window, series)
    outcomes: pd.DataFrame  # windows x series -> 1/0
    implied: dict[int, pd.DataFrame]  # horizon -> windows x series -> yes mid
    asks: dict[int, pd.DataFrame]  # horizon -> windows x series -> (yes_ask, no_ask) frames
    spot: dict[str, pd.DataFrame]  # series -> ts/price
    strikes: pd.DataFrame  # windows x series -> strike
    opens: pd.DataFrame  # windows x series -> open_ts
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- loading


def load(
    db_path: str, universe: Universe | None = None, horizons: tuple[int, ...] = HORIZONS
) -> CrossData:
    universe = universe or load_universe()
    con = sqlite3.connect(db_path)
    try:
        markets = pd.read_sql(
            "SELECT ticker, series_ticker, strike, open_ts, close_ts, result FROM markets "
            "WHERE result IN ('yes', 'no') AND close_ts IS NOT NULL",
            con,
        )
        snaps = pd.read_sql(
            "SELECT ts, ticker, secs_to_close, yes_bid, yes_ask, no_bid, no_ask FROM snapshots",
            con,
        )
        spot = pd.read_sql("SELECT ts, source, symbol, price FROM spot", con)
    finally:
        con.close()

    markets["window"] = (markets["close_ts"] / 900).round().astype("int64") * 900
    markets["won_yes"] = (markets["result"] == "yes").astype(float)
    markets = markets.drop_duplicates(["window", "series_ticker"])
    outcomes = markets.pivot(index="window", columns="series_ticker", values="won_yes")
    strikes = markets.pivot(index="window", columns="series_ticker", values="strike")
    opens = markets.pivot(index="window", columns="series_ticker", values="open_ts")

    snaps = snaps.merge(markets[["ticker", "series_ticker", "window"]], on="ticker", how="inner")
    snaps["yes_mid"] = (snaps["yes_bid"] + snaps["yes_ask"]) / 2
    implied: dict[int, pd.DataFrame] = {}
    asks: dict[int, pd.DataFrame] = {}
    for h in horizons:
        at = at_horizon(snaps, h)
        if at.empty:
            implied[h] = pd.DataFrame()
            asks[h] = pd.DataFrame()
            continue
        implied[h] = at.pivot(index="window", columns="series_ticker", values="yes_mid")
        asks[h] = at.set_index(["window", "series_ticker"])[["yes_ask", "no_ask", "ts"]]

    spot_by_series = {
        s: spot_for_series(spot, universe.spot_preference(s))
        for s in sorted(set(markets["series_ticker"]) | set(universe.series))
    }
    return CrossData(universe, markets, outcomes, implied, asks, spot_by_series, strikes, opens)


# ---------------------------------------------------------------- helpers


def bootstrap_mean(
    values: np.ndarray, rounds: int = BOOTSTRAP_ROUNDS, seed: int = 0
) -> tuple[float, float, float]:
    v = np.asarray(values, dtype=float)
    v = v[~np.isnan(v)]
    if len(v) == 0:
        return (np.nan, np.nan, np.nan)
    if len(v) < 3:
        return (float(v.mean()), np.nan, np.nan)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(v), size=(rounds, len(v)))
    means = v[idx].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return (float(v.mean()), float(lo), float(hi))


def phi(a: np.ndarray, b: np.ndarray) -> float:
    """Correlation of two binary series (Pearson on 0/1 values)."""
    if len(a) < 2 or a.std() == 0 or b.std() == 0:
        return np.nan
    return float(np.corrcoef(a, b)[0, 1])


def logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def logistic_fit(X: np.ndarray, y: np.ndarray, iters: int = 50, ridge: float = 1e-6) -> np.ndarray:
    """Newton / IRLS logistic regression with an intercept; returns coefficients."""
    X = np.column_stack([np.ones(len(y)), X])
    beta = np.zeros(X.shape[1])
    for _ in range(iters):
        z = np.clip(X @ beta, -30, 30)
        p = 1 / (1 + np.exp(-z))
        w = p * (1 - p)
        grad = X.T @ (y - p) - ridge * beta
        hess = (X * w[:, None]).T @ X + ridge * np.eye(X.shape[1])
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            break
        beta = beta + step
        if np.abs(step).max() < 1e-8:
            break
    return beta


def bootstrap_coef(
    X: np.ndarray, y: np.ndarray, col: int, rounds: int = 500, seed: int = 0
) -> tuple[float, float, float]:
    """Point estimate and 95% CI for coefficient ``col`` (0-based, excluding intercept)."""
    if len(y) < 10 or y.std() == 0:
        return (np.nan, np.nan, np.nan)
    point = logistic_fit(X, y)[col + 1]
    rng = np.random.default_rng(seed)
    coefs = []
    for _ in range(rounds):
        idx = rng.integers(0, len(y), len(y))
        if y[idx].std() == 0:
            continue
        coefs.append(logistic_fit(X[idx], y[idx])[col + 1])
    if len(coefs) < 20:
        return (float(point), np.nan, np.nan)
    lo, hi = np.percentile(coefs, [2.5, 97.5])
    return (float(point), float(lo), float(hi))


def _pairs(series: list[str]) -> list[tuple[str, str]]:
    return [(a, b) for i, a in enumerate(series) for b in series[i + 1 :]]


# ---------------------------------------------------------------- 1. coverage


def coverage(data: CrossData) -> pd.DataFrame:
    o = data.outcomes
    rows = []
    for s in o.columns:
        col = o[s].dropna()
        rows.append(
            {
                "series": s,
                "windows": len(col),
                "yes_rate": col.mean(),
                "spot_rows": len(data.spot.get(s, pd.DataFrame())),
            }
        )
    return pd.DataFrame(rows)


def overlap(data: CrossData) -> pd.DataFrame:
    o = data.outcomes
    return o.notna().astype(int).T @ o.notna().astype(int)


# ---------------------------------------------------------------- 2. outcome co-movement


def outcome_comovement(data: CrossData) -> pd.DataFrame:
    o = data.outcomes
    rows = []
    for a, b in _pairs(list(o.columns)):
        both = o[[a, b]].dropna()
        if len(both) < MIN_WINDOWS:
            continue
        agree = (both[a] == both[b]).astype(float).to_numpy()
        pt, lo, hi = bootstrap_mean(agree)
        rows.append(
            {
                "a": a,
                "b": b,
                "windows": len(both),
                "agree": pt,
                "agree_lo": lo,
                "agree_hi": hi,
                "phi": phi(both[a].to_numpy(), both[b].to_numpy()),
            }
        )
    return pd.DataFrame(rows).sort_values("agree", ascending=False) if rows else pd.DataFrame()


def hypothesis_signs(data: CrossData) -> pd.DataFrame:
    """For each declared hypothesis: does the realised sign match the claimed one?"""
    o = data.outcomes
    rows = []
    for h in data.universe.hypotheses:
        if h.leader not in o.columns or h.follower not in o.columns:
            rows.append(
                {
                    "hypothesis": h.name,
                    "claimed_sign": h.sign,
                    "windows": 0,
                    "phi": np.nan,
                    "verdict": "no data",
                }
            )
            continue
        both = o[[h.leader, h.follower]].dropna()
        if len(both) < MIN_WINDOWS:
            rows.append(
                {
                    "hypothesis": h.name,
                    "claimed_sign": h.sign,
                    "windows": len(both),
                    "phi": np.nan,
                    "verdict": "too few windows",
                }
            )
            continue
        agree = (both[h.leader] == both[h.follower]).astype(float).to_numpy()
        if h.sign < 0:
            agree = 1 - agree
        pt, lo, hi = bootstrap_mean(agree)
        p = phi(both[h.leader].to_numpy(), both[h.follower].to_numpy())
        if lo > 0.5:
            verdict = "sign confirmed"
        elif hi < 0.5:
            verdict = "sign REVERSED"
        else:
            verdict = "no co-movement detected"
        rows.append(
            {
                "hypothesis": h.name,
                "claimed_sign": h.sign,
                "windows": len(both),
                "agree_claimed_dir": pt,
                "lo": lo,
                "hi": hi,
                "phi": p,
                "verdict": verdict,
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 3. spot lead-lag


def _grid_returns(
    spot: pd.DataFrame, grid: float = GRID_S, step: float = RETURN_STEP_S
) -> pd.Series:
    if spot.empty:
        return pd.Series(dtype=float)
    s = spot.set_index(pd.to_datetime(spot["ts"], unit="s"))["price"].sort_index()
    s = s[~s.index.duplicated(keep="last")]
    g = s.resample(f"{int(grid * 1000)}ms").last().ffill(limit=int(60 / grid))
    k = max(1, int(round(step / grid)))
    return np.log(g).diff(k).dropna()


def spot_lead_lag(data: CrossData, lags: tuple[int, ...] = LAGS_S) -> pd.DataFrame:
    """corr(leader return ending at t - lag, follower return at t); positive lag = leader first."""
    rows = []
    cache: dict[str, pd.Series] = {}
    for h in data.universe.hypotheses:
        ra = cache.setdefault(h.leader, _grid_returns(data.spot.get(h.leader, pd.DataFrame())))
        rb = cache.setdefault(h.follower, _grid_returns(data.spot.get(h.follower, pd.DataFrame())))
        if ra.empty or rb.empty:
            continue
        joined = pd.concat([ra.rename("a"), rb.rename("b")], axis=1, sort=True).dropna()
        if len(joined) < 500:
            continue
        corrs = {}
        for lag in lags:
            shifted = joined["a"].shift(int(round(lag / GRID_S)))
            c = shifted.corr(joined["b"])
            corrs[lag] = c
        best_lag = max(corrs, key=lambda k: abs(corrs[k]) if not np.isnan(corrs[k]) else -1)
        lead_only = {k: v for k, v in corrs.items() if k > 0}
        best_lead = max(
            lead_only, key=lambda k: abs(lead_only[k]) if not np.isnan(lead_only[k]) else -1
        )
        rows.append(
            {
                "hypothesis": h.name,
                "obs": len(joined),
                "corr_lag0": corrs.get(0, np.nan),
                "best_lag_s": best_lag,
                "corr_best": corrs[best_lag],
                "best_lead_s": best_lead,
                "corr_lead": lead_only[best_lead],
                "reads": (
                    "leader leads"
                    if best_lag > 0 and abs(corrs[best_lag]) > abs(corrs.get(0, 0)) * 1.2
                    else "simultaneous"
                    if best_lag == 0
                    else "follower leads"
                ),
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 4. predictor test


def _leader_features(data: CrossData, h: Hypothesis, horizon: int) -> pd.DataFrame:
    """Per window: follower implied/outcome and leader state at the same moment."""
    imp = data.implied.get(horizon, pd.DataFrame())
    if imp.empty or h.follower not in imp.columns:
        return pd.DataFrame()
    df = pd.DataFrame({"p_b": imp[h.follower], "y_b": data.outcomes.get(h.follower)})
    if h.leader in imp.columns:
        df["p_a"] = imp[h.leader]
    df = df.dropna(subset=["p_b", "y_b"])
    # leader spot return since window open, and over the last 60 s, at the follower's snapshot time
    spot_a = data.spot.get(h.leader, pd.DataFrame())
    asks = data.asks.get(horizon, pd.DataFrame())
    if not spot_a.empty and not asks.empty:
        ts_b = (
            asks.xs(h.follower, level="series_ticker")["ts"]
            if h.follower in asks.index.get_level_values(1)
            else pd.Series(dtype=float)
        )
        ts_b = ts_b.reindex(df.index)
        sp = spot_a.sort_values("ts")
        px = sp["price"].to_numpy()
        tt = sp["ts"].to_numpy()

        def at(t: float) -> float:
            i = np.searchsorted(tt, t, side="right") - 1
            return px[i] if 0 <= i < len(px) and t - tt[i] <= 60 else np.nan

        open_a = data.opens.get(h.leader)
        ret_open, ret_60 = [], []
        for w, t in ts_b.items():
            if pd.isna(t):
                ret_open.append(np.nan)
                ret_60.append(np.nan)
                continue
            now = at(t)
            o = (
                at(open_a.get(w, np.nan))
                if open_a is not None and not pd.isna(open_a.get(w, np.nan))
                else at(w - 900)
            )
            back = at(t - 60)
            ret_open.append(np.log(now / o) * 1e4 if now > 0 and o > 0 else np.nan)
            ret_60.append(np.log(now / back) * 1e4 if now > 0 and back > 0 else np.nan)
        df["a_ret_open_bps"] = ret_open
        df["a_ret_60_bps"] = ret_60
    return df


FEATURES = (
    ("p_a", "leader_market"),
    ("a_ret_open_bps", "leader_ret_open"),
    ("a_ret_60_bps", "leader_ret_60s"),
)


def predictor_test(data: CrossData, horizon: int) -> pd.DataFrame:
    """One row per (hypothesis, leader feature): does the leader add information beyond p_b?

    ``beta`` is the logistic coefficient on the (sign-adjusted) leader feature after
    controlling for logit(p_b); ``excess_up`` / ``excess_dn`` are realised minus
    implied for the follower when the leader points up / down.
    """
    rows = []
    for h in data.universe.hypotheses:
        df = _leader_features(data, h, horizon)
        if df.empty or len(df) < MIN_WINDOWS:
            rows.append(
                {
                    "hypothesis": h.name,
                    "horizon_s": horizon,
                    "feature": "-",
                    "windows": len(df),
                    "note": "too few windows",
                }
            )
            continue
        brier = float(((df["p_b"] - df["y_b"]) ** 2).mean())
        for feat, label in FEATURES:
            if feat not in df.columns:
                continue
            d = df.dropna(subset=[feat])
            if len(d) < MIN_WINDOWS:
                continue
            x = d[feat].to_numpy(dtype=float)
            if feat == "p_a":
                x = logit(x)
            x = h.sign * x
            X = np.column_stack([logit(d["p_b"].to_numpy()), x])
            pt, lo, hi = bootstrap_coef(X, d["y_b"].to_numpy(), col=1)
            up, dn = d[x > 0], d[x < 0]
            rows.append(
                {
                    "hypothesis": h.name,
                    "horizon_s": horizon,
                    "feature": label,
                    "windows": len(d),
                    "brier_market": brier,
                    "beta": pt,
                    "beta_lo": lo,
                    "beta_hi": hi,
                    "excess_up": float((up["y_b"] - up["p_b"]).mean()) if len(up) else np.nan,
                    "n_up": len(up),
                    "excess_dn": float((dn["y_b"] - dn["p_b"]).mean()) if len(dn) else np.nan,
                    "n_dn": len(dn),
                    "informative": bool(lo > 0) if not np.isnan(lo) else False,
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 5. divergence backtest


def divergence_backtest(
    data: CrossData, horizon: int, min_gap: float = 0.10, taker_rate: float = 0.07
) -> pd.DataFrame:
    """Buy the follower's side the leader's market favours when the markets disagree.

    gap = sign * (p_a - 0.5) - (p_b - 0.5): how much more confident the leader
    market is in "up" (adjusted for the hypothesis sign) than the follower's.
    Enter when |gap| >= min_gap, on the follower side the leader favours, at
    the follower's ask. Per-contract averages, fee-inclusive.
    """
    rows = []
    asks = data.asks.get(horizon, pd.DataFrame())
    for h in data.universe.hypotheses:
        df = _leader_features(data, h, horizon)
        if df.empty or "p_a" not in df.columns or asks.empty:
            continue
        df = df.dropna(subset=["p_a"])
        if h.follower not in asks.index.get_level_values(1):
            continue
        a = asks.xs(h.follower, level="series_ticker").reindex(df.index)
        df = df.assign(yes_ask=a["yes_ask"], no_ask=a["no_ask"]).dropna(
            subset=["yes_ask", "no_ask"]
        )
        gap = h.sign * (df["p_a"] - 0.5) - (df["p_b"] - 0.5)
        sel = df[gap.abs() >= min_gap].copy()
        if len(sel) < 10:
            rows.append(
                {"hypothesis": h.name, "horizon_s": horizon, "min_gap": min_gap, "trades": len(sel)}
            )
            continue
        buy_yes = gap.loc[sel.index] > 0
        ask = np.where(buy_yes, sel["yes_ask"], sel["no_ask"])
        win = np.where(buy_yes, sel["y_b"] == 1, sel["y_b"] == 0).astype(float)
        gross = win * (1 - ask) - (1 - win) * ask
        fees = np.array([fee_per_contract(p, taker_rate) for p in ask])
        net = gross - fees
        pt, lo, hi = bootstrap_mean(net)
        rows.append(
            {
                "hypothesis": h.name,
                "horizon_s": horizon,
                "min_gap": min_gap,
                "trades": len(sel),
                "win_rate": float(win.mean()),
                "avg_ask": float(ask.mean()),
                "net_per_contract": pt,
                "net_lo": lo,
                "net_hi": hi,
                "breakeven_win": float((ask + fees).mean()),
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- report


def report(data: CrossData) -> str:
    pd.set_option("display.width", 180)
    pd.set_option("display.max_columns", 40)
    pd.set_option("display.float_format", lambda v: f"{v:,.3f}")
    out: list[str] = []
    cov = coverage(data)
    out.append("== coverage (settled windows per series)")
    out.append(cov.to_string(index=False) if not cov.empty else "(no settled markets)")
    if cov.empty:
        return "\n".join(out)
    out.append("\n== common windows between series")
    out.append(overlap(data).to_string())
    n_min = int(cov["windows"].min()) if len(cov) > 1 else 0
    if n_min < MIN_WINDOWS:
        out.append(
            f"\nSome series have under {MIN_WINDOWS} windows; pair statistics for them are "
            "skipped. Keep recording."
        )

    out.append("\n== outcome co-movement (do they finish the same way?)")
    cm = outcome_comovement(data)
    out.append(cm.to_string(index=False) if not cm.empty else "(not enough overlapping windows)")

    out.append("\n== declared hypotheses: realised sign vs claimed")
    out.append(hypothesis_signs(data).to_string(index=False))

    out.append("\n== spot lead-lag (positive lag = leader's past return vs follower's next return)")
    ll = spot_lead_lag(data)
    out.append(ll.to_string(index=False) if not ll.empty else "(not enough overlapping spot data)")

    for hz in (300, 60):
        out.append(
            f"\n== predictor test at T-{hz}s: leader information beyond follower's own price"
        )
        out.append(
            "   informative = beta's 95% CI excludes zero after controlling for the "
            "follower's own price"
        )
        pt = predictor_test(data, hz)
        out.append(pt.to_string(index=False) if not pt.empty else "(no data)")

    out.append(
        "\n== divergence backtest: buy follower side favoured by leader market, "
        "per contract after fees"
    )
    frames = [
        divergence_backtest(data, hz, gap) for hz in (300, 120, 60) for gap in (0.10, 0.20, 0.30)
    ]
    bt = (
        pd.concat([f for f in frames if not f.empty], ignore_index=True)
        if any(not f.empty for f in frames)
        else pd.DataFrame()
    )
    out.append(bt.to_string(index=False) if not bt.empty else "(no data)")
    out.append(
        "\nReading the result: co-movement alone is not an edge. Only a predictor beta with a CI "
        "excluding zero at a horizon where the follower's book is still liquid, AND a divergence "
        "net per contract whose CI excludes zero, counts. Both must hold in a time-ordered "
        "held-out sample before anything trades."
    )
    return "\n".join(out)
