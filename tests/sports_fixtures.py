"""Shared payloads and seed helpers for the sports tests (imported as ``sports_fixtures``)."""

from kalshi_bot.models import Market
from kalshi_sports.catalog import classify
from kalshi_sports.feeds.odds import parse_odds_api
from kalshi_sports.storage import SportsDataStore

ODDS_PAYLOAD = [
    {
        "id": "abc123",
        "sport_key": "baseball_mlb",
        "commence_time": "2026-09-07T23:10:00Z",
        "home_team": "Boston Red Sox",
        "away_team": "New York Yankees",
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": "2026-09-07T20:00:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Boston Red Sox", "price": 2.10},
                            {"name": "New York Yankees", "price": 1.80},
                        ],
                    },
                    {
                        "key": "totals",
                        "outcomes": [
                            {"name": "Over", "price": 1.95, "point": 8.5},
                            {"name": "Under", "price": 1.95, "point": 8.5},
                        ],
                    },
                ],
            },
            {
                "key": "draftkings",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Boston Red Sox", "price": 2.05},
                            {"name": "New York Yankees", "price": "bad"},
                        ],
                    }
                ],
            },
        ],
    }
]

ESPN_PAYLOAD = {
    "events": [
        {
            "id": "401",
            "date": "2026-09-07T23:10Z",
            "name": "New York Yankees at Boston Red Sox",
            "competitions": [
                {
                    "id": "401",
                    "date": "2026-09-07T23:10Z",
                    "competitors": [
                        {
                            "homeAway": "home",
                            "score": "3",
                            "team": {"displayName": "Boston Red Sox", "abbreviation": "BOS"},
                        },
                        {
                            "homeAway": "away",
                            "score": "2",
                            "team": {"displayName": "New York Yankees", "abbreviation": "NYY"},
                        },
                    ],
                    "status": {
                        "period": 7,
                        "displayClock": "0:00",
                        "type": {"state": "in", "detail": "Bottom 7th"},
                    },
                    "situation": {"outs": 1, "onFirst": True},
                }
            ],
        }
    ]
}

NOW = 1788815000.0  # 2026-09-07 21:03 UTC
START = "2026-09-07T23:10:00Z"
START_TS = 1788822600.0  # 2026-09-07 19:10 EDT
CLOSE_TS = START_TS + 3 * 86400  # Kalshi closes game markets three days after the game


def rules(away, home, sport="professional baseball", when="Sep 7, 2026 at 7:10 PM EDT"):
    return (
        f"If {home} wins the {away} vs {home} {sport} game originally scheduled for {when}, "
        "then the market resolves to Yes."
    )


def market(
    ticker,
    series,
    title,
    event,
    *,
    rules_text=None,
    sub=None,
    strike=None,
    close="2026-09-10T23:10:00Z",
    result=None,
    status="open",
    bid="0.45",
    ask="0.47",
):
    d = {
        "ticker": ticker,
        "series_ticker": series,
        "event_ticker": event,
        "title": title,
        "status": status,
        "close_time": close,
        "result": result,
        "yes_bid_dollars": bid,
        "yes_ask_dollars": ask,
        "no_bid_dollars": f"{1 - float(ask):.2f}",
        "no_ask_dollars": f"{1 - float(bid):.2f}",
        "volume": 10,
        "exchange_index": 3,
    }
    if rules_text:
        d["rules_primary"] = rules_text
    if sub:
        d["yes_sub_title"] = sub
    if strike is not None:
        d["floor_strike"] = strike
    return Market.from_dict(d)


def seed(store: SportsDataStore, *, side="NYY", league="mlb", odds=True):
    m = market(
        f"KXMLBGAME-26SEP071910NYYBOS-{side}",
        "KXMLBGAME",
        f"{'New York Y' if side == 'NYY' else 'Boston'} wins",
        "KXMLBGAME-26SEP071910NYYBOS",
        rules_text=rules("New York Y", "Boston"),
        sub="New York Y" if side == "NYY" else "Boston",
        bid="0.40",
        ask="0.42",
    )
    gm = classify(m)
    store.upsert_event(gm, NOW, None)
    store.upsert_market(m, gm, NOW)
    store.insert_snapshot(NOW, m, None, gm.start_ts)
    if odds:
        quotes = parse_odds_api(ODDS_PAYLOAD, league, now=NOW - 60)
        store.insert_odds([q.row() for q in quotes])
    return m
