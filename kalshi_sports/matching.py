"""Join Kalshi games with odds-feed games and score-feed games.

Three sources name the same game three ways: Kalshi (ticker abbreviations and
a title), the odds feed (full team names and a UTC commence time) and ESPN
(display names, abbreviations, a start time). A match needs the league, the
date within a day (Kalshi's ticker date is Eastern; commence times are UTC and
late games cross midnight) and both teams to agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import teams


@dataclass(frozen=True)
class GameRef:
    league: str
    game_id: str
    start_ts: float | None
    home: str
    away: str


def _date_close(game_date: str | None, start_ts: float | None, tolerance_days: int = 1) -> bool:
    if game_date is None or start_ts is None:
        return True  # cannot rule it out
    d = datetime.fromisoformat(game_date).date()
    s = datetime.fromtimestamp(start_ts, tz=UTC).date()
    return abs((s - d).days) <= tolerance_days


def match_game(
    league: str,
    game_date: str | None,
    away: str | None,
    home: str | None,
    candidates: list[GameRef],
) -> GameRef | None:
    """Pick the candidate naming the same two teams on (about) the same date.

    Home and away are tried both ways: Kalshi's ticker order is being verified,
    and a swapped match is still the right game.
    """
    if not away or not home:
        return None
    hits = []
    for c in candidates:
        if c.league != league or not _date_close(game_date, c.start_ts):
            continue
        straight = teams.same_team(league, away, c.away) and teams.same_team(league, home, c.home)
        swapped = teams.same_team(league, away, c.home) and teams.same_team(league, home, c.away)
        if straight or swapped:
            hits.append(c)
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1 and game_date:
        # Doubleheaders and a day-tolerance overlap: prefer the same Eastern date.
        target = datetime.fromisoformat(game_date).date()
        same_day = [
            c
            for c in hits
            if c.start_ts
            and (datetime.fromtimestamp(c.start_ts, tz=UTC) - timedelta(hours=4)).date() == target
        ]
        if len(same_day) == 1:
            return same_day[0]
    return None
