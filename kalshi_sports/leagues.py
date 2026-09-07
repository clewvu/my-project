"""League registry: which Kalshi series, odds-feed sport keys and score feeds go together.

The Kalshi series tickers listed here are *candidates* collected from the
naming pattern of the crypto series (``KX<UNDERLYING><KIND>``). They are
verified by ``kalshi-sports discover`` against production; the recorder polls
whatever is configured and logs series that return no markets.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Kinds of market Kalshi lists on a game, in the vocabulary used across this package.
MONEYLINE = "moneyline"
SPREAD = "spread"
TOTAL = "total"
SERIES_WINNER = "series"
FUTURE = "future"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class League:
    key: str  # short key used in CLI flags and the database: mlb, nfl, nba, ncaaf, ncaab
    name: str
    sport: str  # baseball, football, basketball
    kalshi_series: tuple[str, ...]  # candidate series tickers, verified by `discover`
    odds_sport_key: str  # The Odds API sport key
    espn_path: str  # site.api.espn.com/apis/site/v2/sports/<espn_path>/scoreboard
    espn_params: dict[str, str] = field(default_factory=dict)  # extra query params
    keywords: tuple[str, ...] = ()  # words that identify the league in a series title

    def series_kind(self, series_ticker: str) -> str:
        """Best-effort market kind from the series ticker suffix."""
        s = series_ticker.upper()
        if s.endswith("GAME") or s.endswith("ML"):
            return MONEYLINE
        if "SPREAD" in s or "RUNLINE" in s or s.endswith("LINE"):
            return SPREAD
        if "TOTAL" in s or "OU" in s.split("-")[0][-2:]:
            return TOTAL
        if "SERIES" in s:
            return SERIES_WINNER
        if "CHAMP" in s or "WINNER" in s or "MVP" in s or "PLAYOFF" in s:
            return FUTURE
        return UNKNOWN


MLB = League(
    key="mlb",
    name="MLB",
    sport="baseball",
    kalshi_series=("KXMLBGAME", "KXMLBSPREAD", "KXMLBTOTAL", "KXMLBSERIES"),
    odds_sport_key="baseball_mlb",
    espn_path="baseball/mlb",
    keywords=("MLB", "baseball"),
)
NFL = League(
    key="nfl",
    name="NFL",
    sport="football",
    kalshi_series=("KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL"),
    odds_sport_key="americanfootball_nfl",
    espn_path="football/nfl",
    keywords=("NFL", "football"),
)
NBA = League(
    key="nba",
    name="NBA",
    sport="basketball",
    kalshi_series=("KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL", "KXNBASERIES"),
    odds_sport_key="basketball_nba",
    espn_path="basketball/nba",
    keywords=("NBA", "basketball"),
)
NCAAF = League(
    key="ncaaf",
    name="NCAA football",
    sport="football",
    kalshi_series=("KXNCAAFGAME", "KXNCAAFSPREAD", "KXNCAAFTOTAL", "KXCFBGAME"),
    odds_sport_key="americanfootball_ncaaf",
    espn_path="football/college-football",
    espn_params={"groups": "80", "limit": "300"},  # 80 = all FBS
    keywords=("NCAA", "college football", "CFB"),
)
NCAAB = League(
    key="ncaab",
    name="NCAA men's basketball",
    sport="basketball",
    kalshi_series=("KXNCAABGAME", "KXNCAABSPREAD", "KXNCAABTOTAL", "KXCBBGAME"),
    odds_sport_key="basketball_ncaab",
    espn_path="basketball/mens-college-basketball",
    espn_params={"groups": "50", "limit": "400"},  # 50 = all Division I
    keywords=("NCAA", "college basketball", "CBB"),
)

LEAGUES: dict[str, League] = {lg.key: lg for lg in (MLB, NFL, NBA, NCAAF, NCAAB)}
DEFAULT_LEAGUES = ("mlb", "nfl", "nba", "ncaaf", "ncaab")


def league_for_series(series_ticker: str) -> League | None:
    """Which league a Kalshi series belongs to, from the registry or the ticker prefix."""
    s = series_ticker.upper()
    for lg in LEAGUES.values():
        if s in lg.kalshi_series:
            return lg
    # Order matters: NCAAF/NCAAB before NFL/NBA so KXNCAAB does not match KXNBA-like prefixes.
    for key, prefixes in (
        ("ncaaf", ("KXNCAAF", "KXCFB")),
        ("ncaab", ("KXNCAAB", "KXCBB", "KXNCAAM")),
        ("mlb", ("KXMLB",)),
        ("nfl", ("KXNFL",)),
        ("nba", ("KXNBA",)),
    ):
        if any(s.startswith(p) for p in prefixes):
            return LEAGUES[key]
    return None


def series_for_leagues(keys: list[str] | tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for key in keys:
        lg = LEAGUES.get(key.lower())
        if lg is None:
            raise KeyError(f"unknown league {key!r}; choose from {', '.join(LEAGUES)}")
        out.extend(lg.kalshi_series)
    return out
