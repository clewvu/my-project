"""Sportsbook odds feed.

``OddsFeed`` is the interface the recorder and (later) the pricing engine use;
``TheOddsApiFeed`` is the first adapter. Swapping the source means writing
another adapter, nothing else changes (design note 8, "make the reference
price pluggable").

The Odds API (the-odds-api.com), v4:
    GET /v4/sports/{sport_key}/odds?apiKey=&regions=us&markets=h2h,spreads,totals
        &oddsFormat=decimal
One request per league per poll, regardless of the number of games. The free
tier allows 500 requests a month, so the default cadence here is conservative;
the remaining quota is read from the ``x-requests-remaining`` header and
logged. Nothing here is verified against the live API yet (``kalshi-sports
odds-test``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import httpx

from ..leagues import LEAGUES, League

log = logging.getLogger(__name__)

ODDS_API_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds"
DEFAULT_MARKETS = ("h2h", "spreads", "totals")
DEFAULT_REGIONS = ("us", "us2", "eu")  # eu brings in Pinnacle where the feed carries it
SHARP_BOOKS = ("pinnacle", "betonlineag", "bookmaker", "circasports")


@dataclass(frozen=True)
class OddsQuote:
    ts: float  # when we saw it
    source: str
    league: str
    game_id: str
    commence_ts: float | None
    home: str
    away: str
    book: str
    market: str  # h2h, spreads, totals
    outcome: str  # team name, or Over / Under
    point: float | None  # spread or total line
    price_decimal: float
    book_ts: float | None  # book's last update

    def row(self) -> tuple[Any, ...]:
        return (
            self.ts,
            self.source,
            self.league,
            self.game_id,
            self.commence_ts,
            self.home,
            self.away,
            self.book,
            self.market,
            self.outcome,
            self.point,
            self.price_decimal,
            self.book_ts,
        )


class OddsFeed(Protocol):
    source: str

    def fetch(self, league: League) -> list[OddsQuote]: ...

    def close(self) -> None: ...


def _ts(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_odds_api(
    payload: list[dict[str, Any]], league: str, now: float, source: str = "the-odds-api"
) -> list[OddsQuote]:
    """Flatten The Odds API's nested games -> bookmakers -> markets -> outcomes."""
    out: list[OddsQuote] = []
    for game in payload:
        gid = str(game.get("id") or "")
        home = str(game.get("home_team") or "")
        away = str(game.get("away_team") or "")
        commence = _ts(game.get("commence_time"))
        for bm in game.get("bookmakers", []) or []:
            book = str(bm.get("key") or bm.get("title") or "")
            book_ts = _ts(bm.get("last_update"))
            for mk in bm.get("markets", []) or []:
                mkey = str(mk.get("key") or "")
                for oc in mk.get("outcomes", []) or []:
                    try:
                        price = float(oc["price"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if price <= 1.0:
                        continue
                    point = oc.get("point")
                    out.append(
                        OddsQuote(
                            ts=now,
                            source=source,
                            league=league,
                            game_id=gid,
                            commence_ts=commence,
                            home=home,
                            away=away,
                            book=book,
                            market=mkey,
                            outcome=str(oc.get("name") or ""),
                            point=float(point) if point is not None else None,
                            price_decimal=price,
                            book_ts=book_ts or _ts(mk.get("last_update")),
                        )
                    )
    return out


class TheOddsApiFeed:
    source = "the-odds-api"

    def __init__(
        self,
        api_key: str,
        *,
        regions: tuple[str, ...] = DEFAULT_REGIONS,
        markets: tuple[str, ...] = DEFAULT_MARKETS,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("ODDS_API_KEY is empty")
        self.api_key = api_key
        self.regions = regions
        self.markets = markets
        self.remaining: int | None = None
        self.used: int | None = None
        self._http = httpx.Client(
            timeout=timeout, transport=transport, headers={"User-Agent": "kalshi-sports/0.1"}
        )

    def close(self) -> None:
        self._http.close()

    def fetch(self, league: League) -> list[OddsQuote]:
        now = time.time()
        resp = self._http.get(
            ODDS_API_URL.format(sport=league.odds_sport_key),
            params={
                "apiKey": self.api_key,
                "regions": ",".join(self.regions),
                "markets": ",".join(self.markets),
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
        )
        self._read_quota(resp)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, list):
            raise ValueError(f"unexpected odds payload: {str(payload)[:200]}")
        quotes = parse_odds_api(payload, league.key, now, self.source)
        log.info(
            "odds %s: %d games, %d quotes, quota remaining=%s",
            league.key,
            len(payload),
            len(quotes),
            self.remaining,
        )
        return quotes

    def _read_quota(self, resp: httpx.Response) -> None:
        for attr, header in (("remaining", "x-requests-remaining"), ("used", "x-requests-used")):
            value = resp.headers.get(header)
            if value is not None:
                try:
                    setattr(self, attr, int(float(value)))
                except ValueError:
                    pass


def sport_keys() -> dict[str, str]:
    return {k: lg.odds_sport_key for k, lg in LEAGUES.items()}
