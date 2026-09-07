import sqlite3

import pytest
from sports_fixtures import NOW, START_TS, seed

from kalshi_bot.fees import fee_per_contract
from kalshi_sports.compare import compare, format_table
from kalshi_sports.consensus import consensus_for_game, recent_games
from kalshi_sports.matching import GameRef, match_game
from kalshi_sports.storage import SportsDataStore


def conn_of(store):
    c = store._conn
    c.row_factory = sqlite3.Row
    return c


def test_match_game_by_teams_and_date():
    refs = [
        GameRef("mlb", "g1", 1788822600.0, "Boston Red Sox", "New York Yankees"),
        GameRef("mlb", "g2", 1788822600.0, "Los Angeles Dodgers", "San Diego Padres"),
        GameRef("nfl", "g3", 1788822600.0, "Boston Red Sox", "New York Yankees"),
    ]
    assert match_game("mlb", "2026-09-07", "NYY", "BOS", refs).game_id == "g1"
    assert match_game("mlb", "2026-09-07", "BOS", "NYY", refs).game_id == "g1"  # swapped
    assert match_game("mlb", "2026-09-07", "SD", "LAD", refs).game_id == "g2"
    # Kalshi display names, including the disambiguated city
    assert match_game("mlb", "2026-09-07", "San Diego", "Los Angeles D", refs).game_id == "g2"
    assert match_game("mlb", "2026-09-20", "NYY", "BOS", refs) is None  # wrong date
    assert match_game("mlb", "2026-09-07", None, "BOS", refs) is None
    # alternates: abbreviation unknown, name known
    assert (
        match_game(
            "mlb", "2026-09-07", None, None, refs, alt_away="Yankees", alt_home="Red Sox"
        ).game_id
        == "g1"
    )


def test_match_game_college_by_abbreviation_or_name():
    refs = [
        GameRef(
            "ncaaf",
            "c1",
            1788822600.0,
            "San Jose State Spartans",
            "Fresno State Bulldogs",
            "SJSU",
            "FRES",
        ),
    ]
    assert match_game("ncaaf", "2026-09-07", "FRES", "SJSU", refs).game_id == "c1"
    assert match_game("ncaaf", "2026-09-07", "Fresno St.", "San Jose St.", refs).game_id == "c1"
    assert match_game("ncaaf", "2026-09-07", "Nevada", "San Jose St.", refs) is None


def test_match_game_doubleheader_prefers_same_eastern_date():
    refs = [
        GameRef("mlb", "early", 1788822600.0, "Boston Red Sox", "New York Yankees"),  # Sep 7 ET
        GameRef("mlb", "late", 1788822600.0 + 86400, "Boston Red Sox", "New York Yankees"),
    ]
    assert match_game("mlb", "2026-09-07", "NYY", "BOS", refs).game_id == "early"
    assert match_game("mlb", "2026-09-08", "NYY", "BOS", refs).game_id == "late"


def test_consensus_weights_sharp_books_and_needs_full_market():
    store = SportsDataStore()
    seed(store)
    cons = consensus_for_game(conn_of(store), "mlb", "abc123", now=NOW)
    by_market = {c.market: c for c in cons}
    assert set(by_market) == {"h2h", "totals"}
    h2h = by_market["h2h"]
    assert h2h.n_books == 1 and h2h.sharp_books == 1  # draftkings had a bad price
    p_bos = h2h.p("Boston Red Sox")
    assert p_bos == pytest.approx((1 / 2.10) / (1 / 2.10 + 1 / 1.80))
    assert h2h.probs["New York Yankees"] + p_bos == pytest.approx(1.0)
    assert by_market["totals"].point == 8.5
    assert h2h.age_s == pytest.approx(60.0)
    games = recent_games(conn_of(store), "mlb", NOW - 3600)
    assert len(games) == 1 and games[0]["home"] == "Boston Red Sox"


def test_consensus_ignores_stale_quotes():
    store = SportsDataStore()
    seed(store)
    assert consensus_for_game(conn_of(store), "mlb", "abc123", now=NOW + 5 * 3600) == []


def test_compare_reports_edges_net_of_fee():
    store = SportsDataStore()
    seed(store, side="NYY")
    rows = compare(conn_of(store), now=NOW)
    assert len(rows) == 1
    r = rows[0]
    p_nyy = (1 / 1.80) / (1 / 2.10 + 1 / 1.80)
    assert r.p_consensus == pytest.approx(p_nyy)
    assert r.edge_yes == pytest.approx(p_nyy - 0.42 - fee_per_contract(0.42))
    assert r.edge_no == pytest.approx((1 - p_nyy) - 0.60 - fee_per_contract(0.60))
    assert r.note == "" and r.n_books == 1
    assert r.secs_to_start == pytest.approx(START_TS - NOW)
    text = format_table(rows)
    assert "KXMLBGAME-26SEP071910NYYBOS-NYY" in text and "cons" in text.splitlines()[0]


def test_compare_marks_unmatched_markets():
    store = SportsDataStore()
    seed(store, odds=False)
    rows = compare(conn_of(store), now=NOW)
    assert rows[0].p_consensus is None and rows[0].note == "no odds match"
    assert "no odds match" in format_table(rows)
