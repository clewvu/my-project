from kalshi_bot.models import Market
from kalshi_sports import leagues, teams
from kalshi_sports.catalog import classify, parse_ticker_tail, split_teams, teams_from_title


def mk(ticker, series, title="", **extra):
    d = {"ticker": ticker, "series_ticker": series, "title": title, "status": "open"}
    d.update(extra)
    return Market.from_dict(d)


def test_league_for_series_prefix_and_registry():
    assert leagues.league_for_series("KXMLBGAME").key == "mlb"
    assert leagues.league_for_series("KXNCAAFGAME").key == "ncaaf"
    assert leagues.league_for_series("KXNCAABGAME").key == "ncaab"
    assert leagues.league_for_series("KXNBATOTAL").key == "nba"
    assert leagues.league_for_series("KXNFLSOMETHING").key == "nfl"
    assert leagues.league_for_series("KXBTC15M") is None


def test_series_kind():
    lg = leagues.MLB
    assert lg.series_kind("KXMLBGAME") == leagues.MONEYLINE
    assert lg.series_kind("KXMLBSPREAD") == leagues.SPREAD
    assert lg.series_kind("KXMLBTOTAL") == leagues.TOTAL
    assert lg.series_kind("KXMLBSERIES") == leagues.SERIES_WINNER
    assert lg.series_kind("KXMLBCHAMP") == leagues.FUTURE


def test_series_for_leagues_rejects_unknown():
    assert "KXMLBGAME" in leagues.series_for_leagues(["mlb"])
    try:
        leagues.series_for_leagues(["nhl"])
    except KeyError as exc:
        assert "nhl" in str(exc)
    else:
        raise AssertionError("expected KeyError")


def test_parse_ticker_tail():
    assert parse_ticker_tail("26SEP07NYYBOS") == ("2026-09-07", "NYYBOS")
    assert parse_ticker_tail("NOTADATE") == (None, "NOTADATE")
    assert parse_ticker_tail("26XYZ07AB") == (None, "26XYZ07AB")


def test_split_teams_uses_league_table_for_variable_lengths():
    assert split_teams("NYYBOS", "mlb", None) == ("NYY", "BOS")
    assert split_teams("KCLAC", "nfl", None) == ("KC", "LAC")
    assert split_teams("SFSD", "mlb", None) == ("SF", "SD")
    # college: fall back to the YES side as the anchor
    assert split_teams("OSUMICH", "ncaaf", "MICH") == ("OSU", "MICH")
    assert split_teams("OSUMICH", "ncaaf", "OSU") == ("OSU", "MICH")
    assert split_teams("OSUMICH", "ncaaf", None) == (None, None)


def test_teams_from_title():
    assert teams_from_title("Yankees at Red Sox") == ("Yankees", "Red Sox")
    assert teams_from_title("Ohio State vs Michigan: Winner?") == ("Ohio State", "Michigan")
    assert teams_from_title("Who wins the World Series?") == (None, None)


def test_classify_moneyline():
    m = mk(
        "KXMLBGAME-26SEP07NYYBOS-NYY",
        "KXMLBGAME",
        "Yankees at Red Sox",
        event_ticker="KXMLBGAME-26SEP07NYYBOS",
        exchange_index=3,
        yes_sub_title="Yankees",
    )
    gm = classify(m, {"title": "Yankees at Red Sox", "strike_date": "2026-09-07T23:10:00Z"})
    assert gm.league == "mlb" and gm.kind == "moneyline"
    assert (gm.away, gm.home, gm.side) == ("NYY", "BOS", "NYY")
    assert gm.game_date == "2026-09-07" and gm.line is None
    assert gm.start_ts is not None and gm.exchange_index == 3
    assert gm.key() == "mlb:2026-09-07:NYY:BOS"


def test_classify_total_and_spread_lines():
    t = mk("KXMLBTOTAL-26SEP07NYYBOS-8P5", "KXMLBTOTAL", "Yankees at Red Sox: total runs")
    gt = classify(t)
    assert gt.kind == "total" and gt.line == 8.5 and (gt.away, gt.home) == ("NYY", "BOS")
    s = mk("KXNFLSPREAD-26SEP07KCLAC-KC", "KXNFLSPREAD", "Chiefs at Chargers", floor_strike=-3.5)
    gs = classify(s)
    assert gs.kind == "spread" and gs.line == -3.5 and gs.side == "KC"
    # total with the line only in the subtitle
    u = mk(
        "KXNBATOTAL-26OCT21LALGSW-X", "KXNBATOTAL", "Lakers at Warriors", yes_sub_title="Over 224.5"
    )
    assert classify(u).line == 224.5


def test_classify_unknown_grammar_degrades_to_none():
    m = mk("KXMLBGAME-SOMETHING", "KXMLBGAME", "A weird title")
    gm = classify(m)
    assert gm.league == "mlb" and gm.game_date is None
    assert gm.away is None and gm.home is None and gm.key() is None


def test_team_names():
    assert teams.full_name("mlb", "NYY") == "New York Yankees"
    assert teams.full_name("mlb", "WAS") == "Washington Nationals"  # alias
    assert teams.abbr_for_name("nfl", "Kansas City Chiefs") == "KC"
    assert teams.abbr_for_name("nba", "Warriors") == "GSW"
    assert teams.abbr_for_name("mlb", "New York") is None  # ambiguous city
    assert teams.abbr_for_name("mlb", "Boston") == "BOS"
    assert teams.same_team("mlb", "BOS", "Boston Red Sox")
    assert not teams.same_team("mlb", "BOS", "New York Yankees")
    assert teams.same_team("ncaaf", "Ohio State", "Ohio State Buckeyes")
    assert teams.same_team("ncaab", "UConn", "UConn Huskies")
    assert not teams.same_team("ncaab", "Duke", "Purdue")
