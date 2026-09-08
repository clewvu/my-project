"""Backtest a passive market-making sub-strategy on recorded 15-minute markets.

The idea under test: instead of only taking when the fair-value model sees an
edge, rest a YES bid and a NO bid a little below fair value and try to earn the
spread. On a binary, if BOTH rest orders fill you hold 1 YES + 1 NO, worth
exactly $1 at settlement, for the (yes_fill + no_fill) < $1 you paid -- a locked
spread. If only one side fills you are left directional, and that is where
adverse selection bites: your bid tends to fill precisely when fair value is
sliding away from it.

Fill model (honest, from real aggressor flow): a resting YES bid at price q
fills, up to the size traded, when a public print shows a taker SELLING yes
(``taker_side == 'no'``) at ``yes_price <= q`` in the interval the quote is live.
Symmetrically for the NO bid. Maker fee on Kalshi is 0, so a hold-to-settlement
maker fill pays no fee; we report gross = settlement value - price paid.

We also measure adverse selection directly as the fair-value markout: for each
fill, fair value ``markout_s`` later minus the price paid. Positive means the
fill was, on average, good; negative means we were picked off.

Run:  python -m kalshi_bot.mm_backtest --offsets 0.005,0.01,0.02
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass, field

import numpy as np

from kalshi_bot import fairvalue


def price_tick(price: float) -> float:
    """Kalshi grid: 0.001 below 10c and above 90c, 0.01 in between."""
    return 0.01 if 0.10 <= price < 0.90 else 0.001


def tick_floor(price: float) -> float:
    """Largest grid price <= price."""
    t = price_tick(price)
    return np.floor(price / t + 1e-9) * t


@dataclass
class Fill:
    ticker: str
    ts: float
    side: str  # "yes" | "no"
    price: float
    qty: float
    fair_yes: float  # model p_yes at fill time (for markout)


@dataclass
class Result:
    offset: float
    fills: list[Fill] = field(default_factory=list)
    per_market: list[dict] = field(default_factory=list)


def load_frames(db: str, series: list[str] | None):
    """fair value per snapshot (reuses the live model) + market outcomes + trades."""
    fv = fairvalue.load(db, series=series or None)
    snaps = fv.snapshots.copy()
    # pick the model probability column (fair value for YES) for the first vol window
    pcol = f"p_{fv.vol_windows[0]}"
    snaps = snaps.dropna(subset=[pcol])
    snaps = snaps.rename(columns={pcol: "p_yes"})
    con = sqlite3.connect(db)
    try:
        markets = {
            r[0]: {"result": r[1], "close_ts": r[2]}
            for r in con.execute("SELECT ticker, result, close_ts FROM markets")
        }
        trades = {}
        for tk, ts, yp, np_, side, cnt in con.execute(
            "SELECT ticker, ts, yes_price, no_price, taker_side, count FROM trades ORDER BY ticker, ts"
        ):
            if ts is None:
                continue
            trades.setdefault(tk, []).append((ts, yp, np_, side, float(cnt or 0.0)))
    finally:
        con.close()
    return snaps, markets, trades


def markout_lookup(snaps_ticker) -> tuple[np.ndarray, np.ndarray]:
    """(ts, p_yes) arrays for a ticker, sorted, for future fair-value lookup."""
    ts = snaps_ticker["ts"].to_numpy(dtype=float)
    p = snaps_ticker["p_yes"].to_numpy(dtype=float)
    order = np.argsort(ts)
    return ts[order], p[order]


def run_offset(snaps, markets, trades, offset, quote_size, max_inv, stop_ttc, markout_s) -> Result:
    res = Result(offset=offset)
    for ticker, g in snaps.groupby("ticker", sort=False):
        mk = markets.get(ticker)
        if not mk or mk["result"] not in ("yes", "no"):
            continue
        g = g.sort_values("ts")
        rows = g.to_dict("records")
        tk_trades = trades.get(ticker, [])
        mo_ts, mo_p = markout_lookup(g)

        yes_inv = no_inv = 0.0
        cash = 0.0  # negative = paid out
        fills: list[Fill] = []
        n = len(rows)
        ntr = len(tk_trades)
        ti = 0  # forward pointer into this ticker's trades (single pass)
        for i in range(n):
            r = rows[i]
            t0 = float(r["ts"])
            t1 = float(rows[i + 1]["ts"]) if i + 1 < n else t0 + 5.0
            fair = float(r["p_yes"])
            yes_ask = float(r.get("yes_ask") or 1.0)
            no_ask = float(r.get("no_ask") or 1.0)
            # passive quotes below fair, on the grid, not crossing the book
            yes_q = tick_floor(fair - offset)
            no_q = tick_floor((1.0 - fair) - offset)
            yes_q = min(yes_q, tick_floor(yes_ask - price_tick(yes_ask)))
            no_q = min(no_q, tick_floor(no_ask - price_tick(no_ask)))
            ttc = float(r.get("secs_to_close") or 0.0)
            quoting = ttc >= stop_ttc  # stop quoting near close; just hold inventory
            # aggressor prints in [t0, t1) via the forward pointer
            yes_hit = no_hit = 0.0
            while ti < ntr and tk_trades[ti][0] < t1:
                tts, yp, np_, side, cnt = tk_trades[ti]
                if quoting and tts >= t0:
                    # taker SELLING yes (== buying no) can hit our resting YES bid
                    if side == "no" and yp is not None and yp <= yes_q + 1e-9:
                        yes_hit += cnt
                    # taker SELLING no (== buying yes) can hit our resting NO bid
                    if side == "yes" and np_ is not None and np_ <= no_q + 1e-9:
                        no_hit += cnt
                ti += 1
            if not quoting:
                continue
            if yes_hit > 0 and yes_inv < max_inv and 0.01 <= yes_q <= 0.99:
                q = min(quote_size, yes_hit, max_inv - yes_inv)
                if q > 0:
                    yes_inv += q
                    cash -= yes_q * q
                    fills.append(Fill(ticker, t0, "yes", yes_q, q, fair))
            if no_hit > 0 and no_inv < max_inv and 0.01 <= no_q <= 0.99:
                q = min(quote_size, no_hit, max_inv - no_inv)
                if q > 0:
                    no_inv += q
                    cash -= no_q * q
                    fills.append(Fill(ticker, t0, "no", no_q, q, fair))
        # settle
        win_yes = 1.0 if mk["result"] == "yes" else 0.0
        settle = yes_inv * win_yes + no_inv * (1.0 - win_yes)
        net = cash + settle  # maker fee = 0
        contracts = yes_inv + no_inv
        paired = min(yes_inv, no_inv)  # locked (guaranteed $1) contracts
        # markout per fill: fair p_yes markout_s later vs fill price (adverse selection)
        mo_sum = 0.0
        mo_n = 0
        for f in fills:
            j = np.searchsorted(mo_ts, f.ts + markout_s)
            if j >= len(mo_p):
                j = len(mo_p) - 1
            p_future = mo_p[j]
            if f.side == "yes":
                mo_sum += (p_future - f.price) * f.qty
            else:
                mo_sum += ((1.0 - p_future) - f.price) * f.qty
            mo_n += f.qty
        res.fills.extend(fills)
        if contracts > 0:
            res.per_market.append({
                "ticker": ticker,
                "net": net,
                "contracts": contracts,
                "yes_inv": yes_inv,
                "no_inv": no_inv,
                "paired": paired,
                "markout": mo_sum,
                "markout_n": mo_n,
            })
    return res


def summarize(res: Result) -> dict:
    pm = res.per_market
    if not pm:
        return {"offset": res.offset, "markets": 0}
    nets = np.array([m["net"] for m in pm])
    contracts = np.array([m["contracts"] for m in pm])
    paired = np.array([m["paired"] for m in pm])
    mo = np.array([m["markout"] for m in pm])
    mo_n = np.array([m["markout_n"] for m in pm])
    total_ct = contracts.sum()
    # bootstrap CI on net per contract, clustered by market
    rng = np.random.default_rng(0)
    boot = []
    idx = np.arange(len(pm))
    for _ in range(2000):
        s = rng.choice(idx, size=len(idx), replace=True)
        c = contracts[s].sum()
        boot.append(nets[s].sum() / c if c else 0.0)
    lo, hi = np.percentile(boot, [2.5, 97.5])
    return {
        "offset": res.offset,
        "markets": len(pm),
        "fills": len(res.fills),
        "contracts": float(total_ct),
        "net_total": float(nets.sum()),
        "net_per_contract": float(nets.sum() / total_ct) if total_ct else 0.0,
        "npc_lo": float(lo),
        "npc_hi": float(hi),
        "paired_frac": float(paired.sum() / total_ct) if total_ct else 0.0,
        "markout_per_contract": float(mo.sum() / mo_n.sum()) if mo_n.sum() else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="state/market_data.sqlite")
    ap.add_argument("--series", action="append")
    ap.add_argument("--offsets", default="0.005,0.01,0.02")
    ap.add_argument("--quote-size", type=float, default=10.0)
    ap.add_argument("--max-inv", type=float, default=30.0)
    ap.add_argument("--stop-ttc", type=float, default=60.0)
    ap.add_argument("--markout", type=float, default=60.0)
    args = ap.parse_args()

    print(f"loading {args.db} ...")
    snaps, markets, trades = load_frames(args.db, args.series)
    print(f"snapshots={len(snaps)}  markets={len(markets)}  "
          f"tickers_with_trades={len(trades)}")
    print(f"\n{'offset':>7} {'mkts':>5} {'fills':>7} {'contracts':>10} "
          f"{'net$':>9} {'net/ct':>8} {'95% CI /ct':>18} {'paired%':>8} {'markout/ct':>11}")
    for off in [float(x) for x in args.offsets.split(",")]:
        res = run_offset(snaps, markets, trades, off, args.quote_size,
                         args.max_inv, args.stop_ttc, args.markout)
        s = summarize(res)
        if not s.get("markets"):
            print(f"{off:>7.3f}  (no fills)")
            continue
        print(f"{off:>7.3f} {s['markets']:>5} {s['fills']:>7} {s['contracts']:>10.0f} "
              f"{s['net_total']:>9.2f} {s['net_per_contract']:>8.4f} "
              f"[{s['npc_lo']:>7.4f},{s['npc_hi']:>7.4f}] {100*s['paired_frac']:>7.1f}% "
              f"{s['markout_per_contract']:>11.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
