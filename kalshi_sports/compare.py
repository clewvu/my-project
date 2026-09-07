"""First evidence table: Kalshi's price against the de-vigged sportsbook consensus.

For every open moneyline market with a recent snapshot, find the same game in
the odds table, take the consensus probability of the YES team, and report
the gap against Kalshi's ask on each side net of Kalshi's fee. This is the
raw material of hypothesis H1; it is a report, not a trading rule.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from kalshi_bot.fees import fee_per_contract

from . import teams
from .consensus import Consensus, consensus_for_game, recent_games
from .matching import GameRef, match_game


@dataclass(frozen=True)
class Comparison:
    ticker: str
    league: str
    game: str
    side: str
    yes_bid: float | None
    yes_ask: float | None
    p_consensus: float | None
    n_books: int
    dispersion: float
    age_s: float
    secs_to_start: float | None
    edge_yes: float | None  # p - ask - fee  (buy YES)
    edge_no: float | None  # (1-p) - no_ask - fee  (buy NO)
    note: str


def _side_prob(
    cons: Consensus, league: str, side: str | None, home: str, away: str
) -> float | None:
    if not side:
        return None
    for outcome, p in cons.probs.items():
        if teams.same_team(league, side, outcome):
            return p
    # Kalshi side may be an abbreviation the college matcher cannot resolve; try home/away.
    for team, outcome in ((home, home), (away, away)):
        if teams.same_team(league, side, team):
            return cons.probs.get(outcome)
    return None


def compare(
    conn: sqlite3.Connection,
    *,
    now: float,
    league: str | None = None,
    max_snapshot_age_s: float = 3600.0,
    max_odds_age_s: float = 3 * 3600,
    method: str = "multiplicative",
) -> list[Comparison]:
    games = recent_games(conn, league, now - max_odds_age_s)
    refs = [
        GameRef(g["league"], g["game_id"], g["commence_ts"], g["home"] or "", g["away"] or "")
        for g in games
    ]
    sql = """
        SELECT m.ticker, m.league, m.game_date, m.away, m.home, m.side, m.event_ticker,
               s.yes_bid, s.yes_ask, s.no_ask, s.secs_to_start, s.ts
        FROM markets m
        JOIN snapshots s ON s.id = (
            SELECT id FROM snapshots WHERE ticker = m.ticker ORDER BY ts DESC LIMIT 1
        )
        WHERE m.kind = 'moneyline' AND m.result IS NULL AND s.ts >= ?
    """
    args: list[object] = [now - max_snapshot_age_s]
    if league:
        sql += " AND m.league = ?"
        args.append(league)
    out: list[Comparison] = []
    cache: dict[tuple[str, str], list[Consensus]] = {}
    for r in conn.execute(sql, args):
        lg = r["league"] or ""
        game = f"{r['away'] or '?'} @ {r['home'] or '?'} {r['game_date'] or ''}".strip()
        ref = match_game(lg, r["game_date"], r["away"], r["home"], refs)
        if ref is None:
            out.append(
                Comparison(
                    r["ticker"],
                    lg,
                    game,
                    r["side"] or "",
                    r["yes_bid"],
                    r["yes_ask"],
                    None,
                    0,
                    0.0,
                    0.0,
                    r["secs_to_start"],
                    None,
                    None,
                    "no odds match",
                )
            )
            continue
        key = (ref.league, ref.game_id)
        if key not in cache:
            cache[key] = consensus_for_game(
                conn, ref.league, ref.game_id, now=now, max_age_s=max_odds_age_s, method=method
            )
        h2h = next((c for c in cache[key] if c.market == "h2h"), None)
        if h2h is None:
            out.append(
                Comparison(
                    r["ticker"],
                    lg,
                    game,
                    r["side"] or "",
                    r["yes_bid"],
                    r["yes_ask"],
                    None,
                    0,
                    0.0,
                    0.0,
                    r["secs_to_start"],
                    None,
                    None,
                    "no h2h consensus",
                )
            )
            continue
        p = _side_prob(h2h, lg, r["side"], ref.home, ref.away)
        if p is None:
            out.append(
                Comparison(
                    r["ticker"],
                    lg,
                    game,
                    r["side"] or "",
                    r["yes_bid"],
                    r["yes_ask"],
                    None,
                    h2h.n_books,
                    h2h.dispersion,
                    h2h.age_s,
                    r["secs_to_start"],
                    None,
                    None,
                    "side not matched to an outcome",
                )
            )
            continue
        yes_ask, no_ask = r["yes_ask"], r["no_ask"]
        edge_yes = p - yes_ask - fee_per_contract(yes_ask) if yes_ask else None
        edge_no = (1 - p) - no_ask - fee_per_contract(no_ask) if no_ask else None
        out.append(
            Comparison(
                r["ticker"],
                lg,
                game,
                r["side"] or "",
                r["yes_bid"],
                yes_ask,
                p,
                h2h.n_books,
                h2h.dispersion,
                h2h.age_s,
                r["secs_to_start"],
                edge_yes,
                edge_no,
                "",
            )
        )
    out.sort(key=lambda c: -max(c.edge_yes or -1, c.edge_no or -1))
    return out


def format_table(rows: list[Comparison]) -> str:
    def pc(x: float | None) -> str:
        return "   -  " if x is None else f"{x * 100:6.1f}"

    lines = [
        f"{'ticker':<34} {'game':<28} {'side':<6} {'bid':>6} {'ask':>6} {'cons':>6} "
        f"{'eY':>6} {'eN':>6} {'bk':>3} {'disp':>5} {'age':>5} note"
    ]
    for c in rows:
        age = "-" if not c.age_s else f"{c.age_s / 60:4.0f}m"
        lines.append(
            f"{c.ticker:<34.34} {c.game:<28.28} {c.side:<6.6} "
            f"{pc(c.yes_bid):>6} {pc(c.yes_ask):>6} "
            f"{pc(c.p_consensus):>6} {pc(c.edge_yes):>6} {pc(c.edge_no):>6} {c.n_books:>3} "
            f"{c.dispersion * 100:5.1f} {age:>5} {c.note}"
        )
    return "\n".join(lines)
