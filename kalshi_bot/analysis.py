"""Research analysis over recorded market data.

Answers, from the SQLite file the recorder writes:

* coverage      what was captured, settlement base rates
* calibration   does the market's implied probability match realised outcomes?
* spot signal   how well does "spot above strike" predict the result, by distance?
* lead-lag      does the book move after spot, or with it?
* backtest      fee-inclusive P&L of buying the spot-favoured side at the ask

All prices are dollars; all horizons are seconds before market close.
Requires pandas (``pip install -e ".[research]"``).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import pandas as pd

from .fees import fee_per_contract

SPOT_SYMBOLS = {
    "KXBTC15M": "BTC-USD",
    "KXETH15M": "ETH-USD",
    "KXSOL15M": "SOL-USD",
    "KXDOGE15M": "DOGE-USD",
    "KXXRP15M": "XRP-USD",
}
HORIZONS = (840, 600, 300, 180, 120, 60, 30, 10)
SPOT_TOLERANCE_S = 30.0
DIST_BINS = (0, 1, 2, 5, 10, 20, float("inf"))
PRICE_BINS = tuple(i / 10 for i in range(11))


@dataclass
class Dataset:
    markets: pd.DataFrame
    snapshots: pd.DataFrame  # joined with market + nearest spot
    spot: pd.DataFrame
    horizons: tuple[int, ...] = HORIZONS
    notes: list[str] = field(default_factory=list)

    @property
    def settled(self) -> pd.DataFrame:
        return self.snapshots[self.snapshots["won_yes"].notna()]


# ---------------------------------------------------------------- loading


def load(
    db_path: str, series: list[str] | None = None, horizons: tuple[int, ...] = HORIZONS
) -> Dataset:
    con = sqlite3.connect(db_path)
    try:
        markets = pd.read_sql(
            "SELECT ticker, series_ticker, strike, open_ts, close_ts, status, result, "
            "expiration_value FROM markets",
            con,
        )
        snaps = pd.read_sql(
            """
            SELECT ts, ticker, secs_to_close, yes_bid, yes_ask, no_bid, no_ask, last_price,
                   yes_depth, no_depth
            FROM snapshots
            """,
            con,
        )
        spot = pd.read_sql("SELECT ts, source, symbol, price FROM spot", con)
    finally:
        con.close()
    return assemble(markets, snaps, spot, series=series, horizons=horizons)


def assemble(
    markets: pd.DataFrame,
    snaps: pd.DataFrame,
    spot: pd.DataFrame,
    series: list[str] | None = None,
    horizons: tuple[int, ...] = HORIZONS,
) -> Dataset:
    """Join raw ``markets``, ``snapshots`` and ``spot`` frames into a Dataset.

    This is everything ``load`` does after reading SQLite, exposed so research
    code and tests can build a Dataset from frames directly.
    """
    spot = prefer_websocket_spot(spot)

    if series:
        markets = markets[markets["series_ticker"].isin(series)]
    markets = markets.copy()
    markets["won_yes"] = markets["result"].map({"yes": 1.0, "no": 0.0})

    snaps = snaps.merge(
        markets[["ticker", "series_ticker", "strike", "close_ts", "won_yes"]],
        on="ticker",
        how="inner",
    )
    snaps["yes_mid"] = (snaps["yes_bid"] + snaps["yes_ask"]) / 2
    snaps = _attach_spot(snaps, spot)
    snaps["spot_dist_bps"] = (snaps["spot"] - snaps["strike"]) / snaps["strike"] * 1e4
    # 1.0 / 0.0, NaN when no spot was attached
    snaps["spot_above"] = (snaps["spot"] >= snaps["strike"]).astype(float)
    snaps.loc[snaps["spot"].isna(), "spot_above"] = float("nan")
    snaps = snaps.sort_values(["ticker", "ts"]).reset_index(drop=True)
    return Dataset(markets=markets, snapshots=snaps, spot=spot, horizons=horizons)


def prefer_websocket_spot(spot: pd.DataFrame) -> pd.DataFrame:
    """Per symbol, use WebSocket ticks where they exist and REST polls elsewhere.

    The two sources overlap in time once the WebSocket is running; keeping both
    would let a 5-second REST value shadow a fresher tick in merge_asof.
    """
    if spot.empty or "source" not in spot.columns:
        return spot
    parts = []
    for _, g in spot.groupby("symbol"):
        ws = g[g["source"] == "coinbase_ws"]
        if ws.empty:
            parts.append(g)
            continue
        rest = g[g["source"] != "coinbase_ws"]
        rest = rest[(rest["ts"] < ws["ts"].min()) | (rest["ts"] > ws["ts"].max())]
        parts.append(pd.concat([ws, rest]))
    return pd.concat(parts).sort_values("ts").reset_index(drop=True)


def _attach_spot(snaps: pd.DataFrame, spot: pd.DataFrame) -> pd.DataFrame:
    snaps = snaps.copy()
    snaps["spot"] = float("nan")
    if spot.empty or snaps.empty:
        return snaps
    parts = []
    for series, group in snaps.groupby("series_ticker", sort=False):
        symbol = SPOT_SYMBOLS.get(str(series))
        s = spot[spot["symbol"] == symbol][["ts", "price"]].sort_values("ts")
        g = group.drop(columns=["spot"]).sort_values("ts")
        if symbol is None or s.empty:
            g["spot"] = float("nan")
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


def at_horizon(snaps: pd.DataFrame, horizon: float, stale_after: float = 60.0) -> pd.DataFrame:
    """Latest snapshot per market taken at or before ``horizon`` seconds to close."""
    eligible = snaps[
        (snaps["secs_to_close"] >= horizon) & (snaps["secs_to_close"] <= horizon + stale_after)
    ]
    if eligible.empty:
        return eligible
    return (
        eligible.sort_values("secs_to_close")
        .groupby("ticker", sort=False)
        .head(1)
        .reset_index(drop=True)
    )


# ---------------------------------------------------------------- reports


def coverage(ds: Dataset) -> pd.DataFrame:
    m = ds.markets
    rows = []
    for series, g in m.groupby("series_ticker"):
        settled = g["won_yes"].notna().sum()
        sn = ds.snapshots[ds.snapshots["series_ticker"] == series]
        rows.append(
            {
                "series": series,
                "markets": len(g),
                "settled": int(settled),
                "yes_rate": g["won_yes"].mean() if settled else float("nan"),
                "snapshots": len(sn),
                "with_spot": int(sn["spot"].notna().sum()),
                "hours": (sn["ts"].max() - sn["ts"].min()) / 3600 if len(sn) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def calibration(ds: Dataset, horizon: float) -> pd.DataFrame:
    """Implied probability (YES mid) vs realised YES rate, by price bucket."""
    df = at_horizon(ds.settled, horizon).dropna(subset=["yes_mid"])
    if df.empty:
        return pd.DataFrame()
    bucket = pd.cut(df["yes_mid"], bins=list(PRICE_BINS), include_lowest=True)
    out = (
        df.groupby(bucket, observed=True)
        .agg(n=("won_yes", "size"), implied=("yes_mid", "mean"), realised=("won_yes", "mean"))
        .reset_index()
        .rename(columns={"yes_mid": "mid_bucket"})
    )
    out["edge"] = out["realised"] - out["implied"]
    return out


def brier(ds: Dataset, horizon: float) -> dict[str, float]:
    df = at_horizon(ds.settled, horizon).dropna(subset=["yes_mid"])
    if df.empty:
        return {}
    out = {
        "n": float(len(df)),
        "market": float(((df["yes_mid"] - df["won_yes"]) ** 2).mean()),
        "base_rate": float(((df["won_yes"].mean() - df["won_yes"]) ** 2).mean()),
    }
    with_spot = df.dropna(subset=["spot"])
    if not with_spot.empty:
        pred = with_spot["spot_above"].astype(float)
        out["spot_rule"] = float(((pred - with_spot["won_yes"]) ** 2).mean())
    return out


def spot_signal(ds: Dataset, horizon: float) -> pd.DataFrame:
    """Accuracy of 'spot above strike => YES' by distance from strike (bps)."""
    df = at_horizon(ds.settled, horizon).dropna(subset=["spot", "yes_mid"])
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["correct"] = (df["spot_above"].astype(bool) == (df["won_yes"] == 1.0)).astype(float)
    # market-implied probability of the side spot favours
    df["implied_favoured"] = df["yes_mid"].where(df["spot_above"].astype(bool), 1 - df["yes_mid"])
    bucket = pd.cut(df["spot_dist_bps"].abs(), bins=list(DIST_BINS), right=False)
    out = (
        df.groupby(bucket, observed=True)
        .agg(
            n=("correct", "size"),
            spot_accuracy=("correct", "mean"),
            market_implied=("implied_favoured", "mean"),
        )
        .reset_index()
        .rename(columns={"spot_dist_bps": "abs_dist_bps"})
    )
    total = pd.DataFrame(
        [
            {
                "abs_dist_bps": "all",
                "n": len(df),
                "spot_accuracy": df["correct"].mean(),
                "market_implied": df["implied_favoured"].mean(),
            }
        ]
    )
    return pd.concat([out, total], ignore_index=True)


def lead_lag(ds: Dataset, lag_seconds: float = 30.0) -> pd.DataFrame:
    """Correlation between past spot moves and future mid moves.

    A strong positive 'spot_then_mid' value means the book lags spot (an edge for
    anyone watching spot). 'mid_then_spot' is the reverse. 'same_window' is the
    contemporaneous correlation for reference.
    """
    df = ds.snapshots.dropna(subset=["spot", "yes_mid"])
    if df.empty:
        return pd.DataFrame()
    rows = []
    for series, g in df.groupby("series_ticker"):
        parts = []
        for _, t in g.groupby("ticker"):
            t = t.sort_values("ts")
            dt = t["ts"].diff().median()
            if pd.isna(dt) or dt <= 0:
                continue
            k = max(1, int(round(lag_seconds / dt)))
            x = pd.DataFrame(
                {
                    "d_spot_past": t["spot"].pct_change(k) * 1e4,
                    "d_mid_past": t["yes_mid"].diff(k),
                    "d_spot_next": t["spot"].shift(-k) / t["spot"] * 1e4 - 1e4,
                    "d_mid_next": t["yes_mid"].shift(-k) - t["yes_mid"],
                }
            ).dropna()
            parts.append(x)
        if not parts:
            continue
        x = pd.concat(parts)
        if len(x) < 10:
            continue
        rows.append(
            {
                "series": series,
                "n": len(x),
                "lag_s": lag_seconds,
                "spot_then_mid": x["d_spot_past"].corr(x["d_mid_next"]),
                "mid_then_spot": x["d_mid_past"].corr(x["d_spot_next"]),
                "same_window": x["d_spot_past"].corr(x["d_mid_past"]),
            }
        )
    return pd.DataFrame(rows)


def backtest(
    ds: Dataset,
    horizon: float,
    max_price: float = 0.95,
    min_dist_bps: float = 0.0,
    taker_rate: float = 0.07,
) -> dict[str, float]:
    """Buy one contract of the spot-favoured side at the ask, hold to settlement.

    Returns per-contract averages so results are comparable across sizes.
    """
    df = at_horizon(ds.settled, horizon).dropna(subset=["spot"])
    if df.empty:
        return {}
    df = df.copy()
    above = df["spot_above"].astype(bool)
    df["ask"] = df["yes_ask"].where(above, df["no_ask"])
    df["win"] = (above == (df["won_yes"] == 1.0)).astype(float)
    df = df.dropna(subset=["ask"])
    df = df[(df["ask"] <= max_price) & (df["spot_dist_bps"].abs() >= min_dist_bps)]
    if df.empty:
        return {"n": 0.0}
    gross = df["win"] * (1 - df["ask"]) - (1 - df["win"]) * df["ask"]
    fees = df["ask"].map(lambda p: fee_per_contract(p, taker_rate))
    net = gross - fees
    return {
        "n": float(len(df)),
        "win_rate": float(df["win"].mean()),
        "avg_ask": float(df["ask"].mean()),
        "gross_per_contract": float(gross.mean()),
        "fee_per_contract": float(fees.mean()),
        "net_per_contract": float(net.mean()),
        "net_total": float(net.sum()),
        "breakeven_win_rate": float((df["ask"] + fees).mean()),
    }


def backtest_grid(
    ds: Dataset,
    horizons: tuple[int, ...] | None = None,
    max_prices: tuple[float, ...] = (0.6, 0.8, 0.95),
    min_dists: tuple[float, ...] = (0.0, 2.0, 5.0),
) -> pd.DataFrame:
    rows = []
    for h in horizons or ds.horizons:
        for mp in max_prices:
            for md in min_dists:
                r = backtest(ds, h, max_price=mp, min_dist_bps=md)
                if r.get("n"):
                    rows.append({"horizon_s": h, "max_price": mp, "min_dist_bps": md, **r})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- text report


def report(ds: Dataset) -> str:
    pd.set_option("display.width", 140)
    pd.set_option("display.float_format", lambda v: f"{v:,.3f}")
    out: list[str] = []
    cov = coverage(ds)
    out.append("== coverage")
    out.append(cov.to_string(index=False) if not cov.empty else "(no markets)")
    n_settled = int(ds.markets["won_yes"].notna().sum())
    if n_settled < 20:
        out.append(
            f"\nOnly {n_settled} settled markets; keep recording. "
            "Statistics below are not meaningful under ~100."
        )
    if n_settled == 0:
        return "\n".join(out)

    out.append("\n== brier score by horizon (lower is better; 0.25 = coin flip)")
    rows = [{"horizon_s": h, **brier(ds, h)} for h in ds.horizons]
    out.append(pd.DataFrame([r for r in rows if len(r) > 1]).to_string(index=False))

    for h in (300, 60):
        cal = calibration(ds, h)
        out.append(f"\n== calibration at T-{h}s (implied vs realised YES rate)")
        out.append(cal.to_string(index=False) if not cal.empty else "(no data)")

    for h in (300, 60, 10):
        sig = spot_signal(ds, h)
        out.append(f"\n== spot-vs-strike signal at T-{h}s")
        out.append(sig.to_string(index=False) if not sig.empty else "(no data)")

    ll = lead_lag(ds)
    out.append("\n== lead-lag (does the book follow spot?)")
    out.append(ll.to_string(index=False) if not ll.empty else "(no data)")

    grid = backtest_grid(ds)
    out.append("\n== backtest: buy spot-favoured side at the ask, hold to settlement, per contract")
    out.append(grid.to_string(index=False) if not grid.empty else "(no data)")
    return "\n".join(out)
