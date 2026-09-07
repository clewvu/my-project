"""Live game state from ESPN's public scoreboard endpoint (no key, unofficial).

    GET https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard
        [?dates=YYYYMMDD][&groups=..][&limit=..]

One request per league per poll covers every game that day: status
(pre/in/post), period or inning, clock, and the score. That is enough for the
stale-data guards and the settlement cross-check in phase 1; a pitch-level
MLB feed (statsapi.mlb.com) is a later addition for the in-play model.
Field names are read defensively; ``kalshi-sports scores-test`` shows the raw
record for one game so they can be confirmed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from ..leagues import League

log = logging.getLogger(__name__)

ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"


@dataclass(frozen=True)
class GameState:
    ts: float
    source: str
    league: str
    game_id: str
    start_ts: float | None
    home: str
    away: str
    home_abbr: str | None
    away_abbr: str | None
    state: str  # pre, in, post
    detail: str | None  # e.g. "Bottom 7th", "Final", "Postponed"
    period: int | None
    clock: str | None
    home_score: int | None
    away_score: int | None
    raw: dict[str, Any]

    def row(self) -> tuple[Any, ...]:
        import json

        slim = {
            "home_abbr": self.home_abbr,
            "away_abbr": self.away_abbr,
            "situation": self.raw.get("situation"),
            "status": self.raw.get("status"),
            "name": self.raw.get("name"),
        }
        return (
            self.ts,
            self.source,
            self.league,
            self.game_id,
            self.start_ts,
            self.home,
            self.away,
            self.state,
            self.detail,
            self.period,
            self.clock,
            self.home_score,
            self.away_score,
            json.dumps(slim, default=str, separators=(",", ":")),
        )


class ScoreFeed(Protocol):
    source: str

    def fetch(self, league: League, *, date: str | None = None) -> list[GameState]: ...

    def close(self) -> None: ...


def _ts(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_espn(
    payload: dict[str, Any], league: str, now: float, source: str = "espn"
) -> list[GameState]:
    out: list[GameState] = []
    for ev in payload.get("events", []) or []:
        comps = ev.get("competitions") or [{}]
        comp = comps[0] if comps else {}
        status = comp.get("status") or ev.get("status") or {}
        stype = status.get("type") or {}
        home_name = away_name = ""
        home_abbr = away_abbr = None
        home_score = away_score = None
        for c in comp.get("competitors", []) or []:
            team = c.get("team") or {}
            name = team.get("displayName") or team.get("name") or team.get("location") or ""
            abbr = team.get("abbreviation")
            score = _int(c.get("score"))
            if c.get("homeAway") == "home":
                home_name, home_abbr, home_score = name, abbr, score
            else:
                away_name, away_abbr, away_score = name, abbr, score
        out.append(
            GameState(
                ts=now,
                source=source,
                league=league,
                game_id=str(ev.get("id") or comp.get("id") or ""),
                start_ts=_ts(comp.get("date") or ev.get("date")),
                home=home_name,
                away=away_name,
                home_abbr=home_abbr,
                away_abbr=away_abbr,
                state=str(stype.get("state") or "").lower() or "unknown",
                detail=stype.get("detail") or stype.get("shortDetail") or stype.get("description"),
                period=_int(status.get("period")),
                clock=status.get("displayClock"),
                home_score=home_score,
                away_score=away_score,
                raw={
                    "situation": comp.get("situation"),
                    "status": status,
                    "name": ev.get("name") or ev.get("shortName"),
                },
            )
        )
    return out


class EspnScoreFeed:
    source = "espn"

    def __init__(
        self, *, timeout: float = 15.0, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._http = httpx.Client(
            timeout=timeout, transport=transport, headers={"User-Agent": "kalshi-sports/0.1"}
        )

    def close(self) -> None:
        self._http.close()

    def fetch(self, league: League, *, date: str | None = None) -> list[GameState]:
        now = time.time()
        params: dict[str, str] = dict(league.espn_params)
        if date:
            params["dates"] = date.replace("-", "")
        resp = self._http.get(ESPN_URL.format(path=league.espn_path), params=params)
        resp.raise_for_status()
        states = parse_espn(resp.json(), league.key, now, self.source)
        log.debug("scores %s: %d games", league.key, len(states))
        return states


def today_eastern(now: float | None = None) -> str:
    """ESPN's scoreboard day rolls over on US Eastern time."""
    from zoneinfo import ZoneInfo

    dt = datetime.fromtimestamp(now if now is not None else time.time(), tz=UTC)
    return dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()
