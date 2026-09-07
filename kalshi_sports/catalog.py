"""Map Kalshi sports markets to games.

Grammar confirmed against production on 2026-09-07 (`kalshi-sports discover`):

    KXMLBGAME-26SEP101610TEXSEA-TEX       MLB: date, start time HHMM Eastern, AWAY HOME, side
    KXNFLGAME-26SEP21NYGLAR-NYG           NFL/NBA/college: date, AWAY HOME, side (no time)
    KXMLBSPREAD-26SEP071510MINDET-DET6    spread: side is TEAM + digits; line is floor_strike (5.5)
    KXMLBTOTAL-26SEP071510MINDET-14       total: side is the integer above the line; line 13.5
    KXNCAAFGAME-26SEP19PURUCLA-UCLA       college abbreviations vary in length (PUR, UCLA, SJSU)

Kalshi's `rules_primary` always names the matchup and the schedule:
    "If Texas wins the Texas vs Seattle professional baseball game originally
     scheduled for Sep 10, 2026 at 4:10 PM EDT, then the market resolves to Yes."
The first team in "A vs B" is the away team (matches the ticker order and ESPN).
Close time is three days after the game, so it is not the start time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from kalshi_bot.models import Market

from . import leagues, teams

EASTERN = ZoneInfo("America/New_York")

_TAIL = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})(\d{4})?([A-Z].*)?$")
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1
    )
}
_AT_OR_VS = re.compile(r"^(.*?)\s+(?:at|@|vs\.?|v\.?)\s+(.*?)(?:\s*[:(-].*)?$", re.IGNORECASE)
_RULES_MATCHUP = re.compile(
    r"\bthe\s+(.+?)\s+vs\.?\s+(.+?)\s+"
    r"(?:professional|pro|college|men's|women's|[a-z]+)?\s*(?:baseball|football|basketball)?\s*game\b",
    re.IGNORECASE,
)
_RULES_WHEN = re.compile(
    r"scheduled for\s+([A-Z][a-z]{2,8}\.? \d{1,2}, \d{4})"
    r"(?:\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)\s*(E[DS]T)?)?",
    re.IGNORECASE,
)
_TRAILING_DIGITS = re.compile(r"\d+$")
_LINE = re.compile(r"[-+]?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class GameMarket:
    ticker: str
    event_ticker: str | None
    series_ticker: str
    league: str | None  # mlb, nfl, ...
    kind: str  # moneyline, spread, total, series, future, unknown
    game_date: str | None  # YYYY-MM-DD, Eastern, from the ticker
    away: str | None  # display name as Kalshi writes it ("Texas", "Los Angeles D", "San Jose St.")
    home: str | None
    away_abbr: str | None  # from the ticker (TEX, LAD, SJSU)
    home_abbr: str | None
    side: str | None  # raw ticker suffix: TEX, DET6, 14
    side_team: str | None  # team abbreviation YES refers to, digits stripped; None for totals
    side_name: str | None  # display name of that team, or "over" for totals
    line: float | None  # spread or total line
    start_ts: float | None  # game start, if known (ticker time or rules text or event)
    start_exact: bool  # False when start_ts is a date-only approximation
    title: str
    subtitle: str | None
    rules: str | None
    exchange_index: int | None

    def key(self) -> str | None:
        a, h = self.away_abbr or self.away, self.home_abbr or self.home
        if not (self.league and self.game_date and a and h):
            return None
        return f"{self.league}:{self.game_date}:{a}:{h}"


def parse_ticker_tail(tail: str) -> tuple[str | None, str | None, str]:
    """'26SEP101610TEXSEA' -> ('2026-09-10', '16:10', 'TEXSEA').

    '26SEP21NYGLAR' -> ('2026-09-21', None, 'NYGLAR').
    """
    m = _TAIL.match(tail.upper())
    if not m or m.group(2) not in _MONTHS:
        return None, None, tail
    yy, mon, dd, hhmm, rest = m.groups()
    try:
        date = datetime(2000 + int(yy), _MONTHS[mon], int(dd)).date().isoformat()
    except ValueError:
        return None, None, tail
    time = f"{hhmm[:2]}:{hhmm[2:]}" if hhmm else None
    return date, time, rest or ""


def split_teams(pair: str, league: str | None, side: str | None) -> tuple[str | None, str | None]:
    """Split 'NYYBOS' into ('NYY', 'BOS') using the league table, else the YES side as anchor."""
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


def teams_from_rules(rules: str | None) -> tuple[str | None, str | None]:
    """'... the Texas vs Seattle professional baseball game ...' -> ('Texas', 'Seattle').

    The last "the A vs B ... game" in the text wins: totals rules start with
    "If the teams collectively score ...", which the first "the" would catch.
    """
    if not rules:
        return None, None
    matches = list(_RULES_MATCHUP.finditer(rules))
    if not matches:
        return None, None
    m = matches[-1]
    away, home = m.group(1).strip(), m.group(2).strip()
    # A greedy-from-the-left match can still swallow a clause; keep the tail after the last " the ".
    if " the " in f" {away}":
        away = away.rsplit(" the ", 1)[-1].strip()
    return away, home


def start_from_rules(rules: str | None) -> tuple[float | None, bool]:
    """Start time from 'originally scheduled for Sep 10, 2026 at 4:10 PM EDT'.

    Returns (timestamp, exact). Without a clock time the date alone is used at
    13:00 Eastern and flagged approximate.
    """
    if not rules:
        return None, False
    m = _RULES_WHEN.search(rules)
    if not m:
        return None, False
    date_s, time_s, _tz = m.groups()
    for fmt in ("%b %d, %Y", "%b. %d, %Y", "%B %d, %Y"):
        try:
            d = datetime.strptime(date_s, fmt)
            break
        except ValueError:
            continue
    else:
        return None, False
    if time_s:
        try:
            t = datetime.strptime(time_s.replace(" ", "").upper(), "%I:%M%p")
            return d.replace(hour=t.hour, minute=t.minute, tzinfo=EASTERN).timestamp(), True
        except ValueError:
            pass
    return d.replace(hour=13, tzinfo=EASTERN).timestamp(), False


def _eastern_ts(date: str, time: str | None) -> tuple[float | None, bool]:
    try:
        d = datetime.fromisoformat(date)
    except ValueError:
        return None, False
    if time:
        hh, mm = time.split(":")
        return d.replace(hour=int(hh), minute=int(mm), tzinfo=EASTERN).timestamp(), True
    return d.replace(hour=13, tzinfo=EASTERN).timestamp(), False


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _event_start(event: dict[str, Any] | None) -> float | None:
    if not event:
        return None
    for k in ("strike_date", "start_time"):
        v = event.get(k)
        if v:
            try:
                return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
    return None


def classify(market: Market, event: dict[str, Any] | None = None) -> GameMarket:
    raw = market.raw or {}
    lg = leagues.league_for_series(market.series_ticker)
    league_key = lg.key if lg else None
    kind = lg.series_kind(market.series_ticker) if lg else leagues.UNKNOWN
    rules = _first(raw, "rules_primary", "rules_secondary", "settlement_rules")
    subtitle = _first(raw, "yes_sub_title", "subtitle")

    parts = market.ticker.split("-")
    date, time_et, pair, side = None, None, "", None
    if len(parts) >= 2:
        date, time_et, pair = parse_ticker_tail(parts[1])
    if len(parts) >= 3:
        side = "-".join(parts[2:])

    side_team: str | None = None
    if side and kind != leagues.TOTAL and not side.isdigit():
        side_team = _TRAILING_DIGITS.sub("", side.upper()) or None

    line: float | None = None
    if kind in (leagues.SPREAD, leagues.TOTAL):
        if market.strike is not None:
            line = float(market.strike)
        else:
            m = _LINE.search(str(subtitle or market.title or ""))
            line = float(m.group(0)) if m else None

    away_abbr, home_abbr = split_teams(pair, league_key, side_team) if pair else (None, None)
    away, home = teams_from_rules(str(rules) if rules else None)
    if away is None or home is None:
        away, home = teams_from_title((event or {}).get("title") or "")
    if away is None or home is None:
        away, home = teams_from_title(market.title)
    if away is None:
        away = away_abbr
    if home is None:
        home = home_abbr

    side_name: str | None
    if kind == leagues.TOTAL:
        side_name = "over"
    elif side_team and away_abbr and side_team == away_abbr.upper():
        side_name = away
    elif side_team and home_abbr and side_team == home_abbr.upper():
        side_name = home
    elif kind == leagues.MONEYLINE and subtitle:
        side_name = str(subtitle)
    else:
        side_name = None

    start_ts, exact = (None, False)
    if date:
        start_ts, exact = _eastern_ts(date, time_et)
    if not exact:
        r_ts, r_exact = start_from_rules(str(rules) if rules else None)
        if r_ts is not None and (r_exact or start_ts is None):
            start_ts, exact = r_ts, r_exact
    if not exact:
        e_ts = _event_start(event)
        if e_ts is not None:
            start_ts, exact = e_ts, True

    return GameMarket(
        ticker=market.ticker,
        event_ticker=market.event_ticker,
        series_ticker=market.series_ticker,
        league=league_key,
        kind=kind,
        game_date=date,
        away=away,
        home=home,
        away_abbr=away_abbr,
        home_abbr=home_abbr,
        side=side,
        side_team=side_team,
        side_name=side_name,
        line=line,
        start_ts=start_ts,
        start_exact=exact,
        title=market.title,
        subtitle=str(subtitle) if subtitle else None,
        rules=str(rules) if rules else None,
        exchange_index=market.exchange_index,
    )


def date_from_ts(ts: float) -> str:
    """Eastern calendar date of a timestamp, matching the ticker's date convention."""
    return datetime.fromtimestamp(ts, tz=UTC).astimezone(EASTERN).date().isoformat()
