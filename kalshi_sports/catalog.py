"""Map Kalshi sports markets to games.

A Kalshi market on a game carries the information we need spread across the
ticker, the event ticker, the title and the sub-titles. The exact grammar is
being verified against production (``kalshi-sports discover --raw``); this
parser is tolerant and records what it could not determine as ``None`` rather
than guessing.

Ticker shape assumed (from the crypto series and Kalshi's public examples):

    <SERIES>-<YY><MON><DD><AWAY><HOME>-<SIDE>     e.g. KXMLBGAME-26SEP07NYYBOS-NYY
    <SERIES>-<YY><MON><DD><AWAY><HOME>-<LINE>     e.g. KXMLBTOTAL-26SEP07NYYBOS-8.5

Home and away are also read from the title ("Yankees at Red Sox" or
"Yankees vs Red Sox", where the second team is home by US convention).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kalshi_bot.models import Market

from . import leagues, teams

_DATE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})(.*)$")
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1
    )
}
_AT_OR_VS = re.compile(r"^(.*?)\s+(?:at|@|vs\.?|v\.?)\s+(.*?)(?:\s*[:(-].*)?$", re.IGNORECASE)
_LINE = re.compile(r"[-+]?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class GameMarket:
    ticker: str
    event_ticker: str | None
    series_ticker: str
    league: str | None  # mlb, nfl, ...
    kind: str  # moneyline, spread, total, series, future, unknown
    game_date: str | None  # YYYY-MM-DD in the ticker (Eastern date, as Kalshi writes it)
    away: str | None  # team string as written on Kalshi (abbreviation or name)
    home: str | None
    side: str | None  # the team or outcome YES refers to, e.g. NYY, or "over"
    line: float | None  # spread or total line, if any
    start_ts: float | None  # game start if known from the event
    title: str
    subtitle: str | None
    rules: str | None
    exchange_index: int | None

    def key(self) -> str | None:
        """Stable game key for joining with odds and scores: league:date:away:home."""
        if not (self.league and self.game_date and self.away and self.home):
            return None
        return f"{self.league}:{self.game_date}:{self.away}:{self.home}"


def parse_ticker_tail(tail: str) -> tuple[str | None, str]:
    """Split '26SEP07NYYBOS' into ('2026-09-07', 'NYYBOS')."""
    m = _DATE.match(tail.upper())
    if not m or m.group(2) not in _MONTHS:
        return None, tail
    yy, mon, dd, rest = m.groups()
    try:
        date = datetime(2000 + int(yy), _MONTHS[mon], int(dd), tzinfo=UTC).date().isoformat()
    except ValueError:
        return None, tail
    return date, rest


def split_teams(pair: str, league: str | None, side: str | None) -> tuple[str | None, str | None]:
    """Split 'NYYBOS' into ('NYY', 'BOS').

    Abbreviations vary in length, so use the league table when there is one,
    else the YES side (which is one of the two teams) as the anchor.
    """
    pair = pair.upper()
    table = teams.TABLES.get(league or "")
    if table:
        for i in range(2, min(4, len(pair) - 1) + 1):
            a, h = pair[:i], pair[i:]
            if (
                teams.canonical_abbr(league, a) in table
                and teams.canonical_abbr(league, h) in table
            ):
                return a, h
    if side:
        s = side.upper()
        if pair.startswith(s) and len(pair) > len(s):
            return s, pair[len(s) :]
        if pair.endswith(s) and len(pair) > len(s):
            return pair[: -len(s)], s
    return None, None


def teams_from_title(title: str) -> tuple[str | None, str | None]:
    m = _AT_OR_VS.match(title.strip())
    if not m:
        return None, None
    a, h = m.group(1).strip(), m.group(2).strip()
    return (a or None), (h or None)


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def classify(market: Market, event: dict[str, Any] | None = None) -> GameMarket:
    raw = market.raw or {}
    lg = leagues.league_for_series(market.series_ticker)
    league_key = lg.key if lg else None
    kind = lg.series_kind(market.series_ticker) if lg else leagues.UNKNOWN

    parts = market.ticker.split("-")
    date, pair, side = None, "", None
    if len(parts) >= 2:
        date, pair = parse_ticker_tail(parts[1])
    if len(parts) >= 3:
        side = "-".join(parts[2:])

    line: float | None = None
    if kind in (leagues.SPREAD, leagues.TOTAL):
        strike = market.strike
        if strike is not None:
            line = float(strike)
        elif side and _LINE.fullmatch(side.replace("P", ".").replace("N", "-")):
            line = float(side.replace("P", ".").replace("N", "-"))
        else:
            m = _LINE.search(str(_first(raw, "yes_sub_title", "subtitle") or ""))
            line = float(m.group(0)) if m else None

    away, home = None, None
    if pair:
        away, home = split_teams(pair, league_key, side if kind != leagues.TOTAL else None)
    event_title = (event or {}).get("title") or ""
    if away is None or home is None:
        away, home = teams_from_title(event_title or market.title)
    if away is None or home is None:
        t_away, t_home = teams_from_title(market.title)
        away, home = away or t_away, home or t_home

    start_ts = None
    if event:
        for k in ("strike_date", "expected_expiration_time", "start_time"):
            v = event.get(k)
            if v:
                try:
                    start_ts = datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
                    break
                except ValueError:
                    continue

    return GameMarket(
        ticker=market.ticker,
        event_ticker=market.event_ticker,
        series_ticker=market.series_ticker,
        league=league_key,
        kind=kind,
        game_date=date,
        away=away,
        home=home,
        side=side,
        line=line,
        start_ts=start_ts,
        title=market.title,
        subtitle=_first(raw, "yes_sub_title", "subtitle"),
        rules=_first(raw, "rules_primary", "rules_secondary", "settlement_rules"),
        exchange_index=market.exchange_index,
    )
