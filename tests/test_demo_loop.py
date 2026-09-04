import dataclasses
import json
import threading
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kalshi_bot import demo_ui
from kalshi_bot.client import DryRunOrder
from kalshi_bot.demo_loop import (
    DemoLoop,
    LoopConfig,
    LoopState,
    OpenTrade,
    RefusedProduction,
    SeriesState,
)
from kalshi_bot.models import Fill, Market, Order

T0 = 1_800_000_000.0


def market(i, yes_ask=0.55, no_ask=0.47, result=None, status="open", series="KXBTC15M"):
    open_ts = T0 + i * 900
    return Market.from_dict(
        {
            "ticker": f"{series}-{i}",
            "series_ticker": series,
            "status": status,
            "open_time": open_ts,
            "close_time": open_ts + 900,
            "yes_ask_dollars": f"{yes_ask:.3f}",
            "no_ask_dollars": f"{no_ask:.3f}",
            "yes_bid_dollars": f"{yes_ask - 0.01:.3f}",
            "no_bid_dollars": f"{no_ask - 0.01:.3f}",
            "result": result,
        }
    )


class FakeClient:
    """Scripted demo exchange: one open market per 15-minute window."""

    def __init__(self, results, dry_run=False, fill=True, asks=None, series=("KXBTC15M",)):
        self.is_prod = False
        self.dry_run = dry_run
        self.results = results  # index -> "yes" | "no" (same for every series)
        self.fill = fill
        self.asks = asks or {}
        self.series = series
        self.orders: list[dict] = []
        self.cancelled: list[str] = []
        self.now = T0

    def _window(self):
        return int((self.now - T0) // 900)

    def get_markets(self, *, series_ticker=None, status=None, **kw):
        if series_ticker not in self.series:
            return []
        i = self._window()
        kw2 = self.asks.get(i, {})
        return [market(i, series=series_ticker, **kw2)]

    def get_market(self, ticker):
        series, i = ticker.rsplit("-", 1)
        i = int(i)
        closed = self.now >= T0 + (i + 1) * 900
        return market(
            i, result=self.results.get(i) if closed else None, status="settled", series=series
        )

    def create_order(self, ticker, *, side, action, count, price, order_type, **kw):
        body = {"ticker": ticker, "side": side, "action": action, "count": count, "price": price}
        self.orders.append(body)
        if self.dry_run:
            return DryRunOrder(body)
        return Order.from_dict(
            {
                "order_id": f"o{len(self.orders)}",
                "ticker": ticker,
                "side": side,
                "status": "resting",
            }
        )

    def get_fills(self, *, ticker=None, order_id=None, **kw):
        if not self.fill:
            return []
        for n, o in enumerate(self.orders, 1):
            if f"o{n}" == order_id:
                return [
                    Fill.from_dict(
                        {
                            "fill_id": f"f{n}",
                            "order_id": order_id,
                            "ticker": o["ticker"],
                            "side": o["side"],
                            "action": "buy",
                            "count_fp": f"{o['count']:.2f}",
                            f"{o['side']}_price_dollars": f"{o['price']:.4f}",
                        }
                    )
                ]
        return []

    def cancel_order(self, order_id, *, ticker=None):
        assert ticker, "the loop should pass the ticker so the exchange can route the cancel"
        self.cancelled.append(order_id)
        return None


def make(tmp_path, client, **cfg_kw):
    cfg_kw.setdefault("series", tuple(client.series))
    cfg = LoopConfig(
        interval=1.0,
        stop_file=tmp_path / "STOP",
        state_file=tmp_path / "state.json",
        **cfg_kw,
    )
    slept = []

    def sleep(s):
        slept.append(s)
        client.now += 60.0  # one tick per minute keeps tests small

    loop = DemoLoop(client, cfg, clock=lambda: client.now, sleep=sleep)
    return loop, slept


def test_refuses_production(tmp_path):
    client = FakeClient({})
    client.is_prod = True
    with pytest.raises(RefusedProduction):
        DemoLoop(client, LoopConfig(state_file=tmp_path / "s.json", stop_file=tmp_path / "STOP"))


def test_config_validation():
    with pytest.raises(ValueError):
        LoopConfig(contracts=0).validate()
    with pytest.raises(ValueError):
        LoopConfig(max_price=1.5).validate()
    with pytest.raises(ValueError):
        LoopConfig(first_side="up").validate()
    with pytest.raises(ValueError):
        LoopConfig(dollars=0).validate()
    LoopConfig().validate()
    assert LoopConfig(dollars=2.0).size(0.55) == 3
    assert LoopConfig(dollars=2.0).size(0.90) == 2
    assert LoopConfig(dollars=0.10).size(0.55) == 1
    assert LoopConfig(contracts=4).size(0.55) == 4


def test_alternates_and_books_settlement(tmp_path):
    client = FakeClient({0: "yes", 1: "yes", 2: "no"})
    loop, _ = make(tmp_path, client, loss_cap=100, profit_target=100, max_trades=3)
    assert loop.run().startswith("max trades")
    sides = [o["side"] for o in client.orders]
    assert sides == ["yes", "no", "yes"]
    s = loop.state
    assert s.trades == 3 and s.wins == 1 and s.losses == 2
    # yes at 0.55 won, no at 0.47 lost, yes at 0.55 lost: 0.45 - 0.47 - 0.55 minus 3 x 2c fees
    assert s.realized_pnl == pytest.approx(-0.57 - 0.06, abs=1e-9)
    assert s.fees_paid == pytest.approx(0.06)
    assert [h["result"] for h in s.history] == ["yes", "yes", "no"]
    assert s.open_trades == []
    # persisted, and reloadable with the nested series state handled
    reloaded = LoopState.load(loop.cfg.state_file)
    assert reloaded.realized_pnl == pytest.approx(s.realized_pnl)
    assert reloaded.series["KXBTC15M"].last_side == "yes"


def test_no_entry_inside_min_ttc(tmp_path):
    client = FakeClient({0: "yes"})
    client.now = T0 + 800  # 100 s to close, under the 120 s no-entry window
    loop, _ = make(tmp_path, client, loss_cap=100, profit_target=100)
    loop.run(max_ticks=1)
    assert client.orders == []
    client.now = T0 + 900 + 10  # next window, fresh market
    loop.run(max_ticks=1)
    assert len(client.orders) == 1 and client.orders[0]["ticker"] == "KXBTC15M-1"


def test_price_cap_skips_the_window(tmp_path):
    client = FakeClient({0: "yes", 1: "no"}, asks={0: {"yes_ask": 0.80}})
    loop, _ = make(tmp_path, client, max_price=0.60, loss_cap=100, profit_target=100)
    loop.run(max_ticks=20)
    # window 0 skipped (yes ask 0.80 > 0.60), window 1 traded with the first side still YES
    assert len(client.orders) == 1
    assert client.orders[0]["ticker"] == "KXBTC15M-1" and client.orders[0]["side"] == "yes"


def test_loss_cap_halts_and_persists(tmp_path):
    # sides alternate yes, no, yes...; results are the opposite each time, so every trade loses
    client = FakeClient({i: ("no" if i % 2 == 0 else "yes") for i in range(10)})
    loop, _ = make(tmp_path, client, loss_cap=1.0, profit_target=100, first_side="yes")
    reason = loop.run()
    assert reason.startswith("loss cap")
    assert loop.state.halted and loop.state.realized_pnl <= -1.0
    # a restart does not reset the cap
    loop2, _ = make(tmp_path, client, loss_cap=1.0, profit_target=100)
    assert loop2.state.halted
    assert loop2.run() == loop.state.halted


def test_profit_target_halts(tmp_path):
    client = FakeClient({i: ("yes" if i % 2 == 0 else "no") for i in range(20)})
    loop, _ = make(tmp_path, client, loss_cap=100, profit_target=1.0)
    reason = loop.run()
    assert reason.startswith("profit target")
    assert loop.state.realized_pnl >= 1.0


def test_no_profit_cap(tmp_path):
    client = FakeClient({i: ("yes" if i % 2 == 0 else "no") for i in range(10)})
    loop, _ = make(tmp_path, client, loss_cap=100, profit_target=None, max_trades=4)
    assert loop.run().startswith("max trades")
    assert loop.state.realized_pnl > 1.0  # would have tripped a $1 target
    with pytest.raises(ValueError):
        LoopConfig(profit_target=0).validate()
    import kalshi_bot.cli as cli

    args = cli.build_parser().parse_args(["demo-trade", "--profit-target", "0"])
    assert cli._loop_config(args).profit_target is None


def test_max_trades(tmp_path):
    client = FakeClient({i: "yes" for i in range(5)})
    loop, _ = make(tmp_path, client, loss_cap=100, profit_target=100, max_trades=2)
    assert loop.run().startswith("max trades")
    assert len(client.orders) == 2 and not loop.state.open_trades  # halts after the last settles


def test_stop_file_cancels_resting_order(tmp_path):
    client = FakeClient({0: "yes"}, fill=False)
    loop, _ = make(tmp_path, client, loss_cap=100, profit_target=100)
    loop.run(max_ticks=2)
    ss = loop.state.series["KXBTC15M"]
    assert ss.open is not None and not ss.open.filled
    (tmp_path / "STOP").write_text("x")
    reason = loop.run(max_ticks=5)
    assert reason.startswith("stop file")
    assert client.cancelled == ["o1"]
    assert ss.open is None and loop.state.trades == 0 and ss.last_side is None
    assert loop.state.stopped == reason


def test_unfilled_order_cancelled_before_close(tmp_path):
    client = FakeClient({0: "yes", 1: "no"}, fill=False)
    loop, _ = make(tmp_path, client, loss_cap=100, profit_target=100)
    loop.run(max_ticks=16)
    assert client.cancelled == ["o1"]
    assert loop.state.trades == 1  # window 1 entered afresh; alternation restarted at YES
    assert client.orders[1]["side"] == "yes"


def test_keyboard_interrupt_saves_state(tmp_path):
    client = FakeClient({0: "yes"})
    loop, _ = make(tmp_path, client, loss_cap=100, profit_target=100)

    def boom(_):
        raise KeyboardInterrupt

    loop.sleep = boom
    assert loop.run() == "interrupted"
    assert LoopState.load(loop.cfg.state_file).open_trades


def test_dry_run_simulates_fill(tmp_path):
    client = FakeClient({0: "no"}, dry_run=True)
    loop, _ = make(tmp_path, client, loss_cap=100, profit_target=100, max_trades=1)
    assert loop.run().startswith("max trades")
    assert loop.state.trades == 1 and loop.state.losses == 1
    assert loop.state.history[0]["price"] == 0.55 and loop.state.config["env"] == "dry-run"


def test_settlement_waits_for_result(tmp_path):
    client = FakeClient({})  # never settles
    loop, _ = make(tmp_path, client, loss_cap=100, profit_target=100)
    loop.run(max_ticks=20)
    assert loop.state.open_trades and loop.state.trades == 1


def test_state_roundtrip(tmp_path):
    s = LoopState(trades=2, realized_pnl=1.5)
    ss = s.for_series("KXBTC15M")
    ss.last_side = "no"
    ss.open = OpenTrade("T", "yes", 1, 0.5, "o1", T0 + 900, T0, filled_count=1.0, fill_price=0.5)
    s.save(tmp_path / "s.json")
    r = LoopState.load(tmp_path / "s.json")
    rs = r.series["KXBTC15M"]
    assert rs.open == ss.open and r.trades == 2 and rs.last_side == "no"
    assert rs.next_side("yes") == "yes"
    assert SeriesState().next_side("no") == "no"
    for _ in range(150):
        ss.note_ticker(str(_))
    assert len(ss.seen_tickers) == 100
    # an old single-series state file loads without crashing
    (tmp_path / "old.json").write_text(
        json.dumps({"last_side": "yes", "trades": 1, "open": None, "seen_tickers": []})
    )
    assert LoopState.load(tmp_path / "old.json").trades == 1


def test_two_series_alternate_independently(tmp_path):
    client = FakeClient({0: "yes", 1: "no", 2: "yes"}, series=("KXBTC15M", "KXDOGE15M"))
    loop, _ = make(tmp_path, client, loss_cap=100, profit_target=100, dollars=2.0, max_trades=6)
    assert loop.run().startswith("max trades")
    by_series = {}
    for o in client.orders:
        by_series.setdefault(o["ticker"].rsplit("-", 1)[0], []).append(o)
    assert [o["side"] for o in by_series["KXBTC15M"]] == ["yes", "no", "yes"]
    assert [o["side"] for o in by_series["KXDOGE15M"]] == ["yes", "no", "yes"]
    # $2 at a 55c ask is 3 contracts; at 47c it is 4
    assert [o["count"] for o in by_series["KXBTC15M"]] == [3, 4, 3]
    assert loop.state.trades == 6 and loop.state.wins == 6
    assert {h["series"] for h in loop.state.history} == {"KXBTC15M", "KXDOGE15M"}
    text = loop.state.summary()
    assert "trades=6" in text


def test_cap_waits_for_open_positions(tmp_path):
    # every trade loses; the loss cap trips while the second series still has a position
    client = FakeClient(
        {i: ("no" if i % 2 == 0 else "yes") for i in range(10)},
        series=("KXBTC15M", "KXDOGE15M"),
    )
    loop, _ = make(tmp_path, client, loss_cap=1.0, profit_target=100)
    reason = loop.run()
    assert reason.startswith("loss cap")
    assert loop.state.open_trades == []  # nothing left dangling on the exchange
    assert loop.state.halted


# ---------------------------------------------------------------- dashboard


@pytest.fixture
def dashboard(tmp_path):
    server = demo_ui.serve(tmp_path / "state.json", tmp_path / "STOP", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, tmp_path
    server.shutdown()
    server.server_close()


def _get(server, path, method="GET"):
    port = server.server_address[1]
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read()


def test_dashboard_serves_page_and_state(dashboard):
    server, tmp_path = dashboard
    status, body = _get(server, "/")
    assert status == 200 and b"Kalshi demo loop" in body
    status, body = _get(server, "/api/state")
    data = json.loads(body)
    assert data["state"] is None and data["alive"] is False and not data["stop_file_present"]

    state = LoopState(trades=1, realized_pnl=0.4, last_tick_ts=datetime.now(UTC).timestamp())
    state.save(tmp_path / "state.json")
    data = json.loads(_get(server, "/api/state")[1])
    assert data["state"]["trades"] == 1 and data["alive"] is True

    status, body = _get(server, "/api/stop", method="POST")
    assert status == 200 and Path(tmp_path / "STOP").exists()
    assert json.loads(body)["stop_file_present"] is True
    _get(server, "/api/clear-stop", method="POST")
    assert not Path(tmp_path / "STOP").exists()
    with pytest.raises(urllib.error.HTTPError):
        _get(server, "/nope")


def test_dashboard_snapshot_handles_partial_write(tmp_path):
    dash = demo_ui.Dashboard(tmp_path / "s.json", tmp_path / "STOP")
    (tmp_path / "s.json").write_text("{not json")
    snap = dash.snapshot(now=T0)
    assert snap["state"] is None and snap["alive"] is False
    LoopState(last_tick_ts=T0 - 100, halted=None).save(tmp_path / "s.json")
    assert dash.snapshot(now=T0)["alive"] is False  # stale heartbeat
    LoopState(last_tick_ts=T0 - 5, halted="loss cap").save(tmp_path / "s.json")
    assert dash.snapshot(now=T0)["alive"] is False  # halted


# ---------------------------------------------------------------- live path


def test_production_allowed_only_explicitly(tmp_path):
    client = FakeClient({0: "yes"})
    client.is_prod = True
    cfg = LoopConfig(
        series=("KXBTC15M",), state_file=tmp_path / "s.json", stop_file=tmp_path / "STOP"
    )
    with pytest.raises(RefusedProduction):
        DemoLoop(client, cfg)
    loop = DemoLoop(client, cfg, allow_production=True)
    assert loop.live is True and loop.state.config["env"] == "LIVE"
    client.dry_run = True
    assert DemoLoop(client, cfg, allow_production=True).live is False


def test_live_trade_cli_gates(monkeypatch, capsys):
    import kalshi_bot.cli as cli

    parser = cli.build_parser()
    args = parser.parse_args(["live-trade", "--dollars", "2"])
    assert args.loss_cap == 40.0 and args.state_file == "state/live_loop.json"
    demo = cli.Settings.from_env("/nonexistent")
    with pytest.raises(SystemExit):
        cli.cmd_live_trade(demo, args)  # env is demo
    prod = dataclasses.replace(demo, env="prod", dry_run=False)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_live_trade(prod, args)  # no --real-money
    assert "--real-money" in str(exc.value)
    args = parser.parse_args(["live-trade", "--dollars", "500", "--real-money"])
    with pytest.raises(SystemExit) as exc:
        cli.cmd_live_trade(prod, args)
    assert "at most" in str(exc.value)
    assert cli.build_parser().parse_args(["demo-trade"]).func is cli.cmd_demo_trade


def test_dashboard_refuses_a_busy_port(dashboard):
    server, tmp_path = dashboard
    port = server.server_address[1]
    with pytest.raises(OSError, match="already in use"):
        demo_ui.serve(tmp_path / "x.json", tmp_path / "STOP", port=port)


def test_dashboard_prefers_freshest_state_file(tmp_path):
    import os

    live, demo = tmp_path / "live.json", tmp_path / "demo.json"
    dash = demo_ui.Dashboard([live, demo], tmp_path / "STOP")
    assert dash.snapshot(now=T0)["state_file"] == str(live)  # neither exists: first candidate
    LoopState(trades=1).save(demo)
    assert dash.snapshot(now=T0)["state"]["trades"] == 1
    LoopState(trades=7).save(live)
    os.utime(demo, (T0, T0))
    os.utime(live, (T0 + 10, T0 + 10))
    assert dash.snapshot(now=T0)["state"]["trades"] == 7
    os.utime(demo, (T0 + 20, T0 + 20))
    assert dash.snapshot(now=T0)["state"]["trades"] == 1
