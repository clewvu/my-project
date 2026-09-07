from sports_fixtures import ESPN_PAYLOAD, ODDS_PAYLOAD

from kalshi_bot.client import KalshiAPIError
from kalshi_bot.models import Market, Orderbook, Trade
from kalshi_sports import leagues
from kalshi_sports.feeds.odds import parse_odds_api
from kalshi_sports.feeds.scores import parse_espn
from kalshi_sports.recorder import SportsRecorder, cadence_for
from kalshi_sports.storage import SportsDataStore

START = "2026-09-07T23:10:00Z"
START_TS = 1788822600.0


def market(ticker, series, title, event, close="2026-09-08T03:00:00Z", result=None, status="open"):
    return Market.from_dict(
        {
            "ticker": ticker,
            "series_ticker": series,
            "event_ticker": event,
            "title": title,
            "status": status,
            "close_time": close,
            "result": result,
            "yes_bid_dollars": "0.45",
            "yes_ask_dollars": "0.47",
            "no_bid_dollars": "0.53",
            "no_ask_dollars": "0.55",
            "volume": 10,
            "exchange_index": 3,
        }
    )


class FakeClient:
    def __init__(self):
        self.open = {
            "KXMLBGAME": [
                market(
                    "KXMLBGAME-26SEP07NYYBOS-NYY",
                    "KXMLBGAME",
                    "Yankees at Red Sox",
                    "KXMLBGAME-26SEP07NYYBOS",
                ),
                market(
                    "KXMLBGAME-26SEP07NYYBOS-BOS",
                    "KXMLBGAME",
                    "Yankees at Red Sox",
                    "KXMLBGAME-26SEP07NYYBOS",
                ),
            ],
            "KXNFLGAME": [
                market(
                    "KXNFLGAME-26SEP07KCLAC-KC",
                    "KXNFLGAME",
                    "Chiefs at Chargers",
                    "KXNFLGAME-26SEP07KCLAC",
                ),
            ],
        }
        self.events = {
            "KXMLBGAME": [
                {
                    "event_ticker": "KXMLBGAME-26SEP07NYYBOS",
                    "title": "Yankees at Red Sox",
                    "strike_date": START,
                }
            ]
        }
        self.settled = {}
        self.fail_orderbook_for = set()
        self.calls = []

    def get_markets(self, *, series_ticker, status, max_pages):
        self.calls.append(("markets", series_ticker))
        return list(self.open.get(series_ticker, []))

    def get_events(self, *, series_ticker, status, max_pages=5):
        self.calls.append(("events", series_ticker))
        return list(self.events.get(series_ticker, []))

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

    def get_trades(self, ticker, *, min_ts):
        self.calls.append(("trades", ticker, min_ts))
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

    def __init__(self):
        self.fetched = []

    def fetch(self, league, *, date=None):
        self.fetched.append((league.key, date))
        return parse_espn(ESPN_PAYLOAD, league.key, now=1000.0) if league.key == "mlb" else []

    def close(self):
        pass


def make(series=("KXMLBGAME", "KXNFLGAME", "KXNCAAFGAME"), **kw):
    client, store = FakeClient(), SportsDataStore()
    rec = SportsRecorder(
        client, store, series=list(series), league_keys=("mlb", "nfl"), interval=1, **kw
    )
    return client, store, rec


def test_cadence_for():
    assert cadence_for(None, fast=5, slow=900) == 900
    assert cadence_for(-100, fast=5, slow=900) == 5  # in play
    assert cadence_for(20 * 60, fast=5, slow=900) == 5
    assert cadence_for(2 * 3600, fast=5, slow=900) == 60
    assert cadence_for(10 * 3600, fast=5, slow=900) == 300
    assert cadence_for(3 * 86400, fast=5, slow=900) == 900


def test_first_tick_discovers_classifies_and_records():
    client, store, rec = make()
    now = START_TS - 2 * 3600
    res = rec.tick(now)
    assert res.discovered == 3 and res.markets == 3 and res.snapshots == 3 and res.new_trades == 3
    assert not res.errors
    st = store.stats()
    assert st["markets"] == 3 and st["events"] == 2 and st["series"] == 3
    rows = store.latest_rows()
    m = store._conn.execute(
        "SELECT * FROM markets WHERE ticker='KXMLBGAME-26SEP07NYYBOS-NYY'"
    ).fetchone()
    assert (m["league"], m["kind"], m["away"], m["home"], m["side"]) == (
        "mlb",
        "moneyline",
        "NYY",
        "BOS",
        "NYY",
    )
    ev = store._conn.execute(
        "SELECT * FROM events WHERE event_ticker='KXMLBGAME-26SEP07NYYBOS'"
    ).fetchone()
    assert ev["start_ts"] == START_TS
    snap = rows["snapshots"]
    assert snap["yes_bid"] == 0.45 and snap["no_bid"] == 0.53
    # NFL event had no events row, so secs_to_start is unknown there but set for MLB
    mlb_snap = store._conn.execute(
        "SELECT secs_to_start FROM snapshots WHERE ticker LIKE 'KXMLBGAME%' LIMIT 1"
    ).fetchone()
    assert mlb_snap["secs_to_start"] == 2 * 3600
    # the unknown college series was recorded as empty, not as an error
    series = {r["ticker"]: r["open_markets"] for r in store._conn.execute("SELECT * FROM series")}
    assert series["KXNCAAFGAME"] == 0


def test_cadence_skips_far_out_markets_between_ticks():
    client, store, rec = make()
    now = START_TS - 2 * 3600  # 60 s cadence for MLB; NFL has no start -> slow cadence
    rec.tick(now)
    res2 = rec.tick(now + 5)
    assert res2.markets == 0
    res3 = rec.tick(now + 61)
    assert res3.markets == 2  # the two MLB markets; NFL waits for the slow cadence
    # second visit: trades already stored
    assert res3.new_trades == 0


def test_errors_are_recorded_not_raised_and_settlement_is_captured():
    client, store, rec = make()
    client.fail_orderbook_for.add("KXNFLGAME-26SEP07KCLAC-KC")
    now = START_TS - 2 * 3600
    res = rec.tick(now)
    assert len(res.errors) == 1 and "KCLAC" in res.errors[0]
    assert res.snapshots == 3  # snapshot still written from market-level quotes
    # market closes; a later fetch carries the result
    settled = market(
        "KXMLBGAME-26SEP07NYYBOS-NYY",
        "KXMLBGAME",
        "Yankees at Red Sox",
        "KXMLBGAME-26SEP07NYYBOS",
        result="yes",
        status="settled",
    )
    client.settled[settled.ticker] = settled
    client.open["KXMLBGAME"] = []
    later = 1788837000.0 + 700  # after close + grace
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
    assert odds.fetched == ["mlb", "nfl"] and res.odds == 5
    assert [k for k, _ in scores.fetched] == ["mlb", "nfl"] and res.states == 1
    # same state again: no new row; odds not due yet
    res2 = rec.tick(now + 30)
    assert res2.odds == 0 and res2.states == 0
    # mlb is in play (state "in"), so scores poll every 20 s; nfl idle every 300 s
    assert scores.fetched.count(("mlb", scores.fetched[0][1])) == 2
    assert sum(1 for k, _ in scores.fetched if k == "nfl") == 1
    res3 = rec.tick(now + 601)
    assert res3.odds == 5
    # a failing odds feed is an error line, not a crash
    odds.fail = True
    res4 = rec.tick(now + 1300)
    assert res4.odds == 0 and any("odds" in e for e in res4.errors)
    assert store.stats()["odds"] == 10


def test_run_stops_after_max_ticks():
    client, store, rec = make()
    assert rec.run(max_ticks=2, install_signals=False) == 2


def test_league_registry_covers_all_five():
    assert set(leagues.LEAGUES) == {"mlb", "nfl", "nba", "ncaaf", "ncaab"}
    for lg in leagues.LEAGUES.values():
        assert lg.kalshi_series and lg.odds_sport_key and lg.espn_path
