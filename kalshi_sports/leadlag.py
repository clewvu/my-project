"""Does Kalshi's sports moneyline LAG the sharp-book consensus?

The whole sports thesis is that Kalshi is slow to reprice after the sharp books
move, leaving a gap to trade. This measures it directly from recorded data:

For each game, build two aligned series -- the de-vigged sharp consensus for the
market's YES team, and Kalshi's yes-mid -- then ask, when a gap opens between
them, which side moves to close it over the next H seconds:

  kalshi_move(t) = kalshi(t+H) - kalshi(t)   regressed on  gap(t) = cons(t) - kalshi(t)
  cons_move(t)   = cons(t+H)  - cons(t)       regressed on  -gap(t)

A positive Kalshi coefficient means Kalshi converges toward the consensus -- the
lag is real and tradeable. If the consensus coefficient is just as large, the two
merely track each other and there is no edge. If gaps are smaller than the fee,
there is nothing to trade regardless.

    python -m kalshi_sports.leadlag
"""

from __future__ import annotations

import argparse
import sqlite3

import numpy as np

from kalshi_sports.compare import _side_prob
from kalshi_sports.consensus import consensus_for_game, recent_games
from kalshi_sports.matching import GameRef, match_game


def _refs(conn, league, at):
    rows = recent_games(conn, league, at - 3 * 86400)
    return [GameRef(r["league"], r["game_id"], r["commence_ts"], r["home"] or "", r["away"] or "")
            for r in rows]


def series_for_market(conn, mk, grid_s, method):
    """Aligned (ts, kalshi_mid, consensus_p) for one market's pre-game window."""
    start = mk["start_ts"]
    if start is None:
        return []
    refs = _refs(conn, mk["league"], start)
    ref = match_game(mk["league"], mk["game_date"],
                     mk["away_abbr"] or mk["away"], mk["home_abbr"] or mk["home"],
                     refs, alt_away=mk["away"], alt_home=mk["home"])
    if ref is None:
        return []
    snaps = conn.execute(
        "SELECT ts, yes_bid, yes_ask FROM snapshots WHERE ticker=? AND ts>=? AND ts<=? "
        "AND yes_bid>0 AND yes_ask>0 AND yes_ask>yes_bid ORDER BY ts",
        (mk["ticker"], start - 24 * 3600, start),
    ).fetchall()
    if len(snaps) < 4:
        return []
    # resample kalshi mid onto a grid (last value in each bucket)
    grid = {}
    for s in snaps:
        b = int(s["ts"] // grid_s)
        grid[b] = (s["ts"], (s["yes_bid"] + s["yes_ask"]) / 2.0)
    out = []
    for b in sorted(grid):
        ts, mid = grid[b]
        cons = consensus_for_game(conn, mk["league"], ref.game_id, now=ts, method=method)
        h2h = next((c for c in cons if c.market == "h2h"), None)
        if h2h is None:
            continue
        p = _side_prob(h2h, mk["league"], mk["side_team"], mk["side_name"])
        if p is None:
            continue
        out.append((ts, mid, p))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="state/sports_data.sqlite")
    ap.add_argument("--grid", type=float, default=300.0, help="resample seconds")
    ap.add_argument("--horizon", type=float, default=600.0, help="lead-lag horizon seconds")
    ap.add_argument("--max-markets", type=int, default=80)
    ap.add_argument("--method", default="multiplicative")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    # moneyline markets that actually have snapshots, most-covered first
    mkts = conn.execute(
        """SELECT m.ticker, m.league, m.game_date, m.away, m.home, m.away_abbr, m.home_abbr,
                  m.side_team, m.side_name, e.start_ts, COUNT(s.id) AS n
           FROM markets m JOIN events e ON m.event_ticker = e.event_ticker
           JOIN snapshots s ON s.ticker = m.ticker
           WHERE m.kind='moneyline' AND e.start_ts IS NOT NULL
           GROUP BY m.ticker HAVING n >= 8
           ORDER BY n DESC LIMIT ?""",
        (args.max_markets,),
    ).fetchall()
    print(f"analysing {len(mkts)} moneyline markets (grid {args.grid:.0f}s, horizon {args.horizon:.0f}s)")

    H = int(round(args.horizon / args.grid))  # steps
    gaps, kmoves, cmoves = [], [], []
    used = 0
    for mk in mkts:
        ser = series_for_market(conn, mk, args.grid, args.method)
        if len(ser) < H + 2:
            continue
        used += 1
        for i in range(len(ser) - H):
            _, k0, c0 = ser[i]
            _, kH, cH = ser[i + H]
            gaps.append(c0 - k0)
            kmoves.append(kH - k0)
            cmoves.append(cH - c0)
    g = np.array(gaps); km = np.array(kmoves); cm = np.array(cmoves)
    n = len(g)
    print(f"markets with usable series: {used} | paired observations: {n}")
    if n < 30:
        print("not enough data to conclude"); return 0

    def reg(x, y):
        # slope, correlation
        if x.std() == 0:
            return 0.0, 0.0
        b = np.cov(x, y)[0, 1] / x.var()
        r = np.corrcoef(x, y)[0, 1]
        return b, r

    bk, rk = reg(g, km)          # does Kalshi move toward consensus?
    bc, rc = reg(-g, cm)         # does consensus move toward Kalshi?
    print("\n=== lead-lag over the horizon ===")
    print(f"  |gap| (cons - kalshi):  mean {np.abs(g).mean():.3f}  median {np.median(np.abs(g)):.3f}  "
          f">2c: {100*np.mean(np.abs(g) > 0.02):.0f}%  >3c: {100*np.mean(np.abs(g) > 0.03):.0f}%")
    print(f"  Kalshi -> consensus:  slope {bk:+.3f}  corr {rk:+.3f}   "
          f"(positive = Kalshi lags and converges = EDGE)")
    print(f"  consensus -> Kalshi:  slope {bc:+.3f}  corr {rc:+.3f}   "
          f"(if similar, they just track each other)")
    # tradeable read: of the gap, how much does Kalshi close over the horizon?
    print(f"\n  Kalshi closes ~{100*bk:.0f}% of the gap per {args.horizon:.0f}s.")
    big = np.abs(g) > 0.03
    if big.sum() >= 20:
        # when gap>3c, does kalshi move the right way by more than the fee (~1.75c)?
        signed = np.sign(g[big]) * km[big]
        print(f"  when |gap|>3c (n={big.sum()}): Kalshi moves toward consensus by "
              f"{signed.mean()*100:+.2f}c on average over {args.horizon:.0f}s "
              f"(fee ~1.75c/contract round trip).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
