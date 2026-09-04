import json
import threading
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kalshi_bot import demo_ui
from kalshi_bot.client import DryRunOrder
from kalshi_bot.demo_loop import DemoLoop, LoopConfig, LoopState, OpenTrade, RefusedProduction
from kalshi_bot.models import Fill, Market, Order

T0 = 1_800_000_000.0


def market(i, yes_ask=0.55, no_ask=0.47, result=None, status="open"):
    open_ts = T0 + i * 900
    return Market.from_dict(
        {
            "ticker": f"KXBTC15M-{i}",
            "series_ticker": "KXBTC15M",
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

    def __init__(self, results, dry_run=False, fill=True, asks=None):
        self.is_prod = False
        self.dry_run = dry_run
        self.results = results  # index -> "yes" | "no"
        self.fill = fill
        self.asks = asks or {}
        self.orders: list[dict] = []
        self.cancelled: list[str] = []
        self.now = T0

    def _window(self):
        return int((self.now - T0) // 900)

    def get_markets(self, *, series_ticker=None, status=None, **kw):
        i = self._window()
        kw2 = self.asks.get(i, {})
        return [market(i, **kw2)]

    def get_market(self, ticker):
        i = int(ticker.rsplit("-", 1)[1])
        closed = self.now >= T0 + (i + 1) * 900
        return market(i, result=self.results.get(i) if closed else None, status="settled")

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

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return None


def make(tmp_path, client, **cfg_kw):
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
    LoopConfig().validate()


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
    assert s.open is None
    # persisted, and reloadable with the open field handled
    reloaded = LoopState.load(loop.cfg.state_file)
    assert reloaded.realized_pnl == pytest.approx(s.realized_pnl) and reloaded.last_side == "yes"


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


def test_max_trades(tmp_path):
    client = FakeClient({i: "yes" for i in range(5)})
    loop, _ = make(tmp_path, client, loss_cap=100, profit_target=100, max_trades=2)
    assert loop.run().startswith("max trades")
    assert len(client.orders) == 2 and loop.state.open is None  # halts after the last settles


def test_stop_file_cancels_resting_order(tmp_path):
    client = FakeClient({0: "yes"}, fill=False)
    loop, _ = make(tmp_path, client, loss_cap=100, profit_target=100)
    loop.run(max_ticks=2)
    assert loop.state.open is not None and not loop.state.open.filled
    (tmp_path / "STOP").write_text("x")
    reason = loop.run(max_ticks=5)
    assert reason.startswith("stop file")
    assert client.cancelled == ["o1"]
    assert loop.state.open is None and loop.state.trades == 0 and loop.state.last_side is None
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
    assert LoopState.load(loop.cfg.state_file).open is not None


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
    assert loop.state.open is not None and loop.state.trades == 1


def test_state_roundtrip(tmp_path):
    s = LoopState(trades=2, realized_pnl=1.5, last_side="no")
    s.open = OpenTrade("T", "yes", 1, 0.5, "o1", T0 + 900, T0, filled_count=1.0, fill_price=0.5)
    s.save(tmp_path / "s.json")
    r = LoopState.load(tmp_path / "s.json")
    assert r.open == s.open and r.trades == 2 and r.last_side == "no"
    assert r.next_side("yes") == "yes"
    assert LoopState().next_side("no") == "no"
    for _ in range(150):
        s.note_ticker(str(_))
    assert len(s.seen_tickers) == 100


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
