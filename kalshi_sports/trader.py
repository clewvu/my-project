"""The sports trading loop: consensus-gap strategy on Kalshi moneylines, paper or live.

Each tick:
  1. reload strategy parameters if the learner rewrote them
  2. book settlements for open positions (result from the recorder's table, or from
     the exchange once the game is well past its start)
  3. mark closing prices at game start for closing-line value
  4. unless paused: scan candidate markets from the recorder's data, confirm the
     consensus with a fresh odds pull when a candidate passes the pre-screen,
     fetch the live Kalshi book, decide, size, pass the risk engine, execute
  5. every ``learn_every_s``: run the learning cycle (see learn.py)
  6. write a status JSON for a glance

The recorder must be running against the same database: the trader reads its
markets, events, snapshots and odds and adds odds pulls of its own only to
confirm a signal. Nothing is placed on production unless the client was built
with ``allow_live=True`` by ``kalshi-sports live-trade`` after its gates.
"""

from __future__ import annotations

import hashlib
import json
import logging
import signal
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from kalshi_bot.alerts import AlertLog
from kalshi_bot.client import DryRunOrder, KalshiClient, KalshiError
from kalshi_bot.fees import order_fee
from kalshi_bot.sizing import kelly_dollars

from . import leagues
from .compare import _side_prob
from .consensus import Consensus, consensus_for_game, recent_games
from .feeds.odds import OddsFeed
from .matching import GameRef, match_game
from .risk import Intent, RiskEngine
from .storage import SportsDataStore
from .strategy import ConsensusGapStrategy, Context, Decision, Params

log = logging.getLogger(__name__)

DEAD_STATUSES = {"closed", "settled", "finalized", "determined"}


@dataclass
class TraderConfig:
    mode: str  # paper | live
    leagues: tuple[str, ...]
    dollars: float = 5.0  # base stake per trade
    max_dollars: float = 10.0  # ceiling after learned scaling
    paper_bankroll: float = 200.0
    interval: float = 30.0
    params_path: str = "state/sports_params.json"
    status_path: str = "state/sports_trader.json"
    alerts_path: str | None = "state/sports_alerts.jsonl"  # the crypto loop writes alerts.jsonl
    confirm_age_s: float = 600.0  # refresh odds before trading if older than this
    odds_min_gap_s: float = 300.0  # never pull a league's odds more often than this
    settle_grace_s: float = 4 * 3600  # after start, ask the exchange for a result
    balance_every_s: float = 300.0
    learn_every_s: float = 3600.0
    self_learn: bool = True
    skip_log_every_s: float = 3600.0  # log the same skip reason per market at most hourly


@dataclass
class TickResult:
    candidates: int = 0
    entries: int = 0
    skips: int = 0
    settled: int = 0
    marked: int = 0
    odds_pulls: int = 0
    quote_refreshes: int = 0
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        self.errors = self.errors or []


class SportsTrader:
    def __init__(
        self,
        client: KalshiClient,
        store: SportsDataStore,
        cfg: TraderConfig,
        risk: RiskEngine,
        *,
        odds: OddsFeed | None = None,
        alerts: AlertLog | None = None,
        strategy: ConsensusGapStrategy | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.cfg = cfg
        # Positions, decisions and risk counters are tagged with this. A "live" run whose
        # client is in dry-run mode sends nothing, so its simulated fills are kept apart
        # under "dryrun" and never mix with real money.
        self.pos_mode = (
            "dryrun" if cfg.mode == "live" and getattr(client, "dry_run", False) else cfg.mode
        )
        self.risk = risk
        if risk.mode != self.pos_mode:
            risk.rekey(self.pos_mode)
        self.odds = odds
        self.alerts = alerts or AlertLog(cfg.alerts_path)
        self.strategy = strategy or ConsensusGapStrategy(Params.load(cfg.params_path))
        self._params_mtime: float | None = None
        self._last_odds: dict[str, float] = {}
        self._last_balance = 0.0
        self._balance: dict[int, float] = {}  # by exchange shard; -1 = total
        self._last_learn = 0.0
        self._skip_seen: dict[tuple[str, str], float] = {}
        self._stop = threading.Event()
        self.last_reason = ""

    @property
    def conn(self) -> sqlite3.Connection:
        return self.store._conn

    # ------------------------------------------------------------ params / status

    def _load_params(self) -> None:
        p = Path(self.cfg.params_path)
        if not p.exists():
            return
        mtime = p.stat().st_mtime
        if mtime != self._params_mtime:
            self.strategy.params = Params.load(p)
            self._params_mtime = mtime
            log.info(
                "params v%d: margin=%.3f size_scale=%.2f (%s)",
                self.strategy.params.version,
                self.strategy.params.margin,
                self.strategy.params.size_scale,
                self.strategy.params.note,
            )

    def _write_status(self, now: float, res: TickResult) -> None:
        try:
            summary = self.store.trading_summary(self.pos_mode)
            summary.update(
                {
                    "mode": self.pos_mode,
                    "ts": now,
                    "risk": self.risk.describe(now),
                    "params": asdict(self.strategy.params),
                    "last_tick": {k: v for k, v in asdict(res).items() if k != "errors"},
                    "errors": (res.errors or [])[-5:],
                    "balance": self._balance,
                }
            )
            p = Path(self.cfg.status_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(summary, indent=1, default=str))
            tmp.replace(p)
        except OSError as exc:  # pragma: no cover
            log.warning("status write failed: %s", exc)

    # ------------------------------------------------------------ bankroll

    def bankroll(self, now: float, shard: int | None) -> float | None:
        if self.cfg.mode != "live":
            return self.cfg.paper_bankroll + self.risk.total_net()
        if now - self._last_balance >= self.cfg.balance_every_s or not self._balance:
            try:
                bal = self.client.get_balance()
                self._balance = {-1: bal.balance, **{int(k): v for k, v in bal.breakdown.items()}}
                self._last_balance = now
            except KalshiError as exc:
                log.warning("balance failed: %s", exc)
        if shard is not None and shard in self._balance:
            return self._balance[shard]
        return self._balance.get(-1)

    # ------------------------------------------------------------ startup

    def _sports_league(self, ticker: str) -> str | None:
        """League key if the ticker is one of the configured sports series, else None."""
        lg = leagues.league_for_series(ticker.split("-", 1)[0])
        return lg.key if lg is not None and lg.key in self.cfg.leagues else None

    def reconcile(self, now: float) -> list[str]:
        """Compare open positions with the exchange (live only). Returns warnings."""
        if self.cfg.mode != "live":
            return []
        notes: list[str] = []
        try:
            exchange = {
                p.ticker: p
                for p in self.client.get_positions(settlement_status="unsettled")
                if p.position
            }
        except KalshiError as exc:
            notes.append(f"reconcile: positions call failed: {exc}")
            return notes
        ours = {r["ticker"]: r for r in self.store.positions(status="open", mode="live")}
        # Positions adopted from the exchange in the past that turn out not to be sports
        # markets (the crypto loop's, for instance) are voided, not counted as exposure.
        for ticker, row in list(ours.items()):
            if row["order_id"] == "adopted" and self._sports_league(ticker) is None:
                self.store.settle_position(row["id"], status="void", result=None, net=0.0, now=now)
                notes.append(f"{ticker}: not a sports market; dropped the adopted row")
                del ours[ticker]
        for ticker, p in exchange.items():
            if ticker in ours:
                continue
            league = self._sports_league(ticker)
            if league is None:
                continue  # another loop's position (crypto); not ours to track
            side = p.side or "yes"
            contracts = int(abs(p.position))
            price = abs(p.total_cost) / contracts if contracts else 0.0
            self.store.open_position(
                {
                    "mode": "live",
                    "ticker": ticker,
                    "event_ticker": p.event_ticker,
                    "league": league,
                    "side": side,
                    "side_team": None,
                    "contracts": contracts,
                    "price": round(price, 4),
                    "fee": p.fees_paid or 0.0,
                    "dollars": abs(p.total_cost) + (p.fees_paid or 0.0),
                    "order_id": "adopted",
                    "opened_ts": now,
                    "start_ts": None,
                    "p_entry": None,
                    "edge_entry": None,
                }
            )
            notes.append(f"adopted exchange position {ticker} {side} x{contracts}")
        for ticker in ours:
            if ticker not in exchange:
                result, _status = self.store.market_result(ticker)
                if result is None:
                    notes.append(f"{ticker}: open here but not on the exchange")
        for n in notes:
            self.alerts.record("warn", "sports-trader", n, now)
        return notes

    # ------------------------------------------------------------ settlement / CLV

    def _book_settlements(self, now: float, res: TickResult) -> None:
        for row in self.store.positions(status="open", mode=self.pos_mode):
            result, status = self.store.market_result(row["ticker"])
            past_grace = row["start_ts"] and now >= row["start_ts"] + self.cfg.settle_grace_s
            if result is None and past_grace:
                try:
                    m = self.client.get_market(row["ticker"])
                    result, status = m.result, m.status
                except KalshiError as exc:
                    res.errors.append(f"{row['ticker']}: settlement: {exc}")
                    continue
            if not result:
                if status and status.lower() in DEAD_STATUSES:
                    # closed without a result: voided (postponement); fee assumed refunded
                    self.store.settle_position(
                        row["id"], status="void", result=None, net=0.0, now=now
                    )
                    self.alerts.record("info", "sports-trader", f"{row['ticker']} voided", now)
                    res.settled += 1
                continue
            won = result.lower() == row["side"]
            if won:
                gross = row["contracts"] * (1 - row["price"])
            else:
                gross = -row["contracts"] * row["price"]
            net = round(gross - row["fee"], 2)
            self.store.settle_position(row["id"], status="settled", result=result, net=net, now=now)
            note = self.risk.record_result(net, now)
            self.alerts.record(
                "info",
                "sports-trader",
                f"{row['ticker']} {row['side']} x{row['contracts']} settled {result}: {net:+.2f}",
                now,
                net=net,
            )
            if note:
                self.alerts.record("warn", "sports-trader", note, now)
            res.settled += 1

    def _mark_closes(self, now: float, res: TickResult) -> None:
        for row in self.store.positions(status="open", mode=self.pos_mode):
            if not row["start_ts"] or now < row["start_ts"] or row["kalshi_close"] is not None:
                continue
            snap = self.store.latest_snapshot(row["ticker"], before_ts=row["start_ts"])
            k_close = None
            if snap is not None and snap["yes_bid"] is not None and snap["yes_ask"] is not None:
                mid = (snap["yes_bid"] + snap["yes_ask"]) / 2
                k_close = mid if row["side"] == "yes" else 1 - mid
            c_close = None
            cons = self._consensus_for(row["league"], row["event_ticker"], at=row["start_ts"])
            if cons is not None:
                p_yes = _side_prob(cons, row["league"], row["side_team"], None)
                if p_yes is not None:
                    c_close = p_yes if row["side"] == "yes" else 1 - p_yes
            if k_close is None and c_close is None:
                continue
            self.store.set_close_marks(row["id"], k_close, c_close)
            res.marked += 1

    # ------------------------------------------------------------ consensus plumbing

    def _refs(self, league: str, at: float) -> list[GameRef]:
        return [
            GameRef(g["league"], g["game_id"], g["commence_ts"], g["home"] or "", g["away"] or "")
            for g in recent_games(self.conn, league, at - 3 * 3600)
        ]

    def _event_row(self, event_ticker: str | None) -> sqlite3.Row | None:
        if not event_ticker:
            return None
        return self.conn.execute(
            "SELECT * FROM events WHERE event_ticker = ?", (event_ticker,)
        ).fetchone()

    def _consensus_for(
        self, league: str | None, event_ticker: str | None, *, at: float
    ) -> Consensus | None:
        ev = self._event_row(event_ticker)
        if ev is None or not league:
            return None
        ref = match_game(
            league,
            ev["game_date"],
            ev["away_abbr"] or ev["away"],
            ev["home_abbr"] or ev["home"],
            self._refs(league, at),
            alt_away=ev["away"],
            alt_home=ev["home"],
        )
        if ref is None:
            return None
        cons = consensus_for_game(
            self.conn, league, ref.game_id, now=at, method=self.strategy.params.method
        )
        return next((c for c in cons if c.market == "h2h"), None)

    def _refresh_odds(self, league_key: str, now: float, res: TickResult) -> bool:
        if self.odds is None:
            return False
        if now - self._last_odds.get(league_key, 0.0) < self.cfg.odds_min_gap_s:
            return False
        self._last_odds[league_key] = now
        try:
            quotes = self.odds.fetch(leagues.LEAGUES[league_key])
        except Exception as exc:  # noqa: BLE001
            res.errors.append(f"odds {league_key}: {exc}")
            return False
        self.store.insert_odds([q.row() for q in quotes])
        res.odds_pulls += 1
        return True

    # ------------------------------------------------------------ the scan

    def _context(
        self, cand: sqlite3.Row, quotes: dict[str, Any], cons: Consensus | None, now: float
    ) -> Context:
        p_yes = None
        if cons is not None:
            p_yes = _side_prob(cons, cand["league"], cand["side_team"], cand["side_name"])
        return Context(
            ticker=cand["ticker"],
            league=cand["league"],
            side_team=cand["side_team"],
            yes_bid=quotes.get("yes_bid"),
            yes_ask=quotes.get("yes_ask"),
            no_bid=quotes.get("no_bid"),
            no_ask=quotes.get("no_ask"),
            quote_age_s=now - quotes["ts"],
            p_yes=p_yes,
            n_books=cons.n_books if cons else 0,
            sharp_books=cons.sharp_books if cons else 0,
            dispersion=cons.dispersion if cons else 0.0,
            odds_age_s=cons.age_s if cons else None,
            secs_to_start=(cand["start_ts"] - now) if cand["start_ts"] else None,
            event_exposure=self.store.event_exposure(cand["event_ticker"], self.pos_mode),
            start_exact=bool(cand["start_exact"]),
        )

    def _log_decision(
        self, cand: sqlite3.Row, ctx: Context, d: Decision, now: float, **extra: Any
    ) -> None:
        if not d.is_entry:
            key = (cand["ticker"], d.reason)
            last = self._skip_seen.get(key)
            if last is not None and now - last < self.cfg.skip_log_every_s:
                return
            self._skip_seen[key] = now
        self.store.insert_decision(
            {
                "ts": now,
                "mode": self.pos_mode,
                "ticker": cand["ticker"],
                "event_ticker": cand["event_ticker"],
                "league": cand["league"],
                "side_team": cand["side_team"],
                "action": d.action,
                "reason": d.reason,
                "yes_bid": ctx.yes_bid,
                "yes_ask": ctx.yes_ask,
                "no_ask": ctx.no_ask,
                "p_consensus": ctx.p_yes,
                "n_books": ctx.n_books,
                "sharp_books": ctx.sharp_books,
                "dispersion": ctx.dispersion,
                "odds_age_s": ctx.odds_age_s,
                "secs_to_start": ctx.secs_to_start,
                "edge": d.edge,
                "margin": self.strategy.params.margin,
                "raw": d.inputs,
                **extra,
            }
        )

    def _scan(self, now: float, res: TickResult) -> None:
        p = self.strategy.params
        cands = self.store.candidate_markets(
            now, min_lead_s=p.min_lead_s, max_lead_s=p.max_lead_s, leagues=self.cfg.leagues
        )
        res.candidates = len(cands)
        refreshed: set[str] = set()
        for cand in cands:
            league = cand["league"]
            quotes = {
                "yes_bid": cand["yes_bid"],
                "yes_ask": cand["yes_ask"],
                "no_bid": cand["no_bid"],
                "no_ask": cand["no_ask"],
                "ts": cand["quote_ts"],
            }
            cons = self._consensus_for(league, cand["event_ticker"], at=now)
            ctx = self._context(cand, quotes, cons, now)
            pre = self.strategy.evaluate(ctx)
            # a stale local quote alone should not discard a game we could trade: the
            # recorder snapshots each book slower than our freshness gate, so pull the
            # live book and re-judge (the same confirmation we do before executing).
            # Bounded to games that already have a consensus, so we never spend an API
            # call on a market we could not trade anyway.
            if not pre.is_entry and pre.reason == "kalshi quote stale" and cons is not None:
                try:
                    book = self.client.get_orderbook(cand["ticker"], depth=5)
                    quotes = {
                        "yes_bid": book.best_yes_bid,
                        "yes_ask": book.best_yes_ask,
                        "no_bid": book.best_no_bid,
                        "no_ask": book.best_no_ask,
                        "ts": now,
                    }
                    ctx = self._context(cand, quotes, cons, now)
                    pre = self.strategy.evaluate(ctx)
                    res.quote_refreshes += 1
                except KalshiError as exc:
                    res.errors.append(f"{cand['ticker']}: quote refresh: {exc}")
            # pull fresh odds when a signal needs confirming, when the odds we have are
            # stale, or when the recorder has none for this league at all
            wants_fresh = pre.is_entry or pre.reason in ("odds stale", "no consensus")
            if wants_fresh and league not in refreshed:
                if cons is None or cons.age_s > self.cfg.confirm_age_s:
                    if self._refresh_odds(league, now, res):
                        refreshed.add(league)
                        cons = self._consensus_for(league, cand["event_ticker"], at=now)
                        ctx = self._context(cand, quotes, cons, now)
                        pre = self.strategy.evaluate(ctx)
            if not pre.is_entry:
                self._log_decision(cand, ctx, pre, now)
                res.skips += 1
                continue
            # confirm on the live book before spending anything
            try:
                book = self.client.get_orderbook(cand["ticker"], depth=5)
            except KalshiError as exc:
                res.errors.append(f"{cand['ticker']}: orderbook: {exc}")
                continue
            live = {
                "yes_bid": book.best_yes_bid,
                "yes_ask": book.best_yes_ask,
                "no_bid": book.best_no_bid,
                "no_ask": book.best_no_ask,
                "ts": now,
            }
            ctx = self._context(cand, live, cons, now)
            d = self.strategy.evaluate(ctx)
            if not d.is_entry:
                self._log_decision(cand, ctx, d, now)
                res.skips += 1
                continue
            # the YES ask is the best NO bid and vice versa; its size caps a taker fill
            ask_size = book.depth("no", 1) if d.side == "yes" else book.depth("yes", 1)
            self._execute(cand, ctx, d, ask_size, now, res)

    # ------------------------------------------------------------ execution

    def _skip(
        self,
        cand: sqlite3.Row,
        ctx: Context,
        d: Decision,
        reason: str,
        now: float,
        res: TickResult,
        **extra: Any,
    ) -> None:
        self._log_decision(
            cand, ctx, Decision("skip", reason, d.edge, d.price, d.p_side, d.inputs), now, **extra
        )
        res.skips += 1

    def _execute(
        self,
        cand: sqlite3.Row,
        ctx: Context,
        d: Decision,
        ask_size: float,
        now: float,
        res: TickResult,
    ) -> None:
        p = self.strategy.params
        price = float(d.price)  # type: ignore[arg-type]
        side = str(d.side)
        bankroll = self.bankroll(now, cand["exchange_index"])
        dollars = min(self.cfg.dollars * p.size_scale, self.cfg.max_dollars)
        if bankroll is not None and d.p_side is not None:
            kelly = kelly_dollars(
                d.p_side, price, bankroll, base=self.cfg.dollars, max_dollars=self.cfg.max_dollars
            )
            dollars = min(dollars, kelly)
        contracts = int(dollars // price)
        if ask_size and ask_size < contracts:
            contracts = int(ask_size)
        if contracts < 1:
            self._skip(cand, ctx, d, "stake below one contract", now, res)
            return
        fee = order_fee(price, contracts)
        intent = Intent(
            cand["ticker"], cand["event_ticker"], contracts * price + fee, self.pos_mode
        )
        refusal = self.risk.check(intent, now, bankroll)
        if refusal:
            self._skip(cand, ctx, d, f"risk: {refusal}", now, res)
            return

        order_id, filled, mode = "paper", contracts, self.pos_mode
        if self.cfg.mode == "live":
            seed = f"{cand['ticker']}|{side}|{int(now // 60)}".encode()
            coid = hashlib.sha1(seed).hexdigest()[:32]
            try:
                order = self.client.create_order(
                    cand["ticker"],
                    side=side,
                    action="buy",
                    count=contracts,
                    price=price,
                    time_in_force="immediate_or_cancel",
                    client_order_id=coid,
                )
            except KalshiError as exc:
                res.errors.append(f"{cand['ticker']}: order: {exc}")
                self.alerts.record(
                    "warn", "sports-trader", f"order failed {cand['ticker']}: {exc}", now
                )
                return
            if isinstance(order, DryRunOrder):
                order_id, mode = "dryrun", "dryrun"
            else:
                order_id = order.order_id
                filled = int(round(order.count - order.remaining_count))
        if filled < 1:
            self._skip(cand, ctx, d, "no fill", now, res, order_id=order_id)
            return
        fee = order_fee(price, filled)
        dollars_spent = round(filled * price + fee, 2)
        pid = self.store.open_position(
            {
                "mode": mode,
                "ticker": cand["ticker"],
                "event_ticker": cand["event_ticker"],
                "league": cand["league"],
                "side": side,
                "side_team": cand["side_team"],
                "contracts": filled,
                "price": price,
                "fee": fee,
                "dollars": dollars_spent,
                "order_id": order_id,
                "opened_ts": now,
                "start_ts": cand["start_ts"],
                "p_entry": d.p_side,
                "edge_entry": d.edge,
            }
        )
        self._log_decision(
            cand, ctx, d, now, dollars=dollars_spent, contracts=filled, order_id=order_id
        )
        tag = "REAL MONEY " if mode == "live" else ""
        self.alerts.record(
            "info",
            "sports-trader",
            f"{tag}{cand['ticker']} buy {side} x{filled} @ {price:.2f} "
            f"(p={d.p_side:.3f}, edge {d.edge:+.3f}) #{pid}",
            now,
        )
        res.entries += 1

    # ------------------------------------------------------------ loop

    def tick(self, now: float | None = None) -> TickResult:
        now = time.time() if now is None else now
        res = TickResult()
        self._load_params()
        self._book_settlements(now, res)
        self._mark_closes(now, res)
        if self.risk.stop_requested():
            self.last_reason = "stop file"
            self._stop.set()
        elif not self.risk.paused():
            self._scan(now, res)
        if self.cfg.self_learn and now - self._last_learn >= self.cfg.learn_every_s:
            self._last_learn = now
            try:
                from .learn import run_cycle

                note = run_cycle(
                    self.store,
                    self.cfg.params_path,
                    self.pos_mode,
                    now=now,
                    max_scale=self.cfg.max_dollars / self.cfg.dollars,
                )
                if note:
                    self.alerts.record("info", "sports-learn", note, now)
            except Exception as exc:  # noqa: BLE001
                res.errors.append(f"learn: {exc}")
        self._write_status(now, res)
        return res

    def stop(self) -> None:
        self._stop.set()

    def run(self, *, max_ticks: int | None = None, install_signals: bool = True) -> str:
        if install_signals:
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: self.stop())
        now = time.time()
        for note in self.reconcile(now):
            log.warning(note)
        self.alerts.record(
            "info", "sports-trader", f"start {self.cfg.mode} on {','.join(self.cfg.leagues)}", now
        )
        ticks = 0
        while not self._stop.is_set():
            started = time.time()
            try:
                res = self.tick(started)
                log.info(
                    "tick cands=%d entries=%d skips=%d settled=%d marked=%d odds=%d err=%d | %s",
                    res.candidates,
                    res.entries,
                    res.skips,
                    res.settled,
                    res.marked,
                    res.odds_pulls,
                    len(res.errors or []),
                    self.risk.describe(started),
                )
            except Exception:  # noqa: BLE001
                log.exception("tick crashed")
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            self._stop.wait(max(0.0, self.cfg.interval - (time.time() - started)))
        self.alerts.record(
            "halt", "sports-trader", f"stopped: {self.last_reason or 'signal'}", time.time()
        )
        return self.last_reason or "stopped"
