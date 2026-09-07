"""De-vigged sportsbook consensus per game, market and line, from the odds table.

For each book, the outcomes of one market (both teams on h2h; over and under
on a total; both sides of a spread at the same absolute line) are de-vigged
together. The consensus is a weighted average across books, sharp books
counting more, with the dispersion across books reported so a strategy can
refuse to trust a price the books disagree on.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass

from .feeds.devig import devig
from .feeds.odds import SHARP_BOOKS

SHARP_WEIGHT = 3.0


@dataclass(frozen=True)
class Consensus:
    league: str
    game_id: str
    market: str  # h2h, spreads, totals
    point: float | None  # absolute line for spreads/totals, None for h2h
    probs: dict[str, float]  # outcome -> de-vigged probability
    n_books: int
    dispersion: float  # std dev across books of the first outcome's probability
    age_s: float  # seconds since the newest quote used
    sharp_books: int

    def p(self, outcome: str) -> float | None:
        return self.probs.get(outcome)


def _rows_for_game(
    conn: sqlite3.Connection, league: str, game_id: str, since: float
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM odds WHERE league = ? AND game_id = ? AND ts >= ?
        ORDER BY ts ASC
        """,
        (league, game_id, since),
    ).fetchall()


def consensus_for_game(
    conn: sqlite3.Connection,
    league: str,
    game_id: str,
    *,
    now: float,
    max_age_s: float = 3 * 3600,
    method: str = "multiplicative",
) -> list[Consensus]:
    """Consensus for every (market, line) on one game from quotes at most ``max_age_s`` old."""
    rows = _rows_for_game(conn, league, game_id, now - max_age_s)
    # latest quote per (book, market, point-group, outcome)
    latest: dict[tuple[str, str, float | None, str], sqlite3.Row] = {}
    for r in rows:
        point = r["point"]
        group = abs(point) if (point is not None and r["market"] == "spreads") else point
        latest[(r["book"], r["market"], group, r["outcome"])] = r

    per_market: dict[tuple[str, float | None], dict[str, dict[str, sqlite3.Row]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for (book, market, group, outcome), r in latest.items():
        per_market[(market, group)][book][outcome] = r

    out: list[Consensus] = []
    for (market, group), books in per_market.items():
        acc: dict[str, list[tuple[float, float]]] = defaultdict(list)  # outcome -> (p, w)
        first_outcome_ps: list[float] = []
        newest = 0.0
        sharp = 0
        used = 0
        for book, outcomes in books.items():
            if len(outcomes) < 2:
                continue  # need the full market to de-vig
            names = sorted(outcomes)
            decs = [outcomes[n]["price_decimal"] for n in names]
            try:
                ps = devig(decs, method)
            except (ValueError, ZeroDivisionError):
                continue
            w = SHARP_WEIGHT if book in SHARP_BOOKS else 1.0
            sharp += book in SHARP_BOOKS
            used += 1
            for n, p in zip(names, ps, strict=True):
                acc[n].append((p, w))
            first_outcome_ps.append(ps[0])
            newest = max(newest, max(outcomes[n]["ts"] for n in names))
        if not used:
            continue
        probs = {n: sum(p * w for p, w in v) / sum(w for _, w in v) for n, v in acc.items()}
        disp = statistics.pstdev(first_outcome_ps) if len(first_outcome_ps) > 1 else 0.0
        out.append(
            Consensus(
                league=league,
                game_id=game_id,
                market=market,
                point=group,
                probs=probs,
                n_books=used,
                dispersion=disp,
                age_s=max(0.0, now - newest),
                sharp_books=sharp,
            )
        )
    return out


def recent_games(conn: sqlite3.Connection, league: str | None, since: float) -> list[sqlite3.Row]:
    """Distinct (league, game_id, commence_ts, home, away) with quotes since ``since``."""
    sql = """
        SELECT league, game_id, MAX(commence_ts) AS commence_ts, MAX(home) AS home,
               MAX(away) AS away
        FROM odds WHERE ts >= ?
    """
    args: list[object] = [since]
    if league:
        sql += " AND league = ?"
        args.append(league)
    sql += " GROUP BY league, game_id"
    return conn.execute(sql, args).fetchall()
