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


def seed(store: SportsDataStore, *, side="NYY", league="mlb", odds=True):
    m = Market.from_dict(
        {
            "ticker": f"KXMLBGAME-26SEP07NYYBOS-{side}",
            "series_ticker": "KXMLBGAME",
            "event_ticker": "KXMLBGAME-26SEP07NYYBOS",
            "title": "Yankees at Red Sox",
            "status": "open",
            "close_time": "2026-09-08T03:00:00Z",
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.42",
            "no_bid_dollars": "0.58",
            "no_ask_dollars": "0.60",
            "volume": 5,
        }
    )
    gm = classify(m, {"title": "Yankees at Red Sox", "strike_date": "2026-09-07T23:10:00Z"})
    store.upsert_event(gm, NOW, None)
    store.upsert_market(m, gm, NOW)
    store.insert_snapshot(NOW, m, None, gm.start_ts)
    if odds:
        quotes = parse_odds_api(ODDS_PAYLOAD, league, now=NOW - 60)
        store.insert_odds([q.row() for q in quotes])
    return m
