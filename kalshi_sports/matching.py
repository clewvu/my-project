"""Join Kalshi games with odds-feed games and score-feed games.

Three sources name the same game three ways: Kalshi (ticker abbreviations,
plus display names such as "Los Angeles D" or "San Jose St."), the odds feed
(full team names and a UTC commence time) and ESPN (display names,
abbreviations, a start time). A match needs the league, the date within a day
(Kalshi's ticker date is Eastern; commence times are UTC and late games cross
midnight) and both teams to agree by abbreviation or by name.
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
    home_abbr: str | None = None
    away_abbr: str | None = None


def _date_close(game_date: str | None, start_ts: float | None, tolerance_days: int = 1) -> bool:
    if game_date is None or start_ts is None:
        return True  # cannot rule it out
    d = datetime.fromisoformat(game_date).date()
    s = datetime.fromtimestamp(start_ts, tz=UTC).date()
    return abs((s - d).days) <= tolerance_days


def _same(league: str, ours: str | None, alt: str | None, theirs: str, their_abbr: str | None):
    """Our team (abbreviation or name, with an alternate spelling) against theirs."""
    for mine in (ours, alt):
        if not mine:
            continue
        if their_abbr and teams.canonical_abbr(league, mine) == teams.canonical_abbr(
            league, their_abbr
        ):
            return True
        if teams.same_team(league, mine, theirs):
            return True
    return False


def match_game(
    league: str,
    game_date: str | None,
    away: str | None,
    home: str | None,
    candidates: list[GameRef],
    *,
    alt_away: str | None = None,
    alt_home: str | None = None,
) -> GameRef | None:
    """Pick the candidate naming the same two teams on (about) the same date.

    Home and away are tried both ways: a swapped match is still the right game.
    """
    if not (away or alt_away) or not (home or alt_home):
        return None
    hits = []
    for c in candidates:
        if c.league != league or not _date_close(game_date, c.start_ts):
            continue
        straight = _same(league, away, alt_away, c.away, c.away_abbr) and _same(
            league, home, alt_home, c.home, c.home_abbr
        )
        swapped = _same(league, away, alt_away, c.home, c.home_abbr) and _same(
            league, home, alt_home, c.away, c.away_abbr
        )
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
