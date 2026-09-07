import sqlite3

import pytest
from sports_fixtures import CLOSE_TS, NOW, seed

from kalshi_sports import cli
from kalshi_sports.storage import SCHEMA_VERSION, SchemaMismatch, SportsDataStore


def test_store_schema_version_and_mismatch(tmp_path):
    path = tmp_path / "s.sqlite"
    with SportsDataStore(path) as store:
        assert store.stats()["markets"] == 0
        store._conn.execute(
            "UPDATE meta SET value = ? WHERE key='schema_version'", (str(SCHEMA_VERSION + 1),)
        )
        store._conn.commit()
    with pytest.raises(SchemaMismatch):
        SportsDataStore(path)


def test_store_migrates_a_version_1_database(tmp_path):
    path = tmp_path / "v1.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('schema_version', '1');
        CREATE TABLE markets (ticker TEXT PRIMARY KEY, event_ticker TEXT, series_ticker TEXT,
            league TEXT, kind TEXT, game_date TEXT, away TEXT, home TEXT, side TEXT, line REAL,
            title TEXT, subtitle TEXT, rules TEXT, exchange_index INTEGER, open_ts REAL,
            close_ts REAL, expiration_ts REAL, status TEXT, result TEXT, expiration_value REAL,
            first_seen_ts REAL NOT NULL, last_seen_ts REAL NOT NULL, settled_ts REAL, raw TEXT);
        CREATE TABLE events (event_ticker TEXT PRIMARY KEY, series_ticker TEXT, league TEXT,
            game_date TEXT, away TEXT, home TEXT, start_ts REAL, title TEXT, status TEXT,
            first_seen_ts REAL NOT NULL, last_seen_ts REAL NOT NULL, raw TEXT);
        """
    )
    conn.commit()
    conn.close()
    with SportsDataStore(path) as store:
        cols = {r[1] for r in store._conn.execute("PRAGMA table_info(markets)")}
        assert {"away_abbr", "home_abbr", "side_team", "side_name"} <= cols
        ecols = {r[1] for r in store._conn.execute("PRAGMA table_info(events)")}
        assert {"start_exact", "start_source"} <= ecols
        seed(store, odds=False)  # writes through the new columns
        assert store.stats()["markets"] == 1


def test_store_pending_settlements_and_event_start():
    store = SportsDataStore()
    m = seed(store, odds=False)
    assert store.pending_settlements(CLOSE_TS + 100) == []  # inside the 600 s grace
    assert store.pending_settlements(CLOSE_TS + 700) == [m.ticker]
    assert store.event_start("KXMLBGAME-26SEP071910NYYBOS") == (1788822600.0, True)
    assert store.event_start(None) == (None, False)
    assert store.last_snapshot_ts(m.ticker) == NOW
    assert store.events_needing_start("mlb", "2026-09-01") == []


def test_set_event_start_and_needing_start():
    from sports_fixtures import market, rules

    from kalshi_sports.catalog import classify

    store = SportsDataStore()
    m = market(
        "KXNFLGAME-26SEP07KCLAC-KC",
        "KXNFLGAME",
        "Kansas City wins",
        "KXNFLGAME-26SEP07KCLAC",
        rules_text=rules("Kansas City", "Los Angeles C", "Pro Football", "Sep 7, 2026"),
    )
    gm = classify(m)
    store.upsert_event(gm, NOW, None)
    rows = store.events_needing_start("nfl", "2026-09-07")
    assert len(rows) == 1 and rows[0]["away_abbr"] == "KC"
    assert store.events_needing_start("nfl", "2026-09-08") == []
    store.set_event_start(gm.event_ticker, 1788826800.0, "espn")
    assert store.event_start(gm.event_ticker) == (1788826800.0, True)
    assert store.events_needing_start("nfl", "2026-09-07") == []


def test_upsert_market_keeps_parsed_fields_on_conflict():
    store = SportsDataStore()
    m = seed(store, odds=False)
    row = store._conn.execute("SELECT away, home, first_seen_ts FROM markets").fetchone()
    assert (row["away"], row["home"], row["first_seen_ts"]) == ("New York Y", "Boston", NOW)
    from kalshi_sports.catalog import GameMarket

    blank = GameMarket(
        m.ticker,
        m.event_ticker,
        m.series_ticker,
        "mlb",
        "moneyline",
        None,
        None,
        None,
        None,
        None,
        "NYY",
        "NYY",
        None,
        None,
        None,
        False,
        m.title,
        None,
        None,
        None,
    )
    store.upsert_market(m, blank, NOW + 10)
    row = store._conn.execute("SELECT away, home, away_abbr, last_seen_ts FROM markets").fetchone()
    assert (row["away"], row["home"], row["away_abbr"], row["last_seen_ts"]) == (
        "New York Y",
        "Boston",
        "NYY",
        NOW + 10,
    )


def test_cli_parser_and_offline_commands(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("KALSHI_ENV", "demo")
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    db = tmp_path / "sports.sqlite"
    with SportsDataStore(db) as store:
        seed(store)
    assert cli.main(["devig", "-150", "+130"]) == 0
    out = capsys.readouterr().out
    assert "overround" in out and "shin" in out

    assert cli.main(["record-stats", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "markets: 1" in out and "KXMLBGAME" in out and "quotes from 2 books" in out
    assert "exact start: 1 of 1" in out

    assert cli.main(["record-dump", "--db", str(db), "--width", "40"]) == 0
    out = capsys.readouterr().out
    assert "== odds" in out and "== game_state" in out and "(empty)" in out

    assert cli.main(["compare", "--db", str(db), "--games"]) == 0
    out = capsys.readouterr().out
    assert "open moneyline markets" in out and "consensus by game" in out


def test_cli_odds_test_requires_key(monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.setenv("KALSHI_ENV", "demo")
    with pytest.raises(SystemExit):
        cli.main(["odds-test", "mlb"])


def test_cli_rejects_unknown_league():
    p = cli.build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["record", "--league", "nhl"])
    args = p.parse_args(["record", "--league", "ncaaf", "--league", "ncaab", "--ticks", "1"])
    assert args.league == ["ncaaf", "ncaab"] and args.func is cli.cmd_record
    assert args.book_window == 24.0
