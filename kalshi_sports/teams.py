"""Team-name normalisation, so a Kalshi market, an odds quote and a score feed
can be matched to the same game.

Professional leagues get an explicit abbreviation table (odds feeds use full
names, Kalshi tickers use abbreviations, ESPN uses both). College teams are
too many to table; they are matched on normalised school names.
"""

from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# abbreviation -> (city, nickname). Abbreviations follow the common sportsbook
# and Kalshi usage; ESPN's differ for a few teams and are listed as aliases.
MLB_TEAMS: dict[str, tuple[str, str]] = {
    "ARI": ("Arizona", "Diamondbacks"),
    "ATL": ("Atlanta", "Braves"),
    "BAL": ("Baltimore", "Orioles"),
    "BOS": ("Boston", "Red Sox"),
    "CHC": ("Chicago", "Cubs"),
    "CWS": ("Chicago", "White Sox"),
    "CIN": ("Cincinnati", "Reds"),
    "CLE": ("Cleveland", "Guardians"),
    "COL": ("Colorado", "Rockies"),
    "DET": ("Detroit", "Tigers"),
    "HOU": ("Houston", "Astros"),
    "KC": ("Kansas City", "Royals"),
    "LAA": ("Los Angeles", "Angels"),
    "LAD": ("Los Angeles", "Dodgers"),
    "MIA": ("Miami", "Marlins"),
    "MIL": ("Milwaukee", "Brewers"),
    "MIN": ("Minnesota", "Twins"),
    "NYM": ("New York", "Mets"),
    "NYY": ("New York", "Yankees"),
    "OAK": ("Oakland", "Athletics"),
    "ATH": ("Athletics", "Athletics"),
    "PHI": ("Philadelphia", "Phillies"),
    "PIT": ("Pittsburgh", "Pirates"),
    "SD": ("San Diego", "Padres"),
    "SF": ("San Francisco", "Giants"),
    "SEA": ("Seattle", "Mariners"),
    "STL": ("St. Louis", "Cardinals"),
    "TB": ("Tampa Bay", "Rays"),
    "TEX": ("Texas", "Rangers"),
    "TOR": ("Toronto", "Blue Jays"),
    "WSH": ("Washington", "Nationals"),
}
NFL_TEAMS: dict[str, tuple[str, str]] = {
    "ARI": ("Arizona", "Cardinals"),
    "ATL": ("Atlanta", "Falcons"),
    "BAL": ("Baltimore", "Ravens"),
    "BUF": ("Buffalo", "Bills"),
    "CAR": ("Carolina", "Panthers"),
    "CHI": ("Chicago", "Bears"),
    "CIN": ("Cincinnati", "Bengals"),
    "CLE": ("Cleveland", "Browns"),
    "DAL": ("Dallas", "Cowboys"),
    "DEN": ("Denver", "Broncos"),
    "DET": ("Detroit", "Lions"),
    "GB": ("Green Bay", "Packers"),
    "HOU": ("Houston", "Texans"),
    "IND": ("Indianapolis", "Colts"),
    "JAX": ("Jacksonville", "Jaguars"),
    "KC": ("Kansas City", "Chiefs"),
    "LV": ("Las Vegas", "Raiders"),
    "LAC": ("Los Angeles", "Chargers"),
    "LAR": ("Los Angeles", "Rams"),
    "MIA": ("Miami", "Dolphins"),
    "MIN": ("Minnesota", "Vikings"),
    "NE": ("New England", "Patriots"),
    "NO": ("New Orleans", "Saints"),
    "NYG": ("New York", "Giants"),
    "NYJ": ("New York", "Jets"),
    "PHI": ("Philadelphia", "Eagles"),
    "PIT": ("Pittsburgh", "Steelers"),
    "SF": ("San Francisco", "49ers"),
    "SEA": ("Seattle", "Seahawks"),
    "TB": ("Tampa Bay", "Buccaneers"),
    "TEN": ("Tennessee", "Titans"),
    "WSH": ("Washington", "Commanders"),
}
NBA_TEAMS: dict[str, tuple[str, str]] = {
    "ATL": ("Atlanta", "Hawks"),
    "BOS": ("Boston", "Celtics"),
    "BKN": ("Brooklyn", "Nets"),
    "CHA": ("Charlotte", "Hornets"),
    "CHI": ("Chicago", "Bulls"),
    "CLE": ("Cleveland", "Cavaliers"),
    "DAL": ("Dallas", "Mavericks"),
    "DEN": ("Denver", "Nuggets"),
    "DET": ("Detroit", "Pistons"),
    "GSW": ("Golden State", "Warriors"),
    "HOU": ("Houston", "Rockets"),
    "IND": ("Indiana", "Pacers"),
    "LAC": ("Los Angeles", "Clippers"),
    "LAL": ("Los Angeles", "Lakers"),
    "MEM": ("Memphis", "Grizzlies"),
    "MIA": ("Miami", "Heat"),
    "MIL": ("Milwaukee", "Bucks"),
    "MIN": ("Minnesota", "Timberwolves"),
    "NOP": ("New Orleans", "Pelicans"),
    "NYK": ("New York", "Knicks"),
    "OKC": ("Oklahoma City", "Thunder"),
    "ORL": ("Orlando", "Magic"),
    "PHI": ("Philadelphia", "76ers"),
    "PHX": ("Phoenix", "Suns"),
    "POR": ("Portland", "Trail Blazers"),
    "SAC": ("Sacramento", "Kings"),
    "SAS": ("San Antonio", "Spurs"),
    "TOR": ("Toronto", "Raptors"),
    "UTA": ("Utah", "Jazz"),
    "WAS": ("Washington", "Wizards"),
}
# Alternate abbreviations seen in other feeds -> canonical.
ALIASES: dict[str, dict[str, str]] = {
    "mlb": {"WAS": "WSH", "CHW": "CWS", "SDP": "SD", "SFG": "SF", "TBR": "TB", "KCR": "KC"},
    "nfl": {
        "WAS": "WSH",
        "GNB": "GB",
        "KAN": "KC",
        "NWE": "NE",
        "NOR": "NO",
        "SFO": "SF",
        "TAM": "TB",
        "LVR": "LV",
        "JAC": "JAX",
    },
    "nba": {
        "GS": "GSW",
        "NO": "NOP",
        "NY": "NYK",
        "SA": "SAS",
        "PHO": "PHX",
        "BRK": "BKN",
        "UTAH": "UTA",
        "WSH": "WAS",
    },
}
TABLES = {"mlb": MLB_TEAMS, "nfl": NFL_TEAMS, "nba": NBA_TEAMS}

# Kalshi disambiguates shared cities with a trailing initial: "Los Angeles D", "New York G".
KALSHI_CITY_NAMES: dict[str, dict[str, str]] = {
    "mlb": {
        "losangelesd": "LAD",
        "losangelesa": "LAA",
        "newyorky": "NYY",
        "newyorkm": "NYM",
        "chicagoc": "CHC",
        "chicagow": "CWS",
        "as": "ATH",
        "athletics": "ATH",
    },
    "nfl": {"newyorkg": "NYG", "newyorkj": "NYJ", "losangelesr": "LAR", "losangelesc": "LAC"},
    "nba": {"losangelesl": "LAL", "losangelesc": "LAC"},
}


def normalise(name: str) -> str:
    """Lower-case alphanumerics only: 'St. Louis Cardinals' -> 'stlouiscardinals'."""
    return _NON_ALNUM.sub("", name.lower().replace("&", "and"))


def canonical_abbr(league: str, abbr: str) -> str:
    a = abbr.upper()
    return ALIASES.get(league, {}).get(a, a)


def full_name(league: str, abbr: str) -> str | None:
    table = TABLES.get(league)
    if table is None:
        return None
    entry = table.get(canonical_abbr(league, abbr))
    if entry is None:
        return None
    city, nick = entry
    return nick if city == nick else f"{city} {nick}"


def abbr_for_name(league: str, name: str) -> str | None:
    """Abbreviation for a full or partial team name ('Boston Red Sox', 'Red Sox', 'Boston')."""
    table = TABLES.get(league)
    if table is None:
        return None
    target = normalise(name)
    if not target:
        return None
    if target.upper() in table:
        return target.upper()
    if target in KALSHI_CITY_NAMES.get(league, {}):
        return KALSHI_CITY_NAMES[league][target]
    # Full name first, then nickname, then city (city alone is ambiguous for NY/LA/CHI).
    for abbr, (city, nick) in table.items():
        if normalise(f"{city} {nick}") == target or normalise(nick) == target:
            return abbr
    candidates = [abbr for abbr, (city, _) in table.items() if normalise(city) == target]
    return candidates[0] if len(candidates) == 1 else None


def same_team(league: str, a: str, b: str) -> bool:
    """True if two team strings (any of abbreviation, full name, nickname) name one team."""
    if league in TABLES:
        aa = abbr_for_name(league, a) or canonical_abbr(league, a)
        bb = abbr_for_name(league, b) or canonical_abbr(league, b)
        return aa == bb
    # College: normalised containment either way, with a minimum length so
    # 'Miami' does not match 'Miami (OH)' by accident only when both are short.
    na, nb = normalise(a), normalise(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    shorter, longer = sorted((na, nb), key=len)
    return len(shorter) >= 5 and shorter in longer
