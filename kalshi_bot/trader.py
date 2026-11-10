"""The trading loop. Paper by default; live only with every gate open.

Each tick, for every enabled series:
  1. find the open market, fetch its book
  2. read the latest spot and realized vol from the in-process feeds
  3. ask the strategy for a decision; log it with its features
  4. pass any intent through the risk engine
  5. execute: paper fill at the ask, or a live limit order at the ask
Settled markets are scored once a result is published; P&L feeds the risk
engine (daily cap, loss streak).
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
import uuid
from dataclasses import dataclass

from .client import DryRunOrder, KalshiClient, KalshiError
from .fees import order_fee
from .models import Market
from .pyth import PythStream
from .recorder import is_live
from .risk import OrderIntent, RiskEngine, RiskLimits
from .spot_ws import SpotWebSocket
from .storage import MarketDataStore
from .strategy import FairValueParams, FairValueStrategy, SpotHistory
from .universe import Universe

log = logging.getLogger(__name__)


@dataclass
class TickSummary:
    markets: int = 0
    orders: int = 0
    skips: int = 0
    rejected: int = 0
    settled: int = 0
    errors: int = 0


class Trader:
    def __init__(
        self,
        client: KalshiClient,
        store: MarketDataStore,
        universe: Universe,
        strategy: FairValueStrategy,
        limits: RiskLimits,
        *,
        mode: str = "paper",
        interval: float = 5.0,
        spot_ws: SpotWebSocket | None = None,
        pyth: PythStream | None = None,
    ) -> None:
        if mode == "live" and (client.dry_run or not client.allow_live):
            raise ValueError("live mode needs a client with dry_run=False and allow_live=True")
        self.client = client
        self.store = store
        self.universe = universe
        self.strategy = strategy
        self.risk = RiskEngine(store, limits, mode)
        self.mode = mode
        active = store.active_config(mode)
        self.config_id: int | None = None
        if active is not None:
            # entry logic only changes between runs, via a promoted learned config
            self.strategy.params = FairValueParams.from_dict(
                {**self.strategy.params.to_dict(), **active["params"]}
            )
            self.config_id = active["id"]
            log.info(
                "using learned config id=%s (%s): %s",
                active["id"],
                active["source"],
                active["params"],
            )
        self.interval = max(1.0, interval)
        self.spot_ws = spot_ws
        self.pyth = pyth
        self.history = SpotHistory()
        self._book_ts: dict[str, float] = {}
        self._take_next: set[str] = set()  # hybrid: tickers whose maker order expired unfilled
        self._stop = threading.Event()
        self._seed_history()

    # ------------------------------------------------------------ spot

    def _seed_history(self) -> None:
        """Warm the vol estimator from the recorder's spot table if it is in the same DB."""
        now = time.time()
        for asset in self.universe.enabled:
            for source, symbol in asset.spot_preference:
                rows = self.store.recent_spot(source, symbol, since=now - 3600)
                if rows:
                    self.history.seed(self._hist_key(source, symbol), rows)
                    break

    @staticmethod
    def _hist_key(source: str, symbol: str) -> str:
        return f"{source}:{symbol}"

    def _ingest_feeds(self) -> None:
        for feed, source in ((self.spot_ws, "coinbase_ws"), (self.pyth, "pyth")):
            if feed is None:
                continue
            for symbol, tick in list(feed.buffer.latest.items()):
                self.history.push(self._hist_key(source, symbol), tick.local_ts, tick.price)

    def spot_for(self, series: str) -> tuple[float | None, float | None, float | None, str]:
        """(price, ts, sigma, key) from the best available source for a series."""
        for source, symbol in self.universe.spot_preference(series):
            key = self._hist_key(source, symbol)
            latest = self.history.latest(key)
            if latest is None:
                continue
            ts, price = latest
            sigma = self.history.realized_vol(key, self.strategy.params.vol_window)
            return price, ts, sigma, key
        return None, None, None, ""

    # ------------------------------------------------------------ tick

    def tick(self, now: float | None = None) -> TickSummary:
        now = time.time() if now is None else now
        self._ingest_feeds()
        summary = TickSummary()
        self._manage_resting(now, summary)
        self._settle(now, summary)
        open_positions = self.store.positions(self.mode, "open") + self.store.positions(
            self.mode, "resting"
        )
        for asset in self.universe.enabled:
            try:
                markets = self.client.get_markets(
                    series_ticker=asset.series, status="open", max_pages=1
                )
            except KalshiError as exc:
                summary.errors += 1
                log.warning("%s: list markets failed: %s", asset.series, exc)
                continue
            live = [m for m in markets if is_live(m, now)]
            if not live:
                continue
            market = min(live, key=lambda m: m.close_time.timestamp() if m.close_time else 1e18)
            summary.markets += 1
            self._handle_market(asset.series, market, open_positions, now, summary)
        return summary

    def _handle_market(
        self,
        series: str,
        market: Market,
        open_positions: list[dict],
        now: float,
        summary: TickSummary,
    ) -> None:
        try:
            book = self.client.get_orderbook(market.ticker, depth=5)
        except KalshiError as exc:
            summary.errors += 1
            log.warning("%s: orderbook failed: %s", market.ticker, exc)
            return
        book_ts = time.time()
        spot, spot_ts, sigma, key = self.spot_for(series)
        force_taker = (
            self.strategy.params.exec_mode == "hybrid" and market.ticker in self._take_next
        )
        decision = self.strategy.decide(
            market, book, spot, spot_ts, sigma, now, force_taker=force_taker
        )
        secs = market.close_time.timestamp() - now if market.close_time else None
        row = {
            "ts": now,
            "mode": self.mode,
            "series": series,
            "ticker": market.ticker,
            "secs_to_close": secs,
            "spot": spot,
            "spot_age": (now - spot_ts) if spot_ts else None,
            "strike": market.strike,
            "sigma": sigma,
            "fair_p": decision.features.get("fair_p"),
            "yes_ask": book.best_yes_ask,
            "no_ask": book.best_no_ask,
            "action": decision.action,
            "reason": decision.reason,
            "features": {**decision.features, "spot_key": key, "strategy": self.strategy.name},
            "config_id": self.config_id,
        }
        if decision.intent is None:
            summary.skips += 1
            self.store.insert_decision(row)
            return
        it = decision.intent
        intent = OrderIntent(
            ticker=market.ticker,
            series=series,
            side=it.side,
            count=it.count,
            price=it.price,
            secs_to_close=secs or 0.0,
            spot_age=(now - spot_ts) if spot_ts else None,
            book_age=time.time() - book_ts,
        )
        verdict = self.risk.check(intent, open_positions, now)
        row.update(
            {
                "side": it.side,
                "count": verdict.adjusted_count or it.count,
                "price": it.price,
                "edge": it.edge,
            }
        )
        if not verdict.allowed:
            row["action"], row["reason"] = "rejected", verdict.reason
            summary.rejected += 1
            self.store.insert_decision(row)
            log.info(
                "%s %s x%d @%.3f REJECTED: %s",
                market.ticker,
                it.side,
                it.count,
                it.price,
                verdict.reason,
            )
            return
        count = verdict.adjusted_count or it.count
        position = self._execute(market, series, it.side, count, it.price, now, it.exec_mode)
        self._take_next.discard(market.ticker)
        if position is not None:
            open_positions.append(position)
            summary.orders += 1
            row["action"] = "order"
        else:
            row["action"] = "failed"
            row["reason"] = f"order not placed: {getattr(self, 'last_order_error', '')}"
            summary.errors += 1
        self.store.insert_decision(row)

    # ------------------------------------------------------------ execution

    def _execute(
        self,
        market: Market,
        series: str,
        side: str,
        count: int,
        price: float,
        now: float,
        exec_mode: str = "taker",
    ) -> dict | None:
        """Place the order. Taker orders are assumed filled at once (paper) or checked at
        settlement (live). Maker orders rest and are resolved by ``_manage_resting``."""
        params = self.strategy.params
        client_order_id = str(uuid.uuid4())
        order_id = None
        rest = params.rest_seconds if exec_mode == "maker" else 15.0
        if self.mode == "live":
            try:
                result = self.client.create_order(
                    market.ticker,
                    side=side,
                    action="buy",
                    count=count,
                    price=price,
                    client_order_id=client_order_id,
                    expiration_ts=int(now + rest),
                )
            except (KalshiError, ValueError) as exc:
                log.error("%s: live order failed: %s", market.ticker, exc)
                self.last_order_error = str(exc)[:300]
                return None
            if isinstance(result, DryRunOrder):
                log.error("live mode but client returned a dry-run order; refusing")
                return None
            order_id = result.order_id
        rate = params.maker_rate if exec_mode == "maker" else params.taker_rate
        fee = order_fee(price, count, rate)
        row = {
            "mode": self.mode,
            "ticker": market.ticker,
            "series": series,
            "side": side,
            "count": count,
            "price": price,
            "fee": fee,
            "opened_ts": now,
            "close_ts": market.close_time.timestamp() if market.close_time else None,
            "order_id": order_id,
            "client_order_id": client_order_id,
            "status": "resting" if exec_mode == "maker" else "open",
        }
        row["id"] = self.store.open_position(row)
        log.info(
            "%s %s %s %s x%d @%.3f fee %.2f%s",
            self.mode.upper(),
            exec_mode.upper(),
            market.ticker,
            side,
            count,
            price,
            fee,
            f" order={order_id}" if order_id else "",
        )
        return row

    # ------------------------------------------------------------ resting (maker) orders

    def _manage_resting(self, now: float, summary: TickSummary) -> None:
        """Resolve maker orders: filled -> open, expired unfilled -> unfilled (+ hybrid flag)."""
        params = self.strategy.params
        for pos in self.store.positions(self.mode, "resting"):
            expired = now >= pos["opened_ts"] + params.rest_seconds
            filled = self._filled_count(pos, now)
            if filled > 0:
                fee = order_fee(pos["price"], filled, params.maker_rate)
                self.store.update_position(pos["id"], status="open", count=filled, fee=fee)
                log.info("%s maker filled x%g @%.3f", pos["ticker"], filled, pos["price"])
                continue
            if expired:
                if self.mode == "live" and pos["order_id"]:
                    try:
                        self.client.cancel_order(pos["order_id"])
                    except KalshiError as exc:
                        log.debug("cancel %s: %s", pos["order_id"], exc)
                self.store.update_position(pos["id"], status="unfilled", pnl=0.0, settled_ts=now)
                if params.exec_mode == "hybrid":
                    self._take_next.add(pos["ticker"])
                log.info("%s maker order expired unfilled", pos["ticker"])

    def _filled_count(self, pos: dict, now: float) -> float:
        if self.mode == "live" and pos["order_id"]:
            try:
                order = self.client.get_order(pos["order_id"])
            except KalshiError as exc:
                log.warning("get_order %s failed: %s", pos["order_id"], exc)
                return 0.0
            filled = max(0.0, order.count - order.remaining_count)
            if order.status in ("executed", "filled") and filled == 0:
                filled = order.count
            return filled
        # paper: our resting bid is filled if a later public print trades at or through it
        for t in self.store.trades_after(pos["ticker"], pos["opened_ts"]):
            px = t["yes_price"] if pos["side"] == "yes" else t["no_price"]
            if px is not None and px <= pos["price"]:
                return float(pos["count"])
        return 0.0

    # ------------------------------------------------------------ settlement

    def _settle(self, now: float, summary: TickSummary) -> None:
        for pos in self.store.positions(self.mode, "open"):
            if pos["close_ts"] is None or now < pos["close_ts"] + 30:
                continue
            try:
                market = self.client.get_market(pos["ticker"])
            except KalshiError as exc:
                log.warning("%s: settlement fetch failed: %s", pos["ticker"], exc)
                continue
            if not market.result:
                if now > pos["close_ts"] + 3600:
                    log.warning("%s: no result an hour after close; leaving open", pos["ticker"])
                continue
            if self.mode == "live" and pos["order_id"]:
                filled = self._live_fill_count(pos["order_id"], pos["count"])
                if filled == 0:
                    self.store.update_position(
                        pos["id"], status="unfilled", result=market.result, pnl=0.0, settled_ts=now
                    )
                    log.info("%s live order never filled", pos["ticker"])
                    continue
                pos["count"] = filled
            won = market.result == pos["side"]
            gross = pos["count"] * ((1 - pos["price"]) if won else -pos["price"])
            pnl = gross - pos["fee"]
            self.store.update_position(
                pos["id"],
                status="settled",
                result=market.result,
                pnl=pnl,
                count=pos["count"],
                settled_ts=now,
            )
            self.store.set_decision_results(pos["ticker"], market.result)
            self.risk.record_settlement(pnl, now, expected_win_p=self._expected_p(pos))
            summary.settled += 1
            log.info(
                "%s settled %s: %s x%g @%.3f -> %+.2f (day %+.2f)",
                pos["ticker"],
                market.result,
                pos["side"],
                pos["count"],
                pos["price"],
                pnl,
                self.risk.state.day_pnl,
            )

    def _expected_p(self, pos: dict) -> float | None:
        """The model's win probability for this position at entry, from the decision log."""
        for d in self.store.decisions(self.mode, 500):
            if d["ticker"] == pos["ticker"] and d["action"] == "order" and d["fair_p"]:
                return d["fair_p"] if pos["side"] == "yes" else 1 - d["fair_p"]
        return None

    def _live_fill_count(self, order_id: str, requested: float) -> float:
        try:
            fills = self.client.get_fills(order_id=order_id)
        except KalshiError as exc:
            log.warning("fills for %s failed: %s; assuming requested", order_id, exc)
            return requested
        return sum(f.count for f in fills) if fills else 0.0

    # ------------------------------------------------------------ loop

    def stop(self) -> None:
        self._stop.set()

    def run(self, *, max_ticks: int | None = None, install_signals: bool = True) -> int:
        if install_signals:
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: self.stop())
        for feed in (self.spot_ws, self.pyth):
            if feed is not None:
                feed.start()
        log.info(
            "trader %s mode, %s, limits %s",
            self.mode.upper(),
            ",".join(self.universe.series),
            json.dumps(self.risk.limits.__dict__),
        )
        if self.risk.state.halted:
            log.warning(
                "risk engine is HALTED: %s (run `kalshi-bot risk-reset` to clear)",
                self.risk.state.halt_reason,
            )
        ticks = 0
        while not self._stop.is_set():
            started = time.time()
            try:
                s = self.tick(started)
                log.info(
                    "tick markets=%d orders=%d skips=%d rejected=%d settled=%d errors=%d "
                    "day_pnl=%+.2f",
                    s.markets,
                    s.orders,
                    s.skips,
                    s.rejected,
                    s.settled,
                    s.errors,
                    self.risk.state.day_pnl,
                )
            except Exception:  # noqa: BLE001
                log.exception("tick crashed")
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            self._stop.wait(max(0.0, self.interval - (time.time() - started)))
        for feed in (self.spot_ws, self.pyth):
            if feed is not None:
                feed.stop()
        log.info("trader stopped after %d ticks", ticks)
        return ticks
