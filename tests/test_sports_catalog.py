from datetime import UTC, datetime

from sports_fixtures import START_TS, market, rules

from kalshi_sports import leagues, teams
from kalshi_sports.catalog import (
    classify,
    parse_ticker_tail,
    split_teams,
    start_from_rules,
    teams_from_rules,
    teams_from_title,
)


def test_league_for_series_prefix_and_registry():
    assert leagues.league_for_series("KXMLBGAME").key == "mlb"
    assert leagues.league_for_series("KXNCAAFGAME").key == "ncaaf"
    assert leagues.league_for_series("KXNCAAMBGAME").key == "ncaab"
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
    assert "KXNCAAMBGAME" in leagues.series_for_leagues(["ncaab"])
    try:
        leagues.series_for_leagues(["nhl"])
    except KeyError as exc:
        assert "nhl" in str(exc)
    else:
        raise AssertionError("expected KeyError")


def test_parse_ticker_tail_with_and_without_time():
    assert parse_ticker_tail("26SEP101610TEXSEA") == ("2026-09-10", "16:10", "TEXSEA")
    assert parse_ticker_tail("26SEP21NYGLAR") == ("2026-09-21", None, "NYGLAR")
    assert parse_ticker_tail("NOTADATE") == (None, None, "NOTADATE")
    assert parse_ticker_tail("26XYZ07AB") == (None, None, "26XYZ07AB")


def test_split_teams_uses_league_table_for_variable_lengths():
    assert split_teams("NYYBOS", "mlb", None) == ("NYY", "BOS")
    assert split_teams("KCLAC", "nfl", None) == ("KC", "LAC")
    assert split_teams("SFSD", "mlb", None) == ("SF", "SD")
    assert split_teams("TEXSEA", "mlb", None) == ("TEX", "SEA")
    # college: fall back to the YES side as the anchor
    assert split_teams("PURUCLA", "ncaaf", "UCLA") == ("PUR", "UCLA")
    assert split_teams("FRESSJSU", "ncaaf", "FRES") == ("FRES", "SJSU")
    assert split_teams("GRAMTCU", "ncaaf", None) == (None, None)


def test_teams_from_title_and_rules():
    assert teams_from_title("Yankees at Red Sox") == ("Yankees", "Red Sox")
    assert teams_from_title("Ohio State vs Michigan: Winner?") == ("Ohio State", "Michigan")
    assert teams_from_title("Who wins the World Series?") == (None, None)
    assert teams_from_rules(rules("Texas", "Seattle")) == ("Texas", "Seattle")
    assert teams_from_rules(
        "If New York G wins the NY Giants vs LA Rams Pro Football game originally scheduled "
        "for Sep 21, 2026, then the market resolves to Yes."
    ) == ("NY Giants", "LA Rams")
    # totals rules start with "the teams collectively"; the matchup is the last "the A vs B"
    assert teams_from_rules(
        "If the teams collectively score more than 76.5 points in the Grambling St. vs TCU "
        "college football game originally scheduled for Sep 12, 2026, then the market resolves."
    ) == ("Grambling St.", "TCU")
    assert teams_from_rules(None) == (None, None)


def test_start_from_rules():
    ts, exact = start_from_rules(rules("A", "B", when="Sep 10, 2026 at 4:10 PM EDT"))
    assert exact and datetime.fromtimestamp(ts, tz=UTC) == datetime(2026, 9, 10, 20, 10, tzinfo=UTC)
    ts, exact = start_from_rules(rules("A", "B", when="Sep 21, 2026"))
    assert not exact and datetime.fromtimestamp(ts, tz=UTC) == datetime(
        2026, 9, 21, 17, 0, tzinfo=UTC
    )
    assert start_from_rules("no schedule here") == (None, False)


def test_classify_mlb_moneyline_with_time_in_ticker():
    m = market(
        "KXMLBGAME-26SEP101610TEXSEA-SEA",
        "KXMLBGAME",
        "Seattle wins",
        "KXMLBGAME-26SEP101610TEXSEA",
        rules_text=rules("Texas", "Seattle", when="Sep 10, 2026 at 4:10 PM EDT"),
        sub="Seattle",
    )
    gm = classify(m)
    assert gm.league == "mlb" and gm.kind == "moneyline"
    assert (gm.away, gm.home, gm.away_abbr, gm.home_abbr) == ("Texas", "Seattle", "TEX", "SEA")
    assert (gm.side, gm.side_team, gm.side_name) == ("SEA", "SEA", "Seattle")
    assert gm.game_date == "2026-09-10" and gm.line is None
    assert gm.start_exact and datetime.fromtimestamp(gm.start_ts, tz=UTC) == datetime(
        2026, 9, 10, 20, 10, tzinfo=UTC
    )
    assert gm.exchange_index == 3 and gm.key() == "mlb:2026-09-10:TEX:SEA"


def test_classify_spread_and_total_lines_and_sides():
    s = market(
        "KXMLBSPREAD-26SEP071510MINDET-DET6",
        "KXMLBSPREAD",
        "Detroit wins by over 5.5 runs?",
        "KXMLBSPREAD-26SEP071510MINDET",
        rules_text="If Detroit wins by more than 5.5 runs in the Minnesota vs Detroit "
        "professional baseball game originally scheduled for Sep 7, 2026 at 3:10 PM EDT, "
        "then the market resolves to Yes.",
        sub="Detroit wins by over 5.5 runs",
        strike=5.5,
    )
    gs = classify(s)
    assert gs.kind == "spread" and gs.line == 5.5
    assert (gs.side, gs.side_team, gs.side_name) == ("DET6", "DET", "Detroit")
    assert (gs.away, gs.home) == ("Minnesota", "Detroit")

    t = market(
        "KXMLBTOTAL-26SEP082210CINLAD-9",
        "KXMLBTOTAL",
        "Over 8.5 runs scored",
        "KXMLBTOTAL-26SEP082210CINLAD",
        rules_text="If Cincinnati and Los Angeles D collectively score more 8.5 runs in the "
        "Cincinnati vs Los Angeles D professional baseball game originally scheduled for "
        "Sep 8, 2026 at 10:10 PM EDT, then the market resolves to Yes.",
        sub="Over 8.5 runs scored",
        strike=8.5,
    )
    gt = classify(t)
    assert gt.kind == "total" and gt.line == 8.5 and gt.side_team is None
    assert gt.side_name == "over" and (gt.away_abbr, gt.home_abbr) == ("CIN", "LAD")
    assert gt.home == "Los Angeles D"
    # 10:10 PM EDT on Sep 8 is Sep 9 02:10 UTC
    assert datetime.fromtimestamp(gt.start_ts, tz=UTC) == datetime(2026, 9, 9, 2, 10, tzinfo=UTC)

    # line only in the subtitle when the strike is missing
    u = market(
        "KXNBATOTAL-26OCT21LALGSW-225", "KXNBATOTAL", "Over 224.5", "E", sub="Over 224.5 points"
    )
    assert classify(u).line == 224.5


def test_classify_nfl_and_college_without_time_is_approximate():
    n = market(
        "KXNFLSPREAD-26SEP14DENKC-KC8",
        "KXNFLSPREAD",
        "Kansas City wins by over 7.5 points?",
        "KXNFLSPREAD-26SEP14DENKC",
        rules_text="If Kansas City wins by more than 7.5 points in the Denver vs Kansas City Pro "
        "Football game originally scheduled for Sep 14, 2026, then the market resolves to Yes.",
        strike=7.5,
    )
    gn = classify(n)
    assert (gn.away_abbr, gn.home_abbr, gn.side_team, gn.line) == ("DEN", "KC", "KC", 7.5)
    assert gn.start_ts is not None and not gn.start_exact
    c = market(
        "KXNCAAFGAME-26SEP19FRESSJSU-SJSU",
        "KXNCAAFGAME",
        "San Jose St. wins",
        "KXNCAAFGAME-26SEP19FRESSJSU",
        rules_text=rules("Fresno St.", "San Jose St.", "college football", "Sep 19, 2026"),
        sub="San Jose St.",
    )
    gc = classify(c)
    assert (gc.away, gc.home, gc.away_abbr, gc.home_abbr) == (
        "Fresno St.",
        "San Jose St.",
        "FRES",
        "SJSU",
    )
    assert gc.side_name == "San Jose St." and not gc.start_exact
    # an event record with a real start time makes it exact
    ge = classify(c, {"title": "Fresno St. vs San Jose St.", "strike_date": "2026-09-20T02:30:00Z"})
    assert ge.start_exact and datetime.fromtimestamp(ge.start_ts, tz=UTC).hour == 2


def test_classify_unknown_grammar_degrades_to_none():
    gm = classify(market("KXMLBGAME-SOMETHING", "KXMLBGAME", "A weird title", None))
    assert gm.league == "mlb" and gm.game_date is None
    assert gm.away is None and gm.home is None and gm.key() is None and gm.start_ts is None


def test_team_names():
    assert teams.full_name("mlb", "NYY") == "New York Yankees"
    assert teams.full_name("mlb", "WAS") == "Washington Nationals"  # alias
    assert teams.abbr_for_name("nfl", "Kansas City Chiefs") == "KC"
    assert teams.abbr_for_name("nba", "Warriors") == "GSW"
    assert teams.abbr_for_name("mlb", "New York") is None  # ambiguous city
    assert teams.abbr_for_name("mlb", "Boston") == "BOS"
    # Kalshi's disambiguated city names
    assert teams.abbr_for_name("mlb", "Los Angeles D") == "LAD"
    assert teams.abbr_for_name("nfl", "New York G") == "NYG"
    assert teams.same_team("mlb", "Los Angeles D", "Los Angeles Dodgers")
    assert teams.same_team("mlb", "BOS", "Boston Red Sox")
    assert not teams.same_team("mlb", "BOS", "New York Yankees")
    assert teams.same_team("ncaaf", "San Jose St.", "San Jose State Spartans")
    assert teams.same_team("ncaaf", "Ohio State", "Ohio State Buckeyes")
    assert not teams.same_team("ncaab", "Duke", "Purdue")


def test_seed_fixture_start_is_exact():
    from sports_fixtures import seed

    from kalshi_sports.storage import SportsDataStore

    store = SportsDataStore()
    seed(store, odds=False)
    assert store.event_start("KXMLBGAME-26SEP071910NYYBOS") == (START_TS, True)
