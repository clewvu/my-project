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
from kalshi_bot.models import Fill, Market, Order, Position

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
        self.positions_override: dict[str, float] | None = None  # ticker -> signed qty
        self.position_calls = 0

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

    def get_positions(self, **kw):
        """What the exchange holds: every filled, uncancelled buy nets out per ticker,
        unless a test overrides it."""
        self.position_calls += 1
        if self.positions_override is not None:
            held = dict(self.positions_override)
        else:
            held = {}
            for n, o in enumerate(self.orders, 1):
                if not self.fill or f"o{n}" in self.cancelled:
                    continue
                signed = o["count"] if o["side"] == "yes" else -o["count"]
                if o["action"] == "sell":
                    signed = -signed
                held[o["ticker"]] = held.get(o["ticker"], 0) + signed
        return [Position.from_dict({"ticker": t, "position": q}) for t, q in held.items() if q]


def make(tmp_path, client, **cfg_kw):
    cfg_kw.setdefault("series", tuple(client.series))
    cfg_kw.setdefault("decision_log", tmp_path / "decisions.jsonl")
    cfg_kw.setdefault("spot_db", None)
    cfg_kw.setdefault("alerts_path", tmp_path / "alerts.jsonl")
    cfg_kw.setdefault("pause_file", tmp_path / "PAUSE")
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


def test_loop_sells_when_the_strategy_says_exit(tmp_path):
    from kalshi_bot.strategy import Exit, Signal

    class ExitAfterFill:
        name = "exiter"
        size_scale = 0.5  # the learning loop's knob: $2 at 0.55 would be 3, halved -> 1

        def prepare(self, now):
            pass

        def signal(self, market, last_side, now):
            return Signal(side="yes", price=market.yes_ask, reason="in")

        def exit(self, market, side, entry_price, now):
            return Exit(market.yes_bid, "take it", inputs={"bid": market.yes_bid})

    client = FakeClient({0: "no"})  # would have lost at settlement
    cfg = LoopConfig(
        interval=1.0,
        series=("KXBTC15M",),
        dollars=2.0,
        stop_file=tmp_path / "STOP",
        state_file=tmp_path / "state.json",
        decision_log=tmp_path / "decisions.jsonl",
        spot_db=None,
        loss_cap=100,
        profit_target=None,
        max_trades=1,
    )

    def sleep(s):
        client.now += 60.0

    loop = DemoLoop(client, cfg, clock=lambda: client.now, sleep=sleep, strategy=ExitAfterFill())
    assert loop.run().startswith("max trades")
    buys = [o for o in client.orders if o["action"] == "buy"]
    sells = [o for o in client.orders if o["action"] == "sell"]
    assert len(buys) == 1 and buys[0]["count"] == 1  # size halved by size_scale
    assert len(sells) == 1 and sells[0]["price"] == 0.54 and sells[0]["count"] == 1
    h = loop.state.history[-1]
    assert h["result"] == "sold" and h["sold_at"] == 0.54 and h["price"] == 0.55
    # bought at 0.55, sold at 0.54, two fees: a small loss, not the -0.55 settlement loss
    assert -0.06 < h["net"] < 0 and loop.state.open_trades == []
    rows = [json.loads(line) for line in (tmp_path / "decisions.jsonl").read_text().splitlines()]
    assert [r["action"] for r in rows] == ["trade", "exit"]
    # exits can be switched off
    client2 = FakeClient({0: "no"})
    cfg2 = dataclasses.replace(cfg, exits=False, state_file=tmp_path / "s2.json")
    loop2 = DemoLoop(
        client2,
        cfg2,
        clock=lambda: client2.now,
        sleep=lambda s: setattr(client2, "now", client2.now + 60),
        strategy=ExitAfterFill(),
    )
    loop2.run()
    assert not [o for o in client2.orders if o["action"] == "sell"]
    assert loop2.state.history[-1]["result"] == "no"


def test_maker_price_grid():
    from kalshi_bot.demo_loop import maker_price, price_tick

    assert price_tick(0.55) == 0.01 and price_tick(0.05) == 0.001 and price_tick(0.95) == 0.001
    assert maker_price(0.54, 0.56) == 0.55  # room for one tick inside
    assert maker_price(0.55, 0.56) is None  # bid + tick reaches the ask: take
    assert maker_price(None, 0.56) is None
    assert maker_price(0.050, 0.053) == 0.051


def _maker_loop(tmp_path, client, **cfg_kw):
    from kalshi_bot.strategy import Signal

    class Always:
        name = "always"
        size_scale = 1.0

        def prepare(self, now):
            pass

        def signal(self, market, last_side, now):
            return Signal(side="yes", price=market.yes_ask, reason="in")

        def exit(self, market, side, entry_price, now):
            return None

    cfg = LoopConfig(
        interval=1.0,
        series=("KXBTC15M",),
        dollars=2.0,
        entry="maker",
        maker_wait_s=20.0,
        stop_file=tmp_path / "STOP",
        state_file=tmp_path / f"m{id(client)}.json",
        decision_log=None,
        spot_db=None,
        loss_cap=100,
        profit_target=None,
        max_trades=1,
        **cfg_kw,
    )

    def sleep(s):
        client.now += 10.0

    return DemoLoop(client, cfg, clock=lambda: client.now, sleep=sleep, strategy=Always())


def test_maker_entry_rests_inside_the_spread_and_fills(tmp_path):
    client = FakeClient({0: "yes"})  # bid 0.54, ask 0.55 -> bid + tick reaches the ask: takes
    loop = _maker_loop(tmp_path, client)
    loop.run(max_ticks=3)
    assert client.orders[0]["price"] == 0.55 and loop.state.open_trades[0][1].maker is False
    client2 = FakeClient({0: "yes"}, asks={0: {"yes_ask": 0.58}})  # bid 0.57? no: bid = ask-0.01
    # widen the spread: bid 0.55, ask 0.58 -> rest at 0.56
    client2.get_markets = lambda **kw: [
        Market.from_dict(
            {
                "ticker": "KXBTC15M-0",
                "series_ticker": "KXBTC15M",
                "status": "open",
                "close_time": T0 + 900,
                "yes_ask_dollars": "0.580",
                "yes_bid_dollars": "0.550",
                "no_ask_dollars": "0.450",
                "no_bid_dollars": "0.420",
            }
        )
    ]
    loop2 = _maker_loop(tmp_path, client2)
    loop2.run(max_ticks=3)
    o = client2.orders[0]
    assert o["price"] == 0.56 and o["count"] == 3  # $2 / 0.56
    t = loop2.state.open_trades[0][1]
    assert t.maker and t.taker_price == 0.58 and t.filled and t.fill_price == 0.56


def test_maker_falls_back_to_taker_after_the_wait(tmp_path):
    client = FakeClient({0: "yes"}, fill=False)
    wide = Market.from_dict(
        {
            "ticker": "KXBTC15M-0",
            "series_ticker": "KXBTC15M",
            "status": "open",
            "close_time": T0 + 900,
            "yes_ask_dollars": "0.580",
            "yes_bid_dollars": "0.550",
            "no_ask_dollars": "0.450",
            "no_bid_dollars": "0.420",
        }
    )
    client.get_markets = lambda **kw: [wide]
    client.get_market = lambda ticker: wide
    loop = _maker_loop(tmp_path, client)
    loop.run(max_ticks=4)  # 0, 10, 20, 30 s: the maker order is 20 s old at tick 3
    assert [o["price"] for o in client.orders] == [0.56, 0.58]
    assert client.cancelled == ["o1"]
    t = loop.state.open_trades[0][1]
    assert t.maker is False and t.limit_price == 0.58 and t.order_id == "o2"


def test_fixed_fraction_sizing_uses_the_shard_balance(tmp_path):
    from kalshi_bot.models import Balance
    from kalshi_bot.strategy import Signal

    class Sized:
        name = "sized"
        size_scale = 1.0
        risk_fraction = 0.02  # as the learning loop would set it

        def prepare(self, now):
            pass

        def signal(self, market, last_side, now):
            return Signal(side="yes", price=market.yes_ask, reason="in")

    client = FakeClient({0: "yes"})
    client.get_balance = lambda: Balance(balance=1000.0, breakdown={0: 500.0, 2: 500.0})
    cfg = LoopConfig(
        interval=1.0,
        series=("KXBTC15M",),
        dollars=5.0,
        max_dollars=20.0,
        stop_file=tmp_path / "STOP",
        state_file=tmp_path / "s.json",
        decision_log=None,
        spot_db=None,
        loss_cap=100,
        profit_target=None,
        max_trades=1,
    )
    loop = DemoLoop(client, cfg, clock=lambda: client.now, sleep=lambda s: None, strategy=Sized())
    loop.run(max_ticks=2)
    # no exchange_index on the fake market -> total balance 1000 x 2% = $20 -> 36 contracts at 0.55
    assert client.orders[0]["count"] == 36
    # the ceiling applies
    client2 = FakeClient({0: "yes"})
    client2.get_balance = lambda: Balance(balance=100000.0)
    loop2 = DemoLoop(
        client2,
        dataclasses.replace(cfg, state_file=tmp_path / "s2.json"),
        clock=lambda: client2.now,
        sleep=lambda s: None,
        strategy=Sized(),
    )
    loop2.run(max_ticks=2)
    assert client2.orders[0]["count"] == 36  # $20 cap / 0.55
    # --risk-fraction on the command line overrides the strategy's, and 0 means --dollars
    client3 = FakeClient({0: "yes"})
    client3.get_balance = lambda: Balance(balance=1000.0)
    loop3 = DemoLoop(
        client3,
        dataclasses.replace(cfg, state_file=tmp_path / "s3.json", risk_fraction=0.0),
        clock=lambda: client3.now,
        sleep=lambda s: None,
        strategy=Sized(),
    )
    loop3.run(max_ticks=2)
    assert client3.orders[0]["count"] == 9  # $5 / 0.55
    # balance unavailable -> --dollars
    client4 = FakeClient({0: "yes"})
    loop4 = DemoLoop(
        client4,
        dataclasses.replace(cfg, state_file=tmp_path / "s4.json"),
        clock=lambda: client4.now,
        sleep=lambda s: None,
        strategy=Sized(),
    )
    loop4.run(max_ticks=2)
    assert client4.orders[0]["count"] == 9


def _reentry_loop(tmp_path, client, strategy, **cfg_kw):
    cfg = LoopConfig(
        interval=1.0,
        series=("KXBTC15M",),
        dollars=2.0,
        stop_file=tmp_path / "STOP",
        state_file=tmp_path / f"state{id(client)}.json",
        decision_log=None,
        spot_db=None,
        loss_cap=100,
        profit_target=None,
        alerts_path=tmp_path / "alerts.jsonl",
        pause_file=tmp_path / "PAUSE",
        **cfg_kw,
    )

    def sleep(s):
        client.now += 30.0

    return DemoLoop(client, cfg, clock=lambda: client.now, sleep=sleep, strategy=strategy)


class _Churner:
    """Enters whenever allowed and sells at the next tick at a chosen bid."""

    name = "churn"
    size_scale = 1.0

    def __init__(self, sell_at):
        self.sell_at = sell_at

    def prepare(self, now):
        pass

    def signal(self, market, last_side, now):
        from kalshi_bot.strategy import Signal

        return Signal(side="yes", price=market.yes_ask, reason="in")

    def exit(self, market, side, entry_price, now):
        from kalshi_bot.strategy import Exit

        return Exit(self.sell_at, "out")


NO_CHURN_CONTROL = dict(min_hold_s=0, reentry_cooloff_s=0, max_consecutive_losses=0)


def test_reentry_capped_at_two_when_the_market_is_losing(tmp_path):
    client = FakeClient({0: "yes"})
    loop = _reentry_loop(
        tmp_path, client, _Churner(sell_at=0.50), max_entries=6, **NO_CHURN_CONTROL
    )  # sells below entry
    loop.run(max_ticks=25)  # 25 x 30 s: the whole 15-minute window minus the no-entry zone
    buys = [o for o in client.orders if o["action"] == "buy" and o["ticker"] == "KXBTC15M-0"]
    assert len(buys) == 2
    ss = loop.state.series["KXBTC15M"]
    assert ss.entries["KXBTC15M-0"] == 2 and ss.market_pnl["KXBTC15M-0"] < 0


def test_reentry_up_to_six_while_the_market_is_profitable(tmp_path):
    client = FakeClient({0: "yes"})
    loop = _reentry_loop(
        tmp_path, client, _Churner(sell_at=0.70), max_entries=6, **NO_CHURN_CONTROL
    )  # sells well above
    loop.run(max_ticks=25)
    buys = [o for o in client.orders if o["action"] == "buy" and o["ticker"] == "KXBTC15M-0"]
    sells = [o for o in client.orders if o["action"] == "sell" and o["ticker"] == "KXBTC15M-0"]
    assert len(buys) == 6 and len(sells) == 6
    ss = loop.state.series["KXBTC15M"]
    assert ss.entries["KXBTC15M-0"] == 6 and ss.market_pnl["KXBTC15M-0"] > 0
    assert loop.state.realized_pnl > 0.5
    # one entry only when configured so
    client2 = FakeClient({0: "yes"})
    loop2 = _reentry_loop(
        tmp_path, client2, _Churner(sell_at=0.70), max_entries=1, **NO_CHURN_CONTROL
    )
    loop2.run(max_ticks=25)
    assert len([o for o in client2.orders if o["action"] == "buy"]) == 1
    with pytest.raises(ValueError):
        LoopConfig(max_entries=0).validate()
    with pytest.raises(ValueError):
        LoopConfig(min_hold_s=-1).validate()
    with pytest.raises(ValueError):
        LoopConfig(max_consecutive_losses=-1).validate()


def test_min_hold_and_cooloff_slow_the_churn(tmp_path):
    # the churner wants out every tick; with a 60 s hold and a 120 s cool-off a
    # 30-second tick loop can make at most one round trip per three minutes
    client = FakeClient({0: "yes"})
    loop = _reentry_loop(
        tmp_path,
        client,
        _Churner(sell_at=0.70),
        max_entries=6,
        min_hold_s=60,
        reentry_cooloff_s=120,
        max_consecutive_losses=0,
    )
    loop.run(max_ticks=25)
    buys = [o for o in client.orders if o["action"] == "buy"]
    sells = [o for o in client.orders if o["action"] == "sell"]
    assert 3 <= len(buys) <= 5 and len(sells) == len(buys) - (loop.state.open_trades != [])
    ss = loop.state.series["KXBTC15M"]
    assert ss.last_exit_ts["KXBTC15M-0"] > 0
    assert loop.state.loss_streak == 0


class _Flipper(_Churner):
    """Buys YES first, then wants NO on every later signal."""

    def __init__(self, sell_at):
        super().__init__(sell_at)
        self.calls = 0

    def signal(self, market, last_side, now):
        from kalshi_bot.strategy import Signal

        self.calls += 1
        side = "yes" if self.calls == 1 else "no"
        price = market.yes_ask if side == "yes" else market.no_ask
        return Signal(side=side, price=price, reason="flip")


def test_no_flip_within_a_market_unless_allowed(tmp_path):
    client = FakeClient({0: "yes"})
    loop = _reentry_loop(
        tmp_path, client, _Flipper(sell_at=0.70), max_entries=6, **NO_CHURN_CONTROL
    )
    loop.run(max_ticks=25)
    buys = [o for o in client.orders if o["action"] == "buy"]
    assert len(buys) == 1 and buys[0]["side"] == "yes"
    assert loop.state.series["KXBTC15M"].sides_traded["KXBTC15M-0"] == "yes"
    client2 = FakeClient({0: "yes"})
    loop2 = _reentry_loop(
        tmp_path,
        client2,
        _Flipper(sell_at=0.70),
        max_entries=6,
        allow_flip=True,
        **NO_CHURN_CONTROL,
    )
    loop2.run(max_ticks=25)
    buys2 = [o for o in client2.orders if o["action"] == "buy"]
    assert len(buys2) >= 2 and buys2[1]["side"] == "no"


def test_consecutive_loss_breaker_pauses_entries(tmp_path):
    # every settlement loses; after three in a row the loop stops entering for
    # 30 minutes (two windows), then trades again
    client = FakeClient({i: "no" for i in range(12)})
    loop, _ = make(
        tmp_path,
        client,
        loss_cap=100,
        profit_target=None,
        first_side="yes",
        strategy="alternate",
        max_consecutive_losses=3,
        loss_pause_s=1800,
    )
    # alternate buys YES then NO; make the NO markets lose too
    client.results = {i: ("no" if i % 2 == 0 else "yes") for i in range(12)}
    loop.run(max_ticks=16 * 8)  # ~8 windows at one tick a minute
    hist = loop.state.history
    assert len(hist) >= 4 and all(not h["won"] for h in hist)
    entered = sorted({h["ticker"] for h in hist})
    # windows 0,1,2 trade, 3 and 4 are skipped by the breaker, 5 trades again
    assert "KXBTC15M-3" not in entered and "KXBTC15M-4" not in entered
    assert "KXBTC15M-5" in entered
    texts = [a["text"] for a in _alerts(tmp_path)]
    assert any("losses in a row" in t for t in texts) and any("breaker cleared" in t for t in texts)
    assert loop.state.loss_streak >= 3


def test_loop_uses_the_strategy_and_logs_decisions(tmp_path):
    from kalshi_bot.strategy import Signal, Skip

    class PickyStrategy:
        name = "picky"

        def __init__(self):
            self.prepared = 0

        def prepare(self, now):
            self.prepared += 1

        def signal(self, market, last_side, now):
            if market.ticker.endswith("-0"):
                return Skip("waiting for window 1", inputs={"p_yes": 0.5})
            return Signal(side="no", price=market.no_ask, reason="model says no", edge=0.07)

    client = FakeClient({0: "yes", 1: "no"})
    strat = PickyStrategy()
    cfg = LoopConfig(
        interval=1.0,
        series=("KXBTC15M",),
        stop_file=tmp_path / "STOP",
        state_file=tmp_path / "state.json",
        decision_log=tmp_path / "decisions.jsonl",
        spot_db=None,
        loss_cap=100,
        profit_target=None,
        max_trades=1,
    )

    def sleep(s):
        client.now += 60.0

    loop = DemoLoop(client, cfg, clock=lambda: client.now, sleep=sleep, strategy=strat)
    assert loop.run().startswith("max trades")
    assert strat.prepared > 0
    assert [o["ticker"] for o in client.orders] == ["KXBTC15M-1"]
    assert client.orders[0]["side"] == "no" and loop.state.wins == 1
    rows = [json.loads(line) for line in (tmp_path / "decisions.jsonl").read_text().splitlines()]
    assert [r["action"] for r in rows] == ["skip", "trade"]
    assert rows[1]["reason"] == "model says no" and rows[1]["count"] == 1
    assert loop.state.config["strategy"] == "picky"


def test_fairvalue_config_validation(tmp_path):
    with pytest.raises(ValueError):
        LoopConfig(strategy="magic").validate()
    with pytest.raises(ValueError):
        LoopConfig(strategy="fairvalue", vol_window=10).validate()
    LoopConfig(strategy="fairvalue").validate()


def test_settlement_waits_for_result(tmp_path):
    client = FakeClient({})  # never settles
    loop, _ = make(tmp_path, client, loss_cap=100, profit_target=100)
    loop.run(max_ticks=20)
    assert loop.state.open_trades and loop.state.trades == 1


def test_save_retries_when_the_swap_is_refused(tmp_path, monkeypatch):
    from pathlib import Path

    calls = {"n": 0}
    real_replace = Path.replace

    def flaky(self, target):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("locked by another process")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky)
    monkeypatch.setattr("kalshi_bot.demo_loop.time.sleep", lambda s: None)
    LoopState(trades=5).save(tmp_path / "s.json")
    assert calls["n"] == 3 and LoopState.load(tmp_path / "s.json").trades == 5
    # never crashes even if the swap keeps failing
    calls["n"] = -10_000
    LoopState(trades=6).save(tmp_path / "s.json", attempts=2)
    assert LoopState.load(tmp_path / "s.json").trades == 6


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


# ---------------------------------------------------------------- reconciliation, pause, alerts


def _alerts(tmp_path):
    from kalshi_bot.alerts import tail

    return tail(tmp_path / "alerts.jsonl")


def test_reconciliation_passes_when_books_agree(tmp_path):
    client = FakeClient({0: "yes", 1: "no"})
    loop, _ = make(tmp_path, client, max_trades=2, reconcile_s=60.0)
    loop.run()
    assert client.position_calls >= 2
    assert loop.state.reconciled_ts is not None
    assert not any("reconciliation" in a["text"] for a in _alerts(tmp_path))
    levels = [a["level"] for a in _alerts(tmp_path)]
    texts = [a["text"] for a in _alerts(tmp_path)]
    assert texts[0].startswith("loop started") and levels[0] == "info"
    assert any(t.startswith("filled") for t in texts) and any(
        t.startswith("settled") for t in texts
    )
    assert texts[-1].startswith("loop stopped") and levels[-1] == "halt"


def test_reconciliation_halts_on_a_repeated_mismatch(tmp_path):
    client = FakeClient({i: "yes" for i in range(6)})
    client.positions_override = {"KXBTC15M-77": 3}  # a position the loop never opened
    loop, _ = make(tmp_path, client, reconcile_s=60.0)
    assert loop.run(max_ticks=4) == "tick limit"  # halted, but holding market 0 to settlement
    reason = loop.state.halted
    assert reason.startswith("reconciliation mismatch") and "did not open" in reason
    alerts = _alerts(tmp_path)
    warns = [a for a in alerts if a["level"] == "warn" and "reconciliation" in a["text"]]
    halts = [a for a in alerts if a["level"] == "halt"]
    assert len(warns) == 1 and "halts if still there" in warns[0]["text"]
    assert halts and "reconciliation mismatch" in halts[0]["text"]
    assert client.position_calls == 2


def test_reconciliation_forgives_a_one_off_and_skips_simulated(tmp_path):
    client = FakeClient({i: "yes" for i in range(6)})
    client.positions_override = {"KXBTC15M-77": 3}
    loop, _ = make(tmp_path, client, reconcile_s=60.0, max_trades=3)
    assert loop.tick() is None  # warned
    client.positions_override = None  # the exchange now agrees
    client.now += 61
    assert loop.tick() is None and not loop.state.halted
    assert loop._mismatches == {}

    sim = FakeClient({0: "yes"}, dry_run=True)
    sim.positions_override = {"KXBTC15M-0": 99}
    loop, _ = make(tmp_path / "sim", sim, reconcile_s=60.0, max_trades=1)
    loop.run(max_ticks=3)
    assert sim.position_calls == 0 and "reconciliation" not in (loop.state.halted or "")


def test_reconciliation_survives_a_failed_call(tmp_path):
    client = FakeClient({0: "yes"})

    def boom(**kw):
        raise RuntimeError("positions endpoint down")

    client.get_positions = boom
    loop, _ = make(tmp_path, client, max_trades=1, reconcile_s=60.0)
    loop.run()
    assert loop.state.trades == 1
    assert any("reconciliation skipped" in a["text"] for a in _alerts(tmp_path))


def test_pause_file_holds_entries_but_keeps_ticking(tmp_path):
    client = FakeClient({i: "yes" for i in range(6)})
    loop, _ = make(tmp_path, client, max_trades=2)
    assert loop.tick() is None and loop.state.trades == 1
    (tmp_path / "PAUSE").write_text("paused\n")
    client.now += 16 * 60  # past the first market's close; it settles while paused
    assert loop.tick() is None
    assert loop.state.paused and loop.state.wins == 1 and loop.state.trades == 1
    for _ in range(3):
        client.now += 60
        assert loop.tick() is None
    assert loop.state.trades == 1  # nothing new while paused
    (tmp_path / "PAUSE").unlink()
    client.now += 60
    assert loop.tick() is None
    assert not loop.state.paused and loop.state.trades == 2
    texts = [a["text"] for a in _alerts(tmp_path)]
    assert any(t.startswith("paused") for t in texts) and "resumed" in texts


def test_alert_log_tail_and_levels(tmp_path):
    from kalshi_bot.alerts import AlertLog, tail

    log = AlertLog(tmp_path / "a.jsonl")
    for i in range(70):
        log.record("info", "test", f"event {i}", now=T0 + i)
    with pytest.raises(ValueError):
        log.record("loud", "test", "nope")
    rows = tail(tmp_path / "a.jsonl", n=50)
    assert len(rows) == 50 and rows[0]["text"] == "event 20" and rows[-1]["ts"] == T0 + 69
    assert tail(tmp_path / "missing.jsonl") == [] and tail(None) == []
    (tmp_path / "a.jsonl").write_text("{broken\n" + json.dumps({"level": "warn"}) + "\n")
    assert tail(tmp_path / "a.jsonl") == [{"level": "warn"}]
    assert AlertLog(None).record("warn", "x", "unwritten")["level"] == "warn"


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
    assert status == 200 and b"Kalshi 15-minute desk" in body
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


def test_dashboard_pause_resume_and_events(tmp_path):
    from kalshi_bot.alerts import AlertLog

    alerts = tmp_path / "alerts.jsonl"
    AlertLog(alerts).record("info", "live", "filled YES x10", now=T0 - 30)
    AlertLog(alerts).record("warn", "learner", "size cut", now=T0 - 20)
    server = demo_ui.serve(
        tmp_path / "state.json",
        tmp_path / "STOP",
        port=0,
        pause_file=tmp_path / "PAUSE",
        alerts_file=alerts,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        data = json.loads(_get(server, "/api/state")[1])
        assert data["heartbeat"] == "none" and not data["pause_file_present"]
        assert [a["text"] for a in data["alerts"]] == ["filled YES x10", "size cut"]
        status, body = _get(server, "/api/pause", method="POST")
        assert status == 200 and (tmp_path / "PAUSE").exists()
        assert json.loads(body)["pause_file_present"] is True
        _get(server, "/api/resume", method="POST")
        assert not (tmp_path / "PAUSE").exists()
        page = _get(server, "/")[1].decode()
        assert "Pause entries" in page and "Activity" in page and "/api/resume" in page
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_heartbeat_states(tmp_path):
    dash = demo_ui.Dashboard(tmp_path / "s.json", tmp_path / "STOP", alerts_file=None)
    assert dash.snapshot(now=T0)["heartbeat"] == "none"
    LoopState(last_tick_ts=T0 - 5).save(tmp_path / "s.json")
    assert dash.snapshot(now=T0)["heartbeat"] == "alive"
    LoopState(last_tick_ts=T0 - 5, paused=True).save(tmp_path / "s.json")
    assert dash.snapshot(now=T0)["heartbeat"] == "paused"
    LoopState(last_tick_ts=T0 - 5, halted="loss cap").save(tmp_path / "s.json")
    assert dash.snapshot(now=T0)["heartbeat"] == "halted"
    LoopState(last_tick_ts=T0 - 60).save(tmp_path / "s.json")
    assert dash.snapshot(now=T0)["heartbeat"] == "quiet"
    LoopState(last_tick_ts=T0 - 600).save(tmp_path / "s.json")
    snap = dash.snapshot(now=T0)
    assert snap["heartbeat"] == "stale"
    assert (
        snap["alerts"][-1]["source"] == "dashboard" and "no heartbeat" in snap["alerts"][-1]["text"]
    )
    LoopState(last_tick_ts=T0 - 600, stopped="interrupted").save(tmp_path / "s.json")
    assert dash.snapshot(now=T0)["heartbeat"] == "stopped"
    assert dash.pause_file == tmp_path / "PAUSE"  # defaults beside the stop file


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
    args = parser.parse_args(["demo-trade", "--reconcile", "0", "--spot-source", "db"])
    assert args.reconcile == 0 and args.spot_source == "db" and args.entry == "maker"
    assert args.alerts == "state/alerts.jsonl" and args.pause_file == "state/PAUSE"
    cfg = cli._loop_config(args)
    assert cfg.reconcile_s == 0 and cfg.spot_source == "db" and cfg.alerts_path is not None
    assert cfg.max_entries == 2 and cfg.free_entries == 1 and not cfg.allow_flip
    assert cfg.min_hold_s == 60 and cfg.reentry_cooloff_s == 120 and cfg.spot_smooth_s == 10
    assert cfg.max_consecutive_losses == 3 and cfg.loss_pause_s == 1800
    args = parser.parse_args(["demo-trade", "--allow-flip", "--max-consecutive-losses", "0"])
    cfg = cli._loop_config(args)
    assert cfg.allow_flip and cfg.max_consecutive_losses == 0


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
