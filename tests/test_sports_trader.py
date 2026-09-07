import json
import sqlite3

import pytest
from sports_fixtures import NOW, START_TS, market, rules

from kalshi_bot.alerts import AlertLog
from kalshi_bot.client import DryRunOrder, KalshiAPIError
from kalshi_bot.fees import order_fee
from kalshi_bot.models import Market, Orderbook, Position
from kalshi_sports import learn
from kalshi_sports.catalog import classify
from kalshi_sports.feeds.odds import OddsQuote
from kalshi_sports.risk import Intent, RiskEngine, RiskLimits
from kalshi_sports.storage import SportsDataStore
from kalshi_sports.strategy import ConsensusGapStrategy, Context, Params
from kalshi_sports.trader import SportsTrader, TraderConfig

EVENT = "KXMLBGAME-26SEP071910NYYBOS"
T_NYY = f"{EVENT}-NYY"
T_BOS = f"{EVENT}-BOS"


def ctx(**over):
    base = dict(
        ticker=T_NYY,
        league="mlb",
        side_team="NYY",
        yes_bid=0.40,
        yes_ask=0.42,
        no_bid=0.58,
        no_ask=0.60,
        quote_age_s=10.0,
        p_yes=0.50,
        n_books=5,
        sharp_books=1,
        dispersion=0.01,
        odds_age_s=60.0,
        secs_to_start=2 * 3600,
        event_exposure=0.0,
        start_exact=True,
    )
    base.update(over)
    return Context(**base)


# ---------------------------------------------------------------- strategy


def test_strategy_buys_the_side_with_the_larger_edge():
    s = ConsensusGapStrategy(Params(margin=0.03))
    d = s.evaluate(ctx())  # p_yes 0.50 vs ask 0.42: edge ~ +0.063 on YES
    assert d.action == "buy_yes" and d.price == 0.42 and d.p_side == 0.50
    assert d.edge == pytest.approx(0.50 - 0.42 - 0.07 * 0.42 * 0.58)
    d2 = s.evaluate(ctx(p_yes=0.30))  # NO is worth 0.70 against a 0.60 ask
    assert d2.action == "buy_no" and d2.price == 0.60 and d2.p_side == pytest.approx(0.70)


def test_strategy_skip_reasons_in_order():
    s = ConsensusGapStrategy(Params(margin=0.03))
    assert s.evaluate(ctx(secs_to_start=None)).reason == "start time unknown"
    assert s.evaluate(ctx(start_exact=False)).reason == "start time approximate"
    assert s.evaluate(ctx(secs_to_start=600)).reason == "inside no-entry window"
    assert s.evaluate(ctx(secs_to_start=3 * 86400)).reason == "too early"
    assert s.evaluate(ctx(event_exposure=5.0)).reason == "already positioned on this game"
    assert s.evaluate(ctx(quote_age_s=999)).reason == "kalshi quote stale"
    assert s.evaluate(ctx(p_yes=None)).reason == "no consensus"
    assert s.evaluate(ctx(odds_age_s=5000)).reason == "odds stale"
    assert s.evaluate(ctx(n_books=1)).reason == "too few books"
    assert s.evaluate(ctx(sharp_books=0)).reason == "no sharp book"
    assert s.evaluate(ctx(dispersion=0.2)).reason == "books disagree"
    assert s.evaluate(ctx(yes_bid=0.30)).reason == "spread too wide"
    assert s.evaluate(ctx(p_yes=0.45)).reason == "edge below margin"
    assert s.evaluate(ctx(yes_ask=0.10, yes_bid=0.08, p_yes=0.30)).reason == "price outside range"


def test_params_roundtrip_and_adjustments(tmp_path):
    p = Params(margin=0.03)
    path = tmp_path / "params.json"
    p.save(path)
    assert Params.load(path) == p
    assert Params.load(tmp_path / "missing.json") == Params()
    t = p.tightened(note="bad")
    assert t.margin == pytest.approx(0.04) and t.size_scale == 0.5 and t.version == 2
    lo = t.loosened(max_scale=2.0, note="good")
    assert lo.size_scale == pytest.approx(0.625) and lo.margin == pytest.approx(0.04)
    assert Params(margin=0.10).tightened(note="x").margin == 0.10  # capped


# ---------------------------------------------------------------- risk


def test_risk_engine_limits_and_persistence(tmp_path):
    store = SportsDataStore()
    lim = RiskLimits(
        daily_loss_cap=10,
        total_loss_cap=20,
        max_trade_dollars=6,
        max_event_dollars=6,
        max_open_positions=2,
        max_open_dollars=10,
        max_consecutive_losses=2,
        loss_pause_s=3600,
        stop_file=str(tmp_path / "STOP"),
        pause_file=str(tmp_path / "PAUSE"),
    )
    risk = RiskEngine(store, lim, "paper")
    intent = Intent(T_NYY, EVENT, 5.0, "paper")
    assert risk.check(intent, NOW, bankroll=200.0) is None
    assert (
        risk.check(Intent(T_NYY, EVENT, 7.0, "paper"), NOW, 200.0) == "trade exceeds per-trade cap"
    )
    assert risk.check(intent, NOW, bankroll=50.0) == "trade exceeds bankroll share"
    # game exposure counts open positions on the same event
    store.open_position(
        {
            "mode": "paper",
            "ticker": T_BOS,
            "event_ticker": EVENT,
            "league": "mlb",
            "side": "yes",
            "side_team": "BOS",
            "contracts": 5,
            "price": 0.5,
            "fee": 0.09,
            "dollars": 2.59,
            "order_id": "paper",
            "opened_ts": NOW,
            "start_ts": START_TS,
            "p_entry": 0.55,
            "edge_entry": 0.04,
        }
    )
    assert risk.check(intent, NOW, 200.0) == "game exposure cap reached"
    other = Intent("X", "OTHER", 5.0, "paper")
    assert risk.check(other, NOW, 200.0) is None
    # results roll into daily and total counters and trip the breaker
    assert risk.record_result(-4.0, NOW) is None
    note = risk.record_result(-7.0, NOW + 1)
    assert note and "consecutive losses" in note
    assert risk.check(other, NOW + 2, 200.0) == "loss breaker active"
    assert risk.check(other, NOW + 4000, 200.0) == "daily loss cap reached"
    # a new Eastern day resets the daily counter but not the total
    tomorrow = NOW + 86400
    assert risk.daily_net(tomorrow) == 0.0 and risk.total_net() == -11.0
    assert risk.check(other, tomorrow, 200.0) is None
    # -11 so far; another -9.5 crosses the -20 total while the day stays above its -10 cap
    risk.record_result(-9.5, tomorrow)
    assert risk.check(other, tomorrow + 1, 200.0) == "total loss cap reached"
    # kill switches
    risk.reset_totals()
    (tmp_path / "PAUSE").write_text("")
    assert risk.check(other, tomorrow + 2, 200.0) == "paused"
    (tmp_path / "STOP").write_text("")
    assert risk.check(other, tomorrow + 2, 200.0) == "stop file present"
    assert "PAUSED" in risk.describe(tomorrow + 2)


# ---------------------------------------------------------------- trader

CONS_NOW = START_TS - 2 * 3600  # two hours before first pitch


def odds_rows(ts, home_dec=1.60, away_dec=2.50, book="pinnacle"):
    """Boston (home) favourite at 1.60, Yankees (away) 2.50: p_NYY ~ 0.39, p_BOS ~ 0.61."""
    out = []
    for b in (book, "draftkings", "fanduel", "betmgm"):
        for name, dec in (("Boston Red Sox", home_dec), ("New York Yankees", away_dec)):
            out.append(
                OddsQuote(
                    ts,
                    "test",
                    "mlb",
                    "g1",
                    START_TS,
                    "Boston Red Sox",
                    "New York Yankees",
                    b,
                    "h2h",
                    name,
                    None,
                    dec,
                    ts,
                ).row()
            )
    return out


def seed_game(store, *, yes_ask_nyy=0.30, yes_bid_nyy=0.28, odds_ts=None, quote_ts=None):
    """Kalshi prices the Yankees at 0.28/0.30 while the books say ~0.39: a YES edge."""
    q_ts = CONS_NOW - 5 if quote_ts is None else quote_ts
    for ticker, sub, bid, ask in (
        (T_NYY, "New York Y", yes_bid_nyy, yes_ask_nyy),
        (T_BOS, "Boston", round(1 - yes_ask_nyy, 2), round(1 - yes_bid_nyy, 2)),
    ):
        m = market(
            ticker,
            "KXMLBGAME",
            f"{sub} wins",
            EVENT,
            rules_text=rules("New York Y", "Boston"),
            sub=sub,
            bid=f"{bid:.2f}",
            ask=f"{ask:.2f}",
        )
        gm = classify(m)
        store.upsert_event(gm, q_ts, None)
        store.upsert_market(m, gm, q_ts)
        store.insert_snapshot(q_ts, m, None, gm.start_ts)
    store.insert_odds(odds_rows(CONS_NOW - 60 if odds_ts is None else odds_ts))


class FakeClient:
    def __init__(self, *, dry_run=False, fill_all=True, fail_order=False):
        self.dry_run = dry_run
        self.fill_all = fill_all
        self.fail_order = fail_order
        self.orders = []
        self.book = {"yes_dollars": [["0.28", "50"]], "no_dollars": [["0.70", "40"]]}
        self.positions = []
        self.balance = None
        self.settled = {}

    def get_orderbook(self, ticker, depth):
        book = self.book
        if ticker == T_BOS:  # the other side of the same game: mirror the YES/NO bids
            book = {"yes_dollars": self.book["no_dollars"], "no_dollars": self.book["yes_dollars"]}
        return Orderbook.from_dict(ticker, {"orderbook_fp": book})

    def get_balance(self):
        from kalshi_bot.models import Balance

        return Balance.from_dict(
            {
                "balance": 15000,
                "balance_breakdown": [
                    {"exchange_index": 3, "balance": 12000},
                    {"exchange_index": 0, "balance": 3000},
                ],
            }
        )

    def get_positions(self, settlement_status=None):
        return self.positions

    def get_market(self, ticker):
        if ticker in self.settled:
            return self.settled[ticker]
        raise KalshiAPIError(404, "GET", ticker, "nope")

    def create_order(self, ticker, *, side, action, count, price, time_in_force, client_order_id):
        self.orders.append((ticker, side, action, count, price, time_in_force, client_order_id))
        if self.fail_order:
            raise KalshiAPIError(400, "POST", "/portfolio/events/orders", "insufficient_balance")
        if self.dry_run:
            return DryRunOrder({"ticker": ticker})
        filled = count if self.fill_all else count - 1
        from kalshi_bot.models import Order

        return Order.from_dict(
            {
                "order_id": "ord-1",
                "ticker": ticker,
                "side": side,
                "action": action,
                "type": "limit",
                "status": "executed",
                "count": count,
                "remaining_count": count - filled,
            }
        )


class FakeOdds:
    source = "test"

    def __init__(self, ts):
        self.ts = ts
        self.calls = 0

    def fetch(self, league):
        self.calls += 1
        return [OddsQuote(*r) for r in odds_rows(self.ts)]

    def close(self):
        pass


def make_trader(store, tmp_path, *, mode="paper", client=None, odds=None, **cfg_over):
    defaults = dict(
        mode=mode,
        leagues=("mlb",),
        dollars=5.0,
        max_dollars=10.0,
        params_path=str(tmp_path / "params.json"),
        status_path=str(tmp_path / "status.json"),
        alerts_path=str(tmp_path / "alerts.jsonl"),
        self_learn=False,
    )
    cfg = TraderConfig(**{**defaults, **cfg_over})
    lim = RiskLimits(stop_file=str(tmp_path / "STOP"), pause_file=str(tmp_path / "PAUSE"))
    risk = RiskEngine(store, lim, mode)
    client = client or FakeClient()
    return SportsTrader(
        client, store, cfg, risk, odds=odds, alerts=AlertLog(cfg.alerts_path)
    ), client


def test_paper_tick_opens_a_position_and_logs_the_decision(tmp_path):
    store = SportsDataStore()
    seed_game(store)
    trader, client = make_trader(store, tmp_path)
    res = trader.tick(CONS_NOW)
    assert res.candidates == 2 and res.entries == 1 and not res.errors
    pos = store.positions(status="open", mode="paper")
    assert len(pos) == 1
    p = pos[0]
    # live book: YES ask is 1 - best NO bid = 0.30; $5 buys 16 contracts
    assert p["ticker"] == T_NYY and p["side"] == "yes" and p["price"] == pytest.approx(0.30)
    assert p["contracts"] == 16 and p["fee"] == order_fee(0.30, 16)
    assert p["p_entry"] == pytest.approx(0.3902, abs=1e-3) and p["start_ts"] == START_TS
    decisions = store._conn.execute("SELECT action, reason, ticker FROM decisions").fetchall()
    actions = {(d["ticker"], d["action"]) for d in decisions}
    assert (T_NYY, "buy_yes") in actions
    # the Boston market is the same game: skipped for exposure (or edge) and logged once
    assert any(d["ticker"] == T_BOS and d["action"] == "skip" for d in decisions)
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["open"] == 1 and status["mode"] == "paper"
    # second tick: no new entry; the NYY market now skips for exposure (a new reason, logged
    # once), and a third tick adds nothing because the same reasons are suppressed for an hour
    n_before = len(decisions)
    res2 = trader.tick(CONS_NOW + 30)
    assert res2.entries == 0
    n_after = len(store._conn.execute("SELECT id FROM decisions").fetchall())
    assert n_after == n_before + 1
    trader.tick(CONS_NOW + 60)
    assert len(store._conn.execute("SELECT id FROM decisions").fetchall()) == n_after


def test_stale_odds_trigger_a_confirming_pull_then_trade(tmp_path):
    store = SportsDataStore()
    seed_game(store, odds_ts=CONS_NOW - 3000)  # older than max_odds_age (900 s)
    odds = FakeOdds(CONS_NOW)
    trader, client = make_trader(store, tmp_path, odds=odds)
    res = trader.tick(CONS_NOW)
    assert odds.calls == 1 and res.odds_pulls == 1 and res.entries == 1
    # without a feed the stale odds are a skip, not a trade
    store2 = SportsDataStore()
    seed_game(store2, odds_ts=CONS_NOW - 3000)
    trader2, _ = make_trader(store2, tmp_path)
    res2 = trader2.tick(CONS_NOW)
    assert res2.entries == 0
    reasons = {r["reason"] for r in store2._conn.execute("SELECT reason FROM decisions")}
    assert "odds stale" in reasons


def test_live_book_can_veto_the_prescreen(tmp_path):
    store = SportsDataStore()
    seed_game(store)
    client = FakeClient()
    client.book = {"yes_dollars": [["0.36", "50"]], "no_dollars": [["0.61", "40"]]}  # ask 0.39
    trader, _ = make_trader(store, tmp_path, client=client)
    res = trader.tick(CONS_NOW)
    assert res.entries == 0
    reasons = {r["reason"] for r in store._conn.execute("SELECT reason FROM decisions")}
    assert "edge below margin" in reasons


def test_live_mode_places_ioc_order_and_records_partial_fill(tmp_path):
    store = SportsDataStore()
    seed_game(store)
    client = FakeClient(fill_all=False)
    trader, _ = make_trader(store, tmp_path, mode="live", client=client)
    res = trader.tick(CONS_NOW)
    assert res.entries == 1 and len(client.orders) == 1
    ticker, side, action, count, price, tif, coid = client.orders[0]
    assert (ticker, side, action, tif) == (T_NYY, "yes", "buy", "immediate_or_cancel")
    assert price == pytest.approx(0.30) and len(coid) == 32
    # bankroll-share cap: 5% of the $120 shard-3 balance is $6, so about 20 contracts max,
    # but the stake is $5 -> 16 requested, 15 filled
    pos = store.positions(status="open", mode="live")[0]
    assert pos["contracts"] == count - 1 and pos["order_id"] == "ord-1"
    assert pos["fee"] == order_fee(0.30, count - 1)


def test_dry_run_client_in_live_mode_simulates_and_tags_dryrun(tmp_path):
    store = SportsDataStore()
    seed_game(store)
    trader, client = make_trader(store, tmp_path, mode="live", client=FakeClient(dry_run=True))
    res = trader.tick(CONS_NOW)
    assert res.entries == 1
    assert store.positions(mode="dryrun")[0]["order_id"] == "dryrun"
    assert store.positions(mode="live") == []


def test_order_failure_is_an_error_not_a_position(tmp_path):
    store = SportsDataStore()
    seed_game(store)
    trader, client = make_trader(store, tmp_path, mode="live", client=FakeClient(fail_order=True))
    res = trader.tick(CONS_NOW)
    assert res.entries == 0 and any("order" in e for e in res.errors)
    assert store.positions(status="open", mode="live") == []
    alerts = (tmp_path / "alerts.jsonl").read_text()
    assert "order failed" in alerts


def test_risk_refusal_is_logged_as_skip(tmp_path):
    store = SportsDataStore()
    seed_game(store)
    trader, _ = make_trader(store, tmp_path)
    (tmp_path / "PAUSE").write_text("")
    res = trader.tick(CONS_NOW)
    assert res.entries == 0 and res.candidates == 0  # paused: no scan at all
    (tmp_path / "PAUSE").unlink()
    trader.risk.limits = RiskLimits(
        max_open_dollars=1.0, stop_file=str(tmp_path / "STOP"), pause_file=str(tmp_path / "PAUSE")
    )
    res = trader.tick(CONS_NOW + 1)
    assert res.entries == 0
    reasons = {r["reason"] for r in store._conn.execute("SELECT reason FROM decisions")}
    assert "risk: open exposure cap reached" in reasons


def test_settlement_clv_and_breaker(tmp_path):
    store = SportsDataStore()
    seed_game(store)
    trader, client = make_trader(store, tmp_path)
    trader.tick(CONS_NOW)
    pos = store.positions(status="open", mode="paper")[0]
    # a snapshot just before the start gives the Kalshi close; odds at start give consensus close
    m = market(
        T_NYY,
        "KXMLBGAME",
        "New York Y wins",
        EVENT,
        rules_text=rules("New York Y", "Boston"),
        sub="New York Y",
        bid="0.34",
        ask="0.36",
    )
    store.insert_snapshot(START_TS - 60, m, None, START_TS)
    store.insert_odds(
        odds_rows(START_TS - 30, home_dec=1.70, away_dec=2.30)
    )  # NYY drifted to ~0.42
    res = trader.tick(START_TS + 10)
    assert res.marked == 1
    row = store.positions(mode="paper")[0]
    assert row["kalshi_close"] == pytest.approx(0.35) and row["clv_kalshi"] == pytest.approx(0.05)
    assert row["consensus_close"] == pytest.approx(0.425, abs=1e-3)
    assert row["clv_consensus"] == pytest.approx(row["consensus_close"] - row["p_entry"])
    # the recorder writes the result; the trader books it
    settled = Market.from_dict(
        {
            "ticker": T_NYY,
            "series_ticker": "KXMLBGAME",
            "event_ticker": EVENT,
            "title": "New York Y wins",
            "status": "settled",
            "result": "no",
            "close_time": "2026-09-10T23:10:00Z",
        }
    )
    store.mark_settled(settled, START_TS + 4 * 3600)
    res = trader.tick(START_TS + 4 * 3600)
    assert res.settled == 1
    row = store.positions(mode="paper")[0]
    assert row["status"] == "settled" and row["result"] == "no"
    assert row["net"] == pytest.approx(-(pos["contracts"] * pos["price"]) - pos["fee"], abs=0.01)
    assert trader.risk.total_net() == pytest.approx(row["net"])
    assert trader.risk.loss_streak() == 1
    summary = store.trading_summary("paper")
    assert summary["settled"] == 1 and summary["net"] == pytest.approx(row["net"])


def test_settlement_falls_back_to_exchange_after_grace(tmp_path):
    store = SportsDataStore()
    seed_game(store)
    trader, client = make_trader(store, tmp_path)
    trader.tick(CONS_NOW)
    client.settled[T_NYY] = Market.from_dict(
        {
            "ticker": T_NYY,
            "series_ticker": "KXMLBGAME",
            "title": "x",
            "status": "settled",
            "result": "yes",
        }
    )
    res = trader.tick(START_TS + 5 * 3600)
    assert res.settled == 1
    row = store.positions(mode="paper")[0]
    assert row["result"] == "yes" and row["net"] > 0


def test_reconcile_adopts_exchange_positions_in_live_mode(tmp_path):
    store = SportsDataStore()
    client = FakeClient()
    client.positions = [
        Position.from_dict(
            {
                "ticker": T_BOS,
                "event_ticker": EVENT,
                "position": 4,
                "total_traded": 240,
                "fees_paid": 5,
            }
        )
    ]
    trader, _ = make_trader(store, tmp_path, mode="live", client=client)
    notes = trader.reconcile(NOW)
    assert any("adopted" in n for n in notes)
    row = store.positions(status="open", mode="live")[0]
    assert row["ticker"] == T_BOS and row["contracts"] == 4 and row["price"] == pytest.approx(0.60)
    assert row["order_id"] == "adopted"
    # paper mode never touches the exchange
    trader_p, _ = make_trader(SportsDataStore(), tmp_path, mode="paper", client=client)
    assert trader_p.reconcile(NOW) == []


def test_stop_file_ends_the_run(tmp_path):
    store = SportsDataStore()
    seed_game(store)
    trader, _ = make_trader(store, tmp_path, interval=0.01)
    (tmp_path / "STOP").write_text("")
    assert trader.run(max_ticks=3, install_signals=False) == "stop file"


# ---------------------------------------------------------------- learning


def settled_rows(store, n, *, clv, net_each, mode="paper", league="mlb"):
    # later batches open after earlier ones, so "the last 20" means the latest batch
    offset = len(store.positions(mode=mode))
    for j in range(n):
        i = offset + j
        pid = store.open_position(
            {
                "mode": mode,
                "ticker": f"T{i}",
                "event_ticker": f"E{i}",
                "league": league,
                "side": "yes",
                "side_team": "X",
                "contracts": 10,
                "price": 0.45,
                "fee": 0.18,
                "dollars": 4.68,
                "order_id": "paper",
                "opened_ts": NOW + i,
                "start_ts": NOW + i + 7200,
                "p_entry": 0.52,
                "edge_entry": 0.05,
            }
        )
        store.set_close_marks(pid, 0.45 + clv, 0.52 + clv)
        store.settle_position(
            pid,
            status="settled",
            result="yes" if net_each > 0 else "no",
            net=net_each,
            now=NOW + i + 20000,
        )


def test_learn_holds_with_few_results_then_tightens_on_negative_clv(tmp_path):
    store = SportsDataStore()
    settled_rows(store, 10, clv=-0.03, net_each=-1.0)
    rv = learn.review(store, "paper")
    assert rv.n == 10 and rv.net == -10.0
    new, why = learn.propose(Params(), rv, max_scale=2.0)
    assert new is None and why.startswith("hold")
    settled_rows(store, 25, clv=-0.03, net_each=-1.0)
    rv = learn.review(store, "paper")
    new, why = learn.propose(Params(), rv, max_scale=2.0)
    assert new is not None and new.margin == pytest.approx(0.04) and new.size_scale == 0.5
    assert why.startswith("tighten")
    text = learn.format_review(rv)
    assert "CLV vs consensus" in text and "by price" in text
    note = learn.run_cycle(store, tmp_path / "p.json", "paper", now=NOW, max_scale=2.0)
    assert note and "v2" in note and Params.load(tmp_path / "p.json").version == 2


def test_learn_loosens_only_past_the_gate_and_detects_drift(tmp_path):
    store = SportsDataStore()
    settled_rows(store, 60, clv=+0.02, net_each=+0.8)
    rv = learn.review(store, "paper")
    new, why = learn.propose(Params(), rv, max_scale=2.0)
    assert new is not None and new.size_scale == pytest.approx(1.25) and why.startswith("loosen")
    assert learn.propose(Params(size_scale=2.0), rv, max_scale=2.0)[0] is None  # at the ceiling
    # a losing run of 20 with mostly losses halves size without touching the margin
    settled_rows(store, 20, clv=+0.02, net_each=-1.5)
    rv = learn.review(store, "paper")
    new, why = learn.propose(Params(margin=0.03, size_scale=1.25), rv, max_scale=2.0)
    assert new is not None and why.startswith("drift")
    assert new.margin == pytest.approx(0.03) and new.size_scale == pytest.approx(0.625)


def test_trader_runs_learning_cycle_when_due(tmp_path):
    store = SportsDataStore()
    settled_rows(store, 40, clv=-0.05, net_each=-1.0)
    trader, _ = make_trader(store, tmp_path, self_learn=True, learn_every_s=10)
    trader.tick(NOW)
    assert Params.load(tmp_path / "params.json").version == 2
    assert trader.strategy.params.version == 1  # picked up on the next tick
    trader.tick(NOW + 1)
    assert trader.strategy.params.version == 2 and trader.strategy.params.size_scale == 0.5


def test_cli_positions_and_learn_offline(tmp_path, capsys, monkeypatch):
    from kalshi_sports import cli

    monkeypatch.setenv("KALSHI_ENV", "demo")
    db = tmp_path / "s.sqlite"
    with SportsDataStore(db) as store:
        settled_rows(store, 3, clv=0.01, net_each=1.0)
    assert cli.main(["positions", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "== paper" in out and "settled" in out
    assert (
        cli.main(
            ["learn", "--db", str(db), "--mode", "paper", "--params", str(tmp_path / "p.json")]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "settled 3" in out and "hold" in out
    # live-trade refuses without production, dry-run off, credentials and --real-money
    with pytest.raises(SystemExit):
        cli.main(["--env", "demo", "live-trade", "--db", str(db)])
    monkeypatch.setenv("KALSHI_DRY_RUN", "true")
    with pytest.raises(SystemExit):
        cli.main(["live-trade", "--db", str(db), "--real-money"])


def test_sqlite_row_factory_on_store():
    store = SportsDataStore()
    assert isinstance(store._conn.execute("SELECT 1 AS x").fetchone(), sqlite3.Row)
