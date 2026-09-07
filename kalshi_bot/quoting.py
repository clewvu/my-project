"""Passive two-sided quoting around fair value, tested on recorded prints.

The directional trade (buy the side the model likes, pay the ask) has been
measured twice now: on the recorder's data by ``kalshi_bot.fairvalue`` and
on the owner's own money by ``kalshi_bot.review``. Both say the market is
faster than a spot model. The published evidence on these 15-minute
binaries says the same and points the other way: makers, who are paid the
spread by takers, lose far less than takers, and on the crypto series the
maker fee is zero.

This module backtests that pivot on the recorder's snapshots and trade
prints before any of it goes live:

* At every snapshot, quote both sides ``spread`` below the model's fair
  value: a resting YES bid at ``fair - spread`` and a resting NO bid at
  ``(1 - fair) - spread``, each rounded down to the tick. Fair value is the
  research brief's TWAP-aware model, which already prices the 60-second
  settlement average (``effective_tau``), so it does not overprice "it will
  move" the way spot-at-expiry pricing does.
* A quote lives until the next snapshot. It fills when a public print
  crosses it in the meantime: a taker selling YES at or below our YES bid
  (``fill="touch"``) or strictly below it (``fill="cross"``, the default,
  which stands in for queue priority we cannot see).
* At most one fill per side per market; a filled side stops quoting. Both
  sides filled is a locked position that pays ``1 - yes_bid - no_bid``
  whatever happens. Fills are held to settlement. No quoting inside
  ``min_ttc`` seconds of close.
* Net per fill is the settlement payoff minus the maker fee (zero unless
  told otherwise). Adverse selection shows up directly: a print crosses a
  stale quote exactly when spot has moved against it.

The report cuts the result by spread, by seconds to close and by whether
one or both sides filled, with a clustered bootstrap by 15-minute window,
and a time-ordered held-out split. Same gate philosophy as the fair-value
test: nothing goes live on the strength of the in-sample number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import fairvalue as fvmod
from .fees import MAKER_RATE
from .models import TICK
from .whale import cluster_bootstrap

SPREADS = (0.01, 0.02, 0.03, 0.05)
MIN_TTC_S = 120.0
MIN_FILLS_FOR_VERDICT = 100
VIABLE_NET = 0.005  # dollars per contract, held out, CI above zero

FILL_COLUMNS = [
    "ticker",
    "series_ticker",
    "window",
    "close_ts",
    "quote_ts",
    "fill_ts",
    "secs_to_close",
    "side",
    "price",
    "fair",
    "won",
    "gross",
    "fee",
    "net",
]


def _floor_tick(x: np.ndarray) -> np.ndarray:
    return np.floor(x / TICK + 1e-9) * TICK


@dataclass
class QuoteResult:
    fills: pd.DataFrame  # one row per fill
    markets: pd.DataFrame  # one row per market quoted: fills, locked, net


def backtest(
    fv: fvmod.FairValueData,
    spread: float = 0.02,
    vol_window: int = fvmod.VOL_WINDOWS[0],
    min_ttc: float = MIN_TTC_S,
    fill: str = "cross",
    maker_rate: float = MAKER_RATE,
    max_fills_per_side: int = 1,
) -> QuoteResult:
    if fill not in ("cross", "touch"):
        raise ValueError("fill must be cross or touch")
    s = fv.snapshots
    trades = fv.trades
    if s.empty or trades is None or trades.empty or f"p_{vol_window}" not in s.columns:
        return QuoteResult(pd.DataFrame(columns=FILL_COLUMNS), pd.DataFrame())
    if "taker_side" not in trades.columns:
        raise ValueError("quoting needs trades with taker_side; re-load with fairvalue.load")
    t = trades.dropna(subset=["ts", "yes_price"]).sort_values("ts")
    rows: list[dict] = []
    market_rows: list[dict] = []
    for ticker, g in s.groupby("ticker", sort=False):
        g = g.sort_values("ts")
        won_yes = g["won_yes"].iloc[0]
        if pd.isna(won_yes):
            continue
        tt = t[t["ticker"] == ticker]
        ts = g["ts"].to_numpy()
        fair = g[f"p_{vol_window}"].to_numpy()
        ttc = g["secs_to_close"].to_numpy()
        next_ts = np.append(ts[1:], g["close_ts"].iloc[0])
        yes_bid = _floor_tick(fair - spread)
        no_bid = _floor_tick((1 - fair) - spread)
        p_ts = tt["ts"].to_numpy()
        p_yes = tt["yes_price"].to_numpy(dtype=float)
        p_side = tt["taker_side"].to_numpy()
        filled = {"yes": 0, "no": 0}
        fills_here: list[dict] = []
        for i in range(len(ts)):
            if np.isnan(fair[i]) or ttc[i] < min_ttc:
                continue
            lo = np.searchsorted(p_ts, ts[i], side="right")
            hi = np.searchsorted(p_ts, next_ts[i], side="right")
            if hi <= lo:
                continue
            for side, our in (("yes", yes_bid[i]), ("no", no_bid[i])):
                if filled[side] >= max_fills_per_side or not 0 < our < 1:
                    continue
                # a taker buying NO sells YES to our YES bid; a taker buying YES
                # sells NO to our NO bid, at no_price = 1 - yes_price
                taker = "no" if side == "yes" else "yes"
                px = p_yes[lo:hi] if side == "yes" else 1 - p_yes[lo:hi]
                hit = (p_side[lo:hi] == taker) & (
                    (px < our - 1e-9) if fill == "cross" else (px <= our + 1e-9)
                )
                if not hit.any():
                    continue
                k = lo + int(np.argmax(hit))
                won = float(won_yes if side == "yes" else 1 - won_yes)
                gross = (1 - our) if won else -our
                fee = maker_rate * our * (1 - our)
                filled[side] += 1
                fills_here.append(
                    {
                        "ticker": ticker,
                        "series_ticker": g["series_ticker"].iloc[0],
                        "window": g["window"].iloc[0],
                        "close_ts": g["close_ts"].iloc[0],
                        "quote_ts": ts[i],
                        "fill_ts": p_ts[k],
                        "secs_to_close": ttc[i],
                        "side": side,
                        "price": float(our),
                        "fair": float(fair[i] if side == "yes" else 1 - fair[i]),
                        "won": won,
                        "gross": gross,
                        "fee": fee,
                        "net": gross - fee,
                    }
                )
        rows.extend(fills_here)
        market_rows.append(
            {
                "ticker": ticker,
                "window": g["window"].iloc[0],
                "fills": len(fills_here),
                "locked": int(filled["yes"] > 0 and filled["no"] > 0),
                "net": sum(f["net"] for f in fills_here),
            }
        )
    fills = pd.DataFrame(rows, columns=FILL_COLUMNS)
    return QuoteResult(fills, pd.DataFrame(market_rows))


def summarize(res: QuoteResult, label: str = "all") -> dict:
    f = res.fills
    m = res.markets
    n = len(f)
    net = cluster_bootstrap(f, "net", cluster="window") if n else (np.nan, np.nan, np.nan)
    return {
        "label": label,
        "markets": int(len(m)),
        "fills": n,
        "fill_rate": float(n / max(1, len(m))),
        "locked": float(m["locked"].mean()) if len(m) else np.nan,
        "win_rate": float(f["won"].mean()) if n else np.nan,
        "avg_price": float(f["price"].mean()) if n else np.nan,
        "adverse": float((f["fair"] - f["won"]).mean()) if n else np.nan,
        "net": net[0],
        "net_lo": net[1],
        "net_hi": net[2],
        "net_total": float(f["net"].sum()) if n else 0.0,
    }


def grid(
    fv: fvmod.FairValueData,
    tickers: set | None = None,
    spreads: tuple[float, ...] = SPREADS,
    vol_windows: tuple[int, ...] | None = None,
    min_ttc: float = MIN_TTC_S,
    fill: str = "cross",
) -> pd.DataFrame:
    sub = fv if tickers is None else _subset(fv, tickers)
    out = []
    for w in vol_windows or fv.vol_windows:
        for k in spreads:
            row = summarize(backtest(sub, spread=k, vol_window=w, min_ttc=min_ttc, fill=fill))
            row.update({"vol_window": w, "spread": k})
            out.append(row)
    return pd.DataFrame(out)


def _subset(fv: fvmod.FairValueData, tickers: set) -> fvmod.FairValueData:
    return fvmod.FairValueData(
        snapshots=fv.snapshots[fv.snapshots["ticker"].isin(tickers)],
        markets=fv.markets[fv.markets["ticker"].isin(tickers)],
        spot=fv.spot,
        trades=None if fv.trades is None else fv.trades[fv.trades["ticker"].isin(tickers)],
        vol_windows=fv.vol_windows,
    )


def by_ttc(fills: pd.DataFrame) -> pd.DataFrame:
    if fills.empty:
        return pd.DataFrame()
    edges = [0, 180, 360, 600, 900, math.inf]
    labels = ["<3 min", "3-6 min", "6-10 min", "10-15 min", ">15 min"]
    f = fills.assign(bucket=pd.cut(fills["secs_to_close"], edges, labels=labels, right=False))
    return (
        f.groupby("bucket", observed=True)
        .agg(fills=("net", "size"), win_rate=("won", "mean"), net=("net", "mean"))
        .reset_index()
    )


def verdict(fv: fvmod.FairValueData, min_ttc: float = MIN_TTC_S, fill: str = "cross") -> str:
    train, test = fvmod.split_tickers(fv)
    g = grid(fv, train, min_ttc=min_ttc, fill=fill)
    ok = g[g["fills"] >= 30].dropna(subset=["net"])
    if ok.empty:
        return "INCONCLUSIVE: too few fills in the training markets"
    best = ok.sort_values("net", ascending=False).iloc[0]
    held = summarize(
        backtest(
            _subset(fv, test),
            spread=float(best["spread"]),
            vol_window=int(best["vol_window"]),
            min_ttc=min_ttc,
            fill=fill,
        ),
        "held out",
    )
    if held["fills"] < MIN_FILLS_FOR_VERDICT * 0.3:
        return (
            f"INCONCLUSIVE: best training config spread {best['spread']:.2f} window "
            f"{int(best['vol_window'])}s; only {held['fills']} held-out fills"
        )
    if held["net"] >= VIABLE_NET and held["net_lo"] > 0:
        return (
            f"VIABLE: spread {best['spread']:.2f} window {int(best['vol_window'])}s nets "
            f"{held['net']:+.4f}/contract held out [{held['net_lo']:+.4f}, {held['net_hi']:+.4f}] "
            f"over {held['fills']} fills"
        )
    return (
        f"NOT VIABLE: spread {best['spread']:.2f} window {int(best['vol_window'])}s nets "
        f"{held['net']:+.4f}/contract held out [{held['net_lo']:+.4f}, {held['net_hi']:+.4f}] "
        f"over {held['fills']} fills"
    )


def report(fv: fvmod.FairValueData, min_ttc: float = MIN_TTC_S, fill: str = "cross") -> str:
    lines = ["== passive quoting around fair value, on recorded prints"]
    lines.append(
        f"settled markets: {len(fv.settled_markets)}; prints: "
        f"{0 if fv.trades is None else len(fv.trades)}; fill model: {fill}; "
        f"maker fee {MAKER_RATE:.3f}; no quotes inside {min_ttc:.0f}s of close"
    )
    if fv.trades is None or fv.trades.empty:
        lines.append("no trade prints recorded yet; the recorder needs to run longer")
        return "\n".join(lines)
    g = grid(fv, min_ttc=min_ttc, fill=fill)
    cols = [
        "vol_window",
        "spread",
        "markets",
        "fills",
        "fill_rate",
        "locked",
        "win_rate",
        "avg_price",
        "adverse",
        "net",
        "net_lo",
        "net_hi",
        "net_total",
    ]
    with pd.option_context("display.width", 160, "display.float_format", "{:.4f}".format):
        lines.append("\n-- spread grid, all markets (net is dollars per contract after fees)")
        lines.append(g[cols].to_string(index=False))
        best = g.dropna(subset=["net"]).sort_values("net", ascending=False)
        if not best.empty:
            b = best.iloc[0]
            res = backtest(
                fv, spread=float(b["spread"]), vol_window=int(b["vol_window"]), min_ttc=min_ttc
            )
            lines.append(
                f"\n-- best config (spread {b['spread']:.2f}, window {int(b['vol_window'])}s) "
                "by seconds to close at the quote"
            )
            lines.append(by_ttc(res.fills).to_string(index=False))
            f = res.fills
            if not f.empty:
                locked = res.markets[res.markets["locked"] == 1]
                lines.append(
                    f"\nadverse selection: fills were worth {f['won'].mean():.3f} at settlement "
                    f"against a model fair value of {f['fair'].mean():.3f} when quoted"
                )
                if len(locked):
                    lines.append(
                        f"{len(locked)} markets filled both sides "
                        f"(locked {locked['net'].mean():+.4f} each)"
                    )
    lines.append("\n== verdict (best spread on the first 70% of markets, judged on the last 30%)")
    lines.append(verdict(fv, min_ttc=min_ttc, fill=fill))
    return "\n".join(lines)
