"""Snapshot-free calibration of the fair-value model over the backfilled sample.

The learner and the evolution core score through the order-book backtest, so they
can only use live-recorded snapshots. The 30-day backfill has spot + outcomes but
no order books, so it was invisible to them. This adapter closes that gap: for
every settled market it computes the model's probability from recorded spot
(exactly the live estimator -- realized_vol + fair_value, vol clamped to the same
floor/cap) at a fixed horizon before close, then checks it against the result.

It answers the question the backtest cannot on this data: *is the model's
probability trustworthy over the large sample, and does it generalise?* -- which is
the input the sizing/gate logic depends on.

    python -m kalshi_bot.calibration --horizon 300
"""

from __future__ import annotations

import argparse
import math
import sqlite3

import numpy as np
import pandas as pd

from . import fairvalue
from .analysis import SPOT_SYMBOLS

PER_YEAR = math.sqrt(365 * 86400)


def build(db: str, horizon_s: float, vol_window: float,
          vol_floor: float = 0.30, vol_cap: float = 3.0) -> pd.DataFrame:
    """One (series, p, win, close_ts) row per settled market, p = model prob at
    ``horizon_s`` before close from recorded spot."""
    con = sqlite3.connect(db)
    try:
        markets = pd.read_sql(
            "SELECT ticker, series_ticker, strike, close_ts, result FROM markets "
            "WHERE result IN ('yes','no') AND strike IS NOT NULL AND close_ts IS NOT NULL",
            con,
        )
        spot = pd.read_sql("SELECT ts, symbol, price FROM spot WHERE price > 0", con)
    finally:
        con.close()
    rows = []
    for series, sym in SPOT_SYMBOLS.items():
        ms = markets[markets["series_ticker"] == series].copy()
        if ms.empty:
            continue
        # 60s grid so 1-minute backfilled candles count (live 5s data still works);
        # the point is cross-period calibration, not the exact live vol estimator.
        vol = fairvalue.realized_vol(spot, sym, vol_window, step_s=60.0)
        sp = spot[spot["symbol"] == sym][["ts", "price"]].dropna().sort_values("ts")
        if vol.empty or len(sp) < 3:
            continue
        ms["eval_ts"] = ms["close_ts"] - horizon_s
        ms = ms.sort_values("eval_ts")
        ms = pd.merge_asof(
            ms, sp.rename(columns={"ts": "eval_ts", "price": "spot"}),
            on="eval_ts", direction="backward", tolerance=180,
        )
        ms = pd.merge_asof(
            ms, vol.rename(columns={"ts": "eval_ts"}),
            on="eval_ts", direction="backward", tolerance=vol_window,
        )
        ms = ms.dropna(subset=["spot", "sigma"])
        if ms.empty:
            continue
        sigma = np.clip(ms["sigma"].to_numpy(), vol_floor / PER_YEAR, vol_cap / PER_YEAR)
        p = fairvalue.fair_value(ms["spot"].to_numpy(), ms["strike"].to_numpy(), sigma, horizon_s)
        win = (ms["result"].to_numpy() == "yes").astype(float)
        for pi, wi, cts in zip(p, win, ms["close_ts"].to_numpy()):
            if not math.isnan(pi):
                rows.append((series, float(pi), float(wi), float(cts)))
    return pd.DataFrame(rows, columns=["series", "p", "win", "close_ts"])


def report(df: pd.DataFrame, label: str) -> None:
    n = len(df)
    if n == 0:
        print(f"  {label:>18}: (no data)")
        return
    p = df["p"].to_numpy()
    w = df["win"].to_numpy()
    brier = float(np.mean((p - w) ** 2))
    # Brier of always predicting the base rate, for reference
    base = float(np.mean((w.mean() - w) ** 2))
    skill = 1 - brier / base if base > 0 else 0.0
    print(f"  {label:>18}: n={n:<5} Brier={brier:.4f} (skill vs base {skill:+.1%})  "
          f"pred={p.mean():.3f} actual={w.mean():.3f}")


def reliability(df: pd.DataFrame) -> None:
    p = df["p"].to_numpy()
    w = df["win"].to_numpy()
    print("     reliability:  bin       n   pred  actual")
    for lo in (0.0, 0.2, 0.4, 0.6, 0.8):
        m = (p >= lo) & (p < lo + 0.2)
        if m.sum():
            print(f"                  [{lo:.1f},{lo+0.2:.1f})  {m.sum():>5}  {p[m].mean():.2f}   {w[m].mean():.2f}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Calibrate the fair-value model on backfilled data.")
    ap.add_argument("--db", default="state/market_data.sqlite")
    ap.add_argument("--horizon", type=float, default=300.0, help="seconds before close to evaluate")
    ap.add_argument("--vol-window", type=float, default=1800.0)
    args = ap.parse_args()

    df = build(args.db, args.horizon, args.vol_window)
    print(f"=== fair-value calibration on backfilled sample (eval {args.horizon:.0f}s to close) ===")
    report(df, "ALL")
    reliability(df)
    for s in sorted(df["series"].unique()):
        report(df[df["series"] == s], s)
    if len(df) >= 100:
        df = df.sort_values("close_ts")
        cut = int(len(df) * 0.7)
        print("  --- temporal generalisation (does calibration hold out-of-sample?) ---")
        report(df.iloc[:cut], "train (first 70%)")
        report(df.iloc[cut:], "test (last 30%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
