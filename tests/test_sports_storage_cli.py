import pytest
from sports_fixtures import NOW, seed

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


def test_store_pending_settlements_and_event_start():
    store = SportsDataStore()
    m = seed(store, odds=False)
    # close is 2026-09-08T03:00Z = 1788836400; grace 600 s
    assert store.pending_settlements(1788836400.0 + 100) == []
    assert store.pending_settlements(1788836400.0 + 700) == [m.ticker]
    assert store.event_start("KXMLBGAME-26SEP07NYYBOS") == 1788822600.0
    assert store.event_start(None) is None
    assert store.last_snapshot_ts(m.ticker) == NOW


def test_upsert_market_keeps_parsed_fields_on_conflict():
    store = SportsDataStore()
    m = seed(store, odds=False)
    row = store._conn.execute("SELECT away, home, first_seen_ts FROM markets").fetchone()
    assert (row["away"], row["home"], row["first_seen_ts"]) == ("NYY", "BOS", NOW)
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
        "NYY",
        None,
        None,
        m.title,
        None,
        None,
        None,
    )
    store.upsert_market(m, blank, NOW + 10)
    row = store._conn.execute("SELECT away, home, last_seen_ts FROM markets").fetchone()
    assert (row["away"], row["home"], row["last_seen_ts"]) == ("NYY", "BOS", NOW + 10)


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
