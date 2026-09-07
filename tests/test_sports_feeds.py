import json

import httpx
import pytest
from sports_fixtures import ESPN_PAYLOAD, ODDS_PAYLOAD

from kalshi_sports import leagues
from kalshi_sports.feeds import devig
from kalshi_sports.feeds.odds import TheOddsApiFeed, parse_odds_api
from kalshi_sports.feeds.scores import EspnScoreFeed, parse_espn, today_eastern


def test_odds_conversions():
    assert devig.american_to_decimal(-150) == pytest.approx(1.6667, abs=1e-4)
    assert devig.american_to_decimal(130) == pytest.approx(2.3)
    assert devig.decimal_to_american(2.3) == pytest.approx(130)
    assert devig.decimal_to_american(1.6667) == pytest.approx(-150, abs=0.1)
    assert devig.implied(2.0) == 0.5
    with pytest.raises(ValueError):
        devig.american_to_decimal(0)


def test_devig_methods_sum_to_one_and_order_longshots():
    decs = [1.30, 4.00]  # heavy favourite, longshot; 1.9% overround
    assert devig.overround(decs) == pytest.approx(1 / 1.3 + 1 / 4.0 - 1)
    for name in devig.METHODS:
        ps = devig.devig(decs, name)
        assert sum(ps) == pytest.approx(1.0, abs=1e-6)
        assert ps[0] > ps[1]
    mult, power, shin = (devig.devig(decs, m) for m in ("multiplicative", "power", "shin"))
    # power and shin shade the longshot harder than multiplicative
    assert power[1] < mult[1] and shin[1] < mult[1]
    with pytest.raises(ValueError):
        devig.devig(decs, "nope")


def test_devig_even_market_is_symmetric():
    ps = devig.devig([1.91, 1.91], "shin")
    assert ps == pytest.approx([0.5, 0.5])
    # an underround (arbitrage) market falls back to plain scaling instead of diverging
    for name in devig.METHODS:
        assert sum(devig.devig([2.10, 2.10], name)) == pytest.approx(1.0)


def test_parse_odds_api_flattens_and_skips_bad_prices():
    quotes = parse_odds_api(ODDS_PAYLOAD, "mlb", now=1000.0)
    assert len(quotes) == 5  # 2 h2h + 2 totals from pinnacle, 1 valid from draftkings
    q = quotes[0]
    assert q.league == "mlb" and q.game_id == "abc123" and q.book == "pinnacle"
    assert q.home == "Boston Red Sox" and q.away == "New York Yankees"
    assert q.commence_ts is not None and q.book_ts is not None
    totals = [q for q in quotes if q.market == "totals"]
    assert {t.point for t in totals} == {8.5}
    assert len(q.row()) == 13


def test_odds_feed_reads_quota_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["apiKey"] == "k"
        assert "h2h" in request.url.params["markets"]
        return httpx.Response(
            200,
            json=ODDS_PAYLOAD,
            headers={"x-requests-remaining": "480", "x-requests-used": "20"},
        )

    feed = TheOddsApiFeed("k", transport=httpx.MockTransport(handler))
    quotes = feed.fetch(leagues.MLB)
    assert len(quotes) == 5 and feed.remaining == 480 and feed.used == 20
    feed.close()
    with pytest.raises(ValueError):
        TheOddsApiFeed("")


def test_parse_espn():
    states = parse_espn(ESPN_PAYLOAD, "mlb", now=1000.0)
    assert len(states) == 1
    s = states[0]
    assert (s.home, s.away, s.home_abbr, s.away_abbr) == (
        "Boston Red Sox",
        "New York Yankees",
        "BOS",
        "NYY",
    )
    assert (s.home_score, s.away_score, s.period, s.state, s.detail) == (
        3,
        2,
        7,
        "in",
        "Bottom 7th",
    )
    row = s.row()
    assert len(row) == 14
    assert json.loads(row[-1])["situation"]["outs"] == 1


def test_espn_feed_passes_group_params_and_date():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=ESPN_PAYLOAD)

    feed = EspnScoreFeed(transport=httpx.MockTransport(handler))
    states = feed.fetch(leagues.NCAAF, date="2026-09-07")
    assert len(states) == 1
    assert "college-football" in seen["url"] and "groups=80" in seen["url"]
    assert "dates=20260907" in seen["url"]
    feed.close()


def test_today_eastern_rolls_at_eastern_midnight():
    # 2026-09-08 03:00 UTC is still 2026-09-07 in New York (EDT, UTC-4)
    from datetime import UTC, datetime

    ts = datetime(2026, 9, 8, 3, 0, tzinfo=UTC).timestamp()
    assert today_eastern(ts) == "2026-09-07"
    assert today_eastern(ts + 3600) == "2026-09-08"
