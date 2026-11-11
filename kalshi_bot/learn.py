"""Scheduled, gated learning from the decision log. Nothing here runs per trade.

Data: every decision the trader logged (orders, skips, risk rejections) joined
to the market's settlement result. Skips are the counterfactuals: we know what
the model thought and what happened, whether or not we traded.

Each run:

1. Builds the dataset for one mode (paper or live) and backfills results.
2. Fits a calibration map on the training part of a strict time split:
   logit(y) ~ a + b * logit(fair_p_model). A well-calibrated model has a=0, b=1.
3. Evaluates a small grid of candidate configs (calibration on/off x margin x
   price band) on the held-out part, by replaying the trading rule over the
   logged asks: buy the side whose calibrated probability beats the ask by fee
   plus margin, one contract, hold to settlement, exact fee.
4. Compares each candidate to the incumbent on the SAME held-out windows
   (paired), with a bootstrap CI on the difference per decision.
5. Promotion gate: the best candidate is proposed only if the paired
   difference's 95% CI excludes zero, the candidate's own held-out net is
   positive, and the held-out sample has at least ``min_test`` decisions.
   ``--promote`` activates it; it is logged and ``--rollback`` reverts.

Entry logic never changes while the trader runs; the trader reads the active
config at start-up.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .fees import fee_per_contract
from .storage import MarketDataStore
from .strategy import FairValueParams, calibrate

MIN_TEST = 100
TRAIN_FRAC = 0.7
MARGINS = (0.02, 0.03, 0.04, 0.06, 0.08)
BOOTSTRAP_ROUNDS = 2000


@dataclass
class EvalResult:
    label: str
    params: dict
    n_test: int
    trades: int
    win_rate: float
    net_total: float
    net_per_decision: float
    net_per_trade: float
    diff_vs_incumbent: float
    diff_lo: float
    diff_hi: float
    trade_lo: float = float("nan")  # 95% CI of net per trade (bootstrap over traded decisions)
    trade_hi: float = float("nan")


def load_dataset(store: MarketDataStore, mode: str) -> pd.DataFrame:
    store.backfill_decision_results()
    rows = store.all_decisions(mode)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    feats = df["features"].map(lambda s: json.loads(s) if isinstance(s, str) else {})
    df["fair_p_model"] = feats.map(lambda f: f.get("fair_p_model", f.get("fair_p")))
    df["z"] = feats.map(lambda f: f.get("z"))
    df["secs_to_close"] = df["secs_to_close"].astype(float)
    df = df[df["result"].isin(["yes", "no"])]
    df = df.dropna(subset=["fair_p_model", "yes_ask", "no_ask"])
    df["y"] = (df["result"] == "yes").astype(float)
    # one row per (ticker, minute) keeps repeated 5-second looks from dominating
    df["minute"] = (df["ts"] // 60).astype(int)
    df = df.sort_values("ts").drop_duplicates(["ticker", "minute"], keep="last")
    return df.reset_index(drop=True)


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def fit_calibration(p_model: np.ndarray, y: np.ndarray, ridge: float = 1e-3) -> tuple[float, float]:
    """Logistic fit of y on logit(p_model); returns (a, b). Falls back to identity."""
    if len(y) < 30 or y.std() == 0:
        return 0.0, 1.0
    x = _logit(p_model)
    X = np.column_stack([np.ones(len(y)), x])
    beta = np.array([0.0, 1.0])
    for _ in range(50):
        z = np.clip(X @ beta, -30, 30)
        p = 1 / (1 + np.exp(-z))
        w = p * (1 - p)
        grad = X.T @ (y - p) - ridge * (beta - np.array([0.0, 1.0]))
        hess = (X * w[:, None]).T @ X + ridge * np.eye(2)
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            break
        beta = beta + step
        if np.abs(step).max() < 1e-8:
            break
    a, b = float(beta[0]), float(beta[1])
    if not (math.isfinite(a) and math.isfinite(b)) or b <= 0:
        return 0.0, 1.0
    return a, b


def _norm_cdf(x: np.ndarray) -> np.ndarray:
    from math import erf, sqrt

    return np.array([0.5 * (1 + erf(v / sqrt(2))) for v in x])


def replay(df: pd.DataFrame, params: FairValueParams) -> pd.Series:
    """Net P&L per decision (0 where the rule does not trade) for one contract."""
    p_model = df["fair_p_model"].to_numpy(dtype=float)
    if params.sigma_scale != 1.0 and "z" in df.columns:
        z = pd.to_numeric(df["z"], errors="coerce").to_numpy(dtype=float)
        rescaled = _norm_cdf(np.nan_to_num(z, nan=0.0) / params.sigma_scale)
        p_model = np.where(np.isnan(z), p_model, rescaled)
    p_up = np.array([calibrate(p, params.cal_a, params.cal_b) for p in p_model])
    out = np.zeros(len(df))
    for side, ask, fair in (
        ("yes", df["yes_ask"].to_numpy(), p_up),
        ("no", df["no_ask"].to_numpy(), 1 - p_up),
    ):
        ok = (ask >= params.min_price) & (ask <= params.max_price)
        if params.favourites_only:
            ok = ok & (ask >= 0.5)
        if params.min_secs_to_close > 0 and "secs_to_close" in df.columns:
            secs = pd.to_numeric(df["secs_to_close"], errors="coerce").to_numpy(dtype=float)
            ok = ok & (np.nan_to_num(secs, nan=0.0) >= params.min_secs_to_close)
        fee = np.array([fee_per_contract(a, params.taker_rate) for a in ask])
        edge = fair - ask - fee
        trade = ok & (edge >= params.margin)
        won = df["result"].to_numpy() == side
        pnl = np.where(won, 1 - ask, -ask) - fee
        # if both sides qualify (should not happen), keep the larger edge
        better = trade & ((out == 0) | (edge > 0))
        out = np.where(better, pnl, out)
    return pd.Series(out, index=df.index)


def _bootstrap_diff(d: np.ndarray, rounds: int = BOOTSTRAP_ROUNDS) -> tuple[float, float]:
    if len(d) < 5:
        return (np.nan, np.nan)
    rng = np.random.default_rng(0)
    idx = rng.integers(0, len(d), size=(rounds, len(d)))
    means = d[idx].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(lo), float(hi)


def evaluate(
    df: pd.DataFrame,
    incumbent: FairValueParams,
    margins: tuple[float, ...] = MARGINS,
    train_frac: float = TRAIN_FRAC,
) -> tuple[list[EvalResult], tuple[float, float], int]:
    """Time-split evaluation of incumbent and candidates. Returns (results, (a, b), n_test)."""
    if df.empty:
        return [], (0.0, 1.0), 0
    cut = int(len(df) * train_frac)
    train, test = df.iloc[:cut], df.iloc[cut:]
    a, b = fit_calibration(train["fair_p_model"].to_numpy(), train["y"].to_numpy())
    base_pnl = replay(test, incumbent).to_numpy()
    candidates: list[tuple[str, FairValueParams]] = [("incumbent", incumbent)]
    base = incumbent.to_dict()
    variants = (
        ("raw", {"cal_a": 0.0, "cal_b": 1.0, "favourites_only": False, "sigma_scale": 1.0}),
        ("calibrated", {"cal_a": a, "cal_b": b, "favourites_only": False, "sigma_scale": 1.0}),
        ("favourites", {"cal_a": 0.0, "cal_b": 1.0, "favourites_only": True, "sigma_scale": 1.0}),
        ("fav+sharp0.7", {"cal_a": 0.0, "cal_b": 1.0, "favourites_only": True, "sigma_scale": 0.7}),
        ("fav+soft1.3", {"cal_a": 0.0, "cal_b": 1.0, "favourites_only": True, "sigma_scale": 1.3}),
        # late-window favourites: the calibration table is tightest above 0.90
        (
            "fav late<=0.99",
            {
                "cal_a": 0.0,
                "cal_b": 1.0,
                "favourites_only": True,
                "sigma_scale": 1.0,
                "max_price": 0.99,
            },
        ),
        (
            "fav late<=0.97",
            {
                "cal_a": 0.0,
                "cal_b": 1.0,
                "favourites_only": True,
                "sigma_scale": 1.0,
                "max_price": 0.97,
            },
        ),
    )
    for m in margins:
        for label, extra in variants:
            candidates.append(
                (f"{label} margin={m}", FairValueParams.from_dict({**base, "margin": m, **extra}))
            )
    results = []
    for label, params in candidates:
        pnl = replay(test, params).to_numpy()
        traded = pnl != 0
        diff = pnl - base_pnl
        lo, hi = _bootstrap_diff(diff)
        t_lo, t_hi = _bootstrap_diff(pnl[traded]) if traded.sum() >= 5 else (np.nan, np.nan)
        results.append(
            EvalResult(
                label=label,
                params=params.to_dict(),
                n_test=len(test),
                trades=int(traded.sum()),
                win_rate=float((pnl[traded] > 0).mean()) if traded.any() else float("nan"),
                net_total=float(pnl.sum()),
                net_per_decision=float(pnl.mean()),
                net_per_trade=float(pnl[traded].mean()) if traded.any() else float("nan"),
                diff_vs_incumbent=float(diff.mean()),
                diff_lo=lo,
                diff_hi=hi,
                trade_lo=t_lo,
                trade_hi=t_hi,
            )
        )
    return results, (a, b), len(test)


EDGE_KEY = "learn:{mode}:edge_ok"
EDGE_MIN_TRADES = 30


def edge_verdict(incumbent: EvalResult | None) -> tuple[bool, str]:
    """Does the ACTIVE config have demonstrated edge on held-out data?

    True only if it traded at least ``EDGE_MIN_TRADES`` held-out decisions, its
    net is positive, and the 95% CI of net per trade excludes zero.
    """
    if incumbent is None:
        return False, "no evaluation yet"
    if incumbent.trades < EDGE_MIN_TRADES:
        return False, f"only {incumbent.trades} held-out trades (< {EDGE_MIN_TRADES})"
    if incumbent.net_total <= 0:
        return False, f"held-out net {incumbent.net_total:+.2f} is not positive"
    if math.isnan(incumbent.trade_lo) or incumbent.trade_lo <= 0:
        return False, (
            f"net per trade {incumbent.net_per_trade:+.4f} not distinguishable from zero "
            f"(CI low {incumbent.trade_lo:+.4f})"
        )
    return True, (
        f"held-out net {incumbent.net_total:+.2f} over {incumbent.trades} trades, "
        f"per-trade CI [{incumbent.trade_lo:+.4f}, {incumbent.trade_hi:+.4f}]"
    )


def choose(results: list[EvalResult], min_test: int = MIN_TEST) -> tuple[EvalResult | None, str]:
    """Promotion gate. Returns (candidate or None, reason)."""
    if not results:
        return None, "no data"
    inc = results[0]
    if inc.n_test < min_test:
        return None, f"held-out sample {inc.n_test} < {min_test}; keep trading the incumbent"
    eligible = [
        r
        for r in results[1:]
        if r.trades >= 20 and r.net_total > 0 and not math.isnan(r.diff_lo) and r.diff_lo > 0
    ]
    if not eligible:
        return None, "no candidate beats the incumbent beyond noise on held-out data"
    best = max(eligible, key=lambda r: r.diff_vs_incumbent)
    return best, (
        f"{best.label}: held-out net {best.net_total:+.2f} over {best.trades} trades, "
        f"paired gain {best.diff_vs_incumbent:+.4f}/decision "
        f"(CI [{best.diff_lo:+.4f}, {best.diff_hi:+.4f}])"
    )


def calibration_table(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    bins = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    b = pd.cut(df["fair_p_model"], bins, include_lowest=True)
    out = (
        df.groupby(b, observed=True)
        .agg(n=("y", "size"), model=("fair_p_model", "mean"), realised=("y", "mean"))
        .reset_index()
        .rename(columns={"fair_p_model": "bucket"})
    )
    out["gap"] = out["realised"] - out["model"]
    return out


def run(store: MarketDataStore, mode: str, promote: bool = False) -> str:
    pd.set_option("display.width", 160)
    pd.set_option("display.float_format", lambda v: f"{v:,.4f}")
    active = store.active_config(mode)
    incumbent = FairValueParams.from_dict(active["params"]) if active else FairValueParams()
    df = load_dataset(store, mode)
    keys = ("margin", "cal_a", "cal_b", "min_price", "max_price")
    shown = {k: incumbent.to_dict()[k] for k in keys}
    out = [
        f"== learn ({mode})",
        f"decisions with outcomes: {len(df)}  incumbent: {json.dumps(shown)}",
    ]
    if df.empty:
        out.append("nothing to learn from yet; run the trader first")
        return "\n".join(out)
    out.append("\n== calibration of the model (all data)")
    out.append(calibration_table(df).to_string(index=False))
    brier = float(((df["fair_p_model"] - df["y"]) ** 2).mean())
    out.append(f"brier={brier:.4f} (0.25 = coin flip)")
    results, (a, b), n_test = evaluate(df, incumbent)
    out.append(
        f"\n== held-out evaluation (last {n_test} decisions; fitted calibration "
        f"a={a:+.3f} b={b:.3f})"
    )
    table = pd.DataFrame([{k: v for k, v in r.__dict__.items() if k != "params"} for r in results])
    out.append(table.to_string(index=False))
    best, reason = choose(results)
    out.append("\n== promotion gate")
    out.append(reason)
    ok, why = edge_verdict(results[0] if results else None)
    key = EDGE_KEY.format(mode=mode)
    store.set_meta(
        {key: "1" if ok else "0", key + ":reason": why, key + ":ts": f"{time.time():.0f}"}
    )
    out.append("\n== edge gate (does the ACTIVE config get to send real orders?)")
    out.append(("OPEN: " if ok else "CLOSED: ") + why)
    if not ok:
        out.append(
            "The trader keeps deciding and logging but sends no live orders until a daily "
            "`learn` run shows the active config profitable on held-out data. Promote a "
            "winning candidate, then re-run `learn` to re-evaluate it."
        )
    report = "\n".join(out)
    if best is not None:
        cid = store.save_config(mode, best.params, source="learn", report=report, activate=promote)
        out.append(
            f"saved candidate config id={cid}"
            + (
                " and ACTIVATED it. The trader picks it up on next start."
                if promote
                else f". Re-run with --promote to activate, or `kalshi-bot learn --activate {cid}`."
            )
        )
    return "\n".join(out)


def rollback(store: MarketDataStore, mode: str) -> str:
    hist = store.config_history(mode)
    active = [c for c in hist if c["active"]]
    if not active:
        return "no active learned config; the trader is on built-in defaults"
    cur = active[-1]
    earlier = [c for c in hist if c["created_ts"] < cur["created_ts"]]
    if earlier:
        store.activate_config(mode, earlier[-1]["id"])
        return f"rolled back to config id={earlier[-1]['id']} ({earlier[-1]['source']})"
    store.save_config(
        mode, FairValueParams().to_dict(), source="rollback", activate=True, now=time.time()
    )
    return "rolled back to built-in defaults"
