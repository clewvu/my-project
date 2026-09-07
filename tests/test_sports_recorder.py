from sports_fixtures import CLOSE_TS, ESPN_PAYLOAD, ODDS_PAYLOAD, START_TS, market, rules

from kalshi_bot.client import KalshiAPIError
from kalshi_bot.models import Orderbook, Trade
from kalshi_sports import leagues
from kalshi_sports.feeds.odds import parse_odds_api
from kalshi_sports.feeds.scores import parse_espn
from kalshi_sports.recorder import SportsRecorder, cadence_for
from kalshi_sports.storage import SportsDataStore

MLB_EVENT = "KXMLBGAME-26SEP071910NYYBOS"
NFL_EVENT = "KXNFLGAME-26SEP07KCLAC"


class FakeClient:
    def __init__(self):
        self.open = {
            "KXMLBGAME": [
                market(
                    f"{MLB_EVENT}-NYY",
                    "KXMLBGAME",
                    "New York Y wins",
                    MLB_EVENT,
                    rules_text=rules("New York Y", "Boston"),
                    sub="New York Y",
                ),
                market(
                    f"{MLB_EVENT}-BOS",
                    "KXMLBGAME",
                    "Boston wins",
                    MLB_EVENT,
                    rules_text=rules("New York Y", "Boston"),
                    sub="Boston",
                ),
            ],
            # NFL: date only, so the start is approximate (13:00 ET) until ESPN refines it
            "KXNFLGAME": [
                market(
                    f"{NFL_EVENT}-KC",
                    "KXNFLGAME",
                    "Kansas City wins",
                    NFL_EVENT,
                    rules_text=rules("Kansas City", "Los Angeles C", "Pro Football", "Sep 7, 2026"),
                    sub="Kansas City",
                    close="2026-09-10T23:00:00Z",
                ),
            ],
        }
        self.settled = {}
        self.fail_orderbook_for = set()
        self.calls = []

    def get_markets(self, *, series_ticker, status, limit=100, max_pages=5):
        self.calls.append(("markets", series_ticker, limit))
        return list(self.open.get(series_ticker, []))

    def get_market(self, ticker):
        self.calls.append(("market", ticker))
        if ticker in self.settled:
            return self.settled[ticker]
        for ms in self.open.values():
            for m in ms:
                if m.ticker == ticker:
                    return m
        raise KalshiAPIError(404, "GET", f"/markets/{ticker}", "nope")

    def get_orderbook(self, ticker, depth):
        self.calls.append(("book", ticker))
        if ticker in self.fail_orderbook_for:
            raise KalshiAPIError(500, "GET", f"/markets/{ticker}/orderbook", "boom")
        return Orderbook.from_dict(
            ticker,
            {"orderbook_fp": {"yes_dollars": [["0.45", "10"]], "no_dollars": [["0.53", "7"]]}},
        )

    def get_trades(self, ticker, *, min_ts, max_pages=5):
        self.calls.append(("trades", ticker, min_ts, max_pages))
        return [
            Trade.from_dict(
                {
                    "trade_id": f"{ticker}-t1",
                    "ticker": ticker,
                    "created_time": "2026-09-07T20:00:00Z",
                    "yes_price_dollars": "0.46",
                    "no_price_dollars": "0.54",
                    "count_fp": "2.00",
                    "taker_side": "yes",
                }
            )
        ]


class FakeOdds:
    source = "fake"

    def __init__(self):
        self.fetched = []
        self.fail = False

    def fetch(self, league):
        self.fetched.append(league.key)
        if self.fail:
            raise RuntimeError("quota")
        return parse_odds_api(ODDS_PAYLOAD, league.key, now=1000.0) if league.key == "mlb" else []

    def close(self):
        pass


class FakeScores:
    source = "fake"

    def __init__(self, payloads=None):
        self.fetched = []
        self.payloads = payloads or {"mlb": ESPN_PAYLOAD}

    def fetch(self, league, *, date=None):
        self.fetched.append((league.key, date))
        payload = self.payloads.get(league.key)
        return parse_espn(payload, league.key, now=1000.0) if payload else []

    def close(self):
        pass


def make(series=("KXMLBGAME", "KXNFLGAME", "KXNCAAFGAME"), **kw):
    client, store = FakeClient(), SportsDataStore()
    rec = SportsRecorder(
        client, store, series=list(series), league_keys=("mlb", "nfl"), interval=1, **kw
    )
    return client, store, rec


def test_cadence_for():
    assert cadence_for(None, fast=5) is None
    assert cadence_for(3 * 86400, fast=5) is None  # light snapshots only
    assert cadence_for(10 * 3600, fast=5) == 300
    assert cadence_for(2 * 3600, fast=5) == 60
    assert cadence_for(20 * 60, fast=5) == 5
    assert cadence_for(-100, fast=5) == 5  # in play


def test_first_tick_discovers_light_snapshots_and_polls_near_games():
    client, store, rec = make()
    now = START_TS - 2 * 3600  # MLB game in 2 h; NFL approx start 13:00 ET is 6 h earlier
    res = rec.tick(now)
    assert res.discovered == 3 and res.light == 3
    # all three are inside the 24 h book window, so all get a book snapshot
    assert res.markets == 3 and res.snapshots == 3 and res.new_trades == 3 and not res.errors
    assert ("markets", "KXMLBGAME", 1000) in client.calls
    # first trade fetch backfills deep; later ones are shallow and bounded by min_ts
    assert ("trades", f"{MLB_EVENT}-NYY", None, 20) in client.calls
    st = store.stats()
    assert st["markets"] == 3 and st["events"] == 2 and st["series"] == 3
    assert st["snapshots"] == 6 and st["book_snapshots"] == 3
    assert st["events_exact_start"] == 1  # MLB from the ticker time; NFL approximate
    m = store._conn.execute(
        "SELECT * FROM markets WHERE ticker=?", (f"{MLB_EVENT}-NYY",)
    ).fetchone()
    assert (m["league"], m["kind"], m["away"], m["home"], m["away_abbr"], m["side_team"]) == (
        "mlb",
        "moneyline",
        "New York Y",
        "Boston",
        "NYY",
        "NYY",
    )
    assert store.event_start(MLB_EVENT) == (START_TS, True)
    nfl_start, exact = store.event_start(NFL_EVENT)
    assert nfl_start is not None and not exact
    # the unknown college series was recorded as empty, not as an error
    series = {r["ticker"]: r["open_markets"] for r in store._conn.execute("SELECT * FROM series")}
    assert series["KXNCAAFGAME"] == 0


def test_far_out_markets_get_light_snapshots_only():
    client, store, rec = make()
    now = START_TS - 3 * 86400
    res = rec.tick(now)
    assert res.light == 3 and res.markets == 0 and res.snapshots == 0
    assert not any(c[0] == "book" for c in client.calls)
    assert store.stats()["snapshots"] == 3


def test_cadence_skips_between_ticks_and_dedups_trades():
    client, store, rec = make()
    now = START_TS - 2 * 3600  # 60 s cadence for MLB
    rec.tick(now)
    books_before = sum(1 for c in client.calls if c[0] == "book" and "KXMLB" in c[1])
    res2 = rec.tick(now + 5)
    # the NFL market's approximate 13:00 ET start is already past, so it polls at the fast
    # cadence; the two MLB markets (2 h out, 60 s cadence) must not
    assert res2.markets == 1
    assert sum(1 for c in client.calls if c[0] == "book" and "KXMLB" in c[1]) == books_before
    res3 = rec.tick(now + 61)
    assert res3.markets == 3 and res3.new_trades == 0
    later = [c for c in client.calls if c[0] == "trades" and c[1] == f"{MLB_EVENT}-NYY"][-1]
    assert later[2] is not None and later[3] == 5


def test_errors_are_recorded_not_raised_and_settlement_is_captured():
    client, store, rec = make()
    client.fail_orderbook_for.add(f"{NFL_EVENT}-KC")
    now = START_TS - 2 * 3600
    res = rec.tick(now)
    assert len(res.errors) == 1 and "KCLAC" in res.errors[0]
    assert res.snapshots == 3  # snapshot still written from market-level quotes
    settled = market(
        f"{MLB_EVENT}-NYY",
        "KXMLBGAME",
        "New York Y wins",
        MLB_EVENT,
        rules_text=rules("New York Y", "Boston"),
        result="yes",
        status="settled",
    )
    client.settled[settled.ticker] = settled
    client.open["KXMLBGAME"] = []
    later = CLOSE_TS + 700  # after the Sep 10 close + grace
    rec._last_settle = 0.0
    res2 = rec.tick(later)
    assert res2.settled == 1
    row = store._conn.execute(
        "SELECT result, settled_ts FROM markets WHERE ticker=?", (settled.ticker,)
    ).fetchone()
    assert row["result"] == "yes" and row["settled_ts"] == later
    assert settled.ticker not in rec.tracked


def test_odds_and_scores_feeds_are_polled_per_league_and_deduplicated():
    odds, scores = FakeOdds(), FakeScores()
    client, store, rec = make(
        odds=odds,
        odds_interval=600,
        scores=scores,
        scores_interval_live=20,
        scores_interval_idle=300,
    )
    now = START_TS - 2 * 3600
    res = rec.tick(now)
    # both leagues have open Kalshi markets, so both are polled
    assert odds.fetched == ["mlb", "nfl"] and res.odds == 5
    assert [k for k, _ in scores.fetched] == ["mlb", "nfl"] and res.states == 1
    res2 = rec.tick(now + 30)
    assert res2.odds == 0 and res2.states == 0
    # mlb is in play (state "in"), so scores poll every 20 s; nfl idle every 300 s
    assert sum(1 for k, _ in scores.fetched if k == "mlb") == 2
    assert sum(1 for k, _ in scores.fetched if k == "nfl") == 1
    res3 = rec.tick(now + 601)
    assert res3.odds == 5
    odds.fail = True
    res4 = rec.tick(now + 1300)
    assert res4.odds == 0 and any("odds" in e for e in res4.errors)
    assert store.stats()["odds"] == 10


def test_espn_schedule_refines_a_date_only_start():
    nfl_payload = {
        "events": [
            {
                "id": "9001",
                "date": "2026-09-08T00:20Z",  # Sep 7, 8:20 PM EDT
                "competitions": [
                    {
                        "date": "2026-09-08T00:20Z",
                        "competitors": [
                            {
                                "homeAway": "home",
                                "score": "0",
                                "team": {
                                    "displayName": "Los Angeles Chargers",
                                    "abbreviation": "LAC",
                                },
                            },
                            {
                                "homeAway": "away",
                                "score": "0",
                                "team": {"displayName": "Kansas City Chiefs", "abbreviation": "KC"},
                            },
                        ],
                        "status": {"type": {"state": "pre", "detail": "Scheduled"}},
                    }
                ],
            }
        ]
    }
    scores = FakeScores({"nfl": nfl_payload})
    client, store, rec = make(scores=scores)
    now = START_TS - 2 * 3600
    res = rec.tick(now)
    assert res.starts_refined == 1
    assert store.event_start(NFL_EVENT) == (
        1788827.0 * 1000 + 600 - 600,
        True,
    ) or store.event_start(NFL_EVENT) == (1788826800.0, True)
    tr = rec.tracked[f"{NFL_EVENT}-KC"]
    assert tr.start_exact and tr.start_ts == 1788826800.0
    # a later discovery must not overwrite the exact start with the approximation
    rec._last_discover = 0.0
    rec.tick(now + 400)
    assert store.event_start(NFL_EVENT) == (1788826800.0, True)


def test_odds_are_not_polled_for_leagues_without_markets():
    odds = FakeOdds()
    client, store, rec = make(series=("KXMLBGAME",), odds=odds)
    rec.tick(START_TS - 3600)
    assert odds.fetched == ["mlb"]


def test_run_stops_after_max_ticks():
    client, store, rec = make()
    assert rec.run(max_ticks=2, install_signals=False) == 2


def test_league_registry_covers_all_five():
    assert set(leagues.LEAGUES) == {"mlb", "nfl", "nba", "ncaaf", "ncaab"}
    for lg in leagues.LEAGUES.values():
        assert lg.kalshi_series and lg.odds_sport_key and lg.espn_path
