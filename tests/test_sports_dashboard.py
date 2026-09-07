import json
import threading
import urllib.request

from sports_fixtures import NOW

from kalshi_bot import demo_ui
from kalshi_bot.alerts import AlertLog
from kalshi_sports.storage import SportsDataStore


def _seed_sports_state(tmp_path, *, mode="paper", ts=NOW):
    status = {
        "mode": mode,
        "ts": ts,
        "risk": "day +0.00/-25, total -1.50/-50, streak 1",
        "params": {"margin": 0.03, "size_scale": 1.0, "version": 1, "note": "defaults"},
        "open": 1,
        "open_dollars": 4.89,
        "settled": 1,
        "net": -1.5,
        "wins": 0,
        "fees": 0.18,
        "decisions": 7,
        "entries": 2,
        "avg_clv_consensus": 0.012,
        "avg_clv_kalshi": 0.02,
        "last_tick": {"candidates": 4, "entries": 0, "skips": 4},
    }
    (tmp_path / f"sports_{mode}.json").write_text(json.dumps(status))
    with SportsDataStore(tmp_path / "sports_data.sqlite") as store:
        for i, (st, net) in enumerate((("open", None), ("settled", -1.5))):
            pid = store.open_position(
                {
                    "mode": mode,
                    "ticker": f"KXMLBGAME-26SEP071910NYYBOS-{'NYY' if i else 'BOS'}",
                    "event_ticker": "KXMLBGAME-26SEP071910NYYBOS",
                    "league": "mlb",
                    "side": "yes",
                    "side_team": "NYY" if i else "BOS",
                    "contracts": 10,
                    "price": 0.45,
                    "fee": 0.18,
                    "dollars": 4.68,
                    "order_id": "paper",
                    "opened_ts": NOW - 100 + i,
                    "start_ts": NOW + 3600,
                    "p_entry": 0.52,
                    "edge_entry": 0.05,
                }
            )
            if st == "settled":
                store.settle_position(pid, status="settled", result="no", net=net, now=NOW)
    AlertLog(tmp_path / "sports_alerts.jsonl").record(
        "info", "sports-trader", "KXMLBGAME-... buy yes x10 @ 0.45", now=NOW - 50
    )
    return status


def test_sports_snapshot_reads_status_positions_and_alerts(tmp_path):
    _seed_sports_state(tmp_path)
    dash = demo_ui.Dashboard(tmp_path / "live_loop.json", tmp_path / "STOP")
    snap = dash.sports_snapshot(now=NOW + 10)
    assert snap["heartbeat"] == "alive" and snap["alive"] is True
    assert snap["status"]["mode"] == "paper" and snap["status"]["net"] == -1.5
    assert snap["db"] and snap["db"].endswith("sports_data.sqlite")
    assert len(snap["positions"]["open"]) == 1 and len(snap["positions"]["settled"]) == 1
    assert snap["positions"]["settled"][0]["net"] == -1.5
    assert [a["source"] for a in snap["alerts"]] == ["sports-trader"]
    # its files are the sports ones, never the crypto loop's
    assert snap["stop_file"].endswith("SPORTS_STOP") and snap["pause_file"].endswith("SPORTS_PAUSE")
    # a silent loop is reported stale; no status at all is "none"
    assert dash.sports_snapshot(now=NOW + 1000)["heartbeat"] == "stale"
    empty = demo_ui.Dashboard(tmp_path / "x" / "live.json", tmp_path / "x" / "STOP")
    e = empty.sports_snapshot(now=NOW)
    assert (
        e["heartbeat"] == "none"
        and e["db"] is None
        and e["positions"]
        == {
            "open": [],
            "settled": [],
        }
    )


def test_sports_controls_touch_only_sports_files(tmp_path):
    dash = demo_ui.Dashboard(tmp_path / "live_loop.json", tmp_path / "STOP")
    assert dash.sports_control("stop") and (tmp_path / "SPORTS_STOP").exists()
    assert not (tmp_path / "STOP").exists()
    assert dash.sports_control("pause") and (tmp_path / "SPORTS_PAUSE").exists()
    assert dash.sports_control("resume") and not (tmp_path / "SPORTS_PAUSE").exists()
    assert dash.sports_control("clear-stop") and not (tmp_path / "SPORTS_STOP").exists()
    assert dash.sports_control("bogus") is False
    # and the crypto controls never touch the sports files
    dash.stop()
    assert (tmp_path / "STOP").exists() and not (tmp_path / "SPORTS_STOP").exists()


def _get(server, path, method="GET"):
    port = server.server_address[1]
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read()


def test_dashboard_serves_sports_panel_and_api(tmp_path):
    _seed_sports_state(tmp_path, mode="live")
    server = demo_ui.serve(tmp_path / "live_loop.json", tmp_path / "STOP", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _get(server, "/")
        page = body.decode()
        assert status == 200
        assert "Sports desk" in page and "kalshi-sports" in page and "/api/sports" in page
        assert "Stop sports loop" in page and "Closing-line value" in page
        data = json.loads(_get(server, "/api/sports")[1])
        assert data["status"]["mode"] == "live" and len(data["positions"]["open"]) == 1
        status, body = _get(server, "/api/sports/stop", method="POST")
        assert status == 200 and json.loads(body)["stop_file_present"] is True
        assert (tmp_path / "SPORTS_STOP").exists() and not (tmp_path / "STOP").exists()
        _get(server, "/api/sports/clear-stop", method="POST")
        assert not (tmp_path / "SPORTS_STOP").exists()
        # the crypto endpoints are untouched by the sports state
        crypto = json.loads(_get(server, "/api/state")[1])
        assert crypto["state"] is None
    finally:
        server.shutdown()
        server.server_close()
