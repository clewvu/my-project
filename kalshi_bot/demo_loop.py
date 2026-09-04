"""Demo-only alternating trader.

A deliberately dumb strategy for exercising the order path on Kalshi's demo
(paper money) exchange: for each configured series (BTC and DOGE 15-minute
markets by default) it buys one side of each successive market, alternating
YES ("up") and NO ("down") per series, holds to settlement, and books the
result. It exists to test plumbing, not to make money; the research modules
decide whether anything has an edge.

Sizing and bounds
-----------------
* ``dollars``: spend about this much per trade; contracts =
  floor(dollars / ask), at least 1. ``contracts`` is used instead when
  ``dollars`` is unset.
* ``max_price``: never pay more than this per contract, which caps the loss
  on any single trade at what was spent plus the fee.
* ``loss_cap``: stop once cumulative realised P&L (after fees) falls to
  ``-loss_cap`` dollars. ``profit_target``: stop once it reaches this
  (``None``: no profit cap).
* ``max_trades``: stop after this many trades across all series.
* ``min_ttc``: no entries, and any unfilled order cancelled, inside this
  many seconds of close.

A cap that is hit while positions are open stops new entries; the loop keeps
running until those positions settle, then exits.

Stopping
--------
Ctrl-C, or create the stop file (``state/STOP`` by default). Both cancel
resting orders; an already-filled position is held to settlement and booked
on the next run. State lives in a JSON file so a restart cannot reset the
caps. ``--reset`` clears it.

Safety
------
The loop refuses to run against production unless constructed with
``allow_production=True``, which only the ``live-trade`` command does, after
its own confirmation step; ``demo-trade`` never does. With
``KALSHI_DRY_RUN=true`` it still runs but simulates fills at the limit price
instead of sending orders, which is the way to try it before configuring a
key's real orders.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .client import DryRunOrder, KalshiClient
from .fees import order_fee
from .models import Market

log = logging.getLogger(__name__)

SIDES = ("yes", "no")
DEFAULT_SERIES = ("KXBTC15M", "KXDOGE15M")
SETTLEMENT_GRACE_S = 30.0
SETTLEMENT_GIVE_UP_S = 3 * 3600.0


class RefusedProduction(Exception):
    """The demo loop was pointed at production."""


@dataclass
class LoopConfig:
    series: tuple[str, ...] = DEFAULT_SERIES
    contracts: int = 1
    dollars: float | None = None
    max_price: float = 0.60
    loss_cap: float = 5.0
    profit_target: float | None = 10.0  # None = run until stopped or the loss cap
    max_trades: int | None = None
    min_ttc: float = 120.0
    interval: float = 5.0
    first_side: str = "yes"
    stop_file: Path = Path("state/STOP")
    state_file: Path = Path("state/demo_loop.json")

    def validate(self) -> None:
        if not self.series:
            raise ValueError("at least one series is required")
        if self.contracts < 1:
            raise ValueError("contracts must be at least 1")
        if self.dollars is not None and self.dollars <= 0:
            raise ValueError("dollars must be positive")
        if not 0 < self.max_price < 1:
            raise ValueError("max_price must be between 0 and 1 dollars")
        if self.loss_cap <= 0:
            raise ValueError("loss_cap must be positive dollars")
        if self.profit_target is not None and self.profit_target <= 0:
            raise ValueError("profit_target must be positive dollars, or None for no cap")
        if self.first_side not in SIDES:
            raise ValueError("first_side must be yes or no")
        if self.min_ttc < 0 or self.interval <= 0:
            raise ValueError("min_ttc must be >= 0 and interval > 0")

    def size(self, price: float) -> int:
        if self.dollars is None:
            return self.contracts
        return max(1, math.floor(self.dollars / price + 1e-9))


@dataclass
class OpenTrade:
    ticker: str
    side: str
    count: int
    limit_price: float
    order_id: str | None
    close_ts: float
    placed_ts: float
    filled_count: float = 0.0
    fill_price: float | None = None
    simulated: bool = False
    prev_side: str | None = None  # the series' last_side before this entry, for undo
    fee_paid: float | None = None  # exchange-reported fees on the fills, when available

    @property
    def filled(self) -> bool:
        return self.filled_count > 0


@dataclass
class SeriesState:
    last_side: str | None = None
    open: OpenTrade | None = None
    seen_tickers: list[str] = field(default_factory=list)

    def next_side(self, first_side: str) -> str:
        if self.last_side is None:
            return first_side
        return "no" if self.last_side == "yes" else "yes"

    def note_ticker(self, ticker: str) -> None:
        if ticker not in self.seen_tickers:
            self.seen_tickers.append(ticker)
            del self.seen_tickers[:-100]


@dataclass
class LoopState:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    series: dict[str, SeriesState] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    halted: str | None = None
    last_tick_ts: float | None = None  # heartbeat for the dashboard
    stopped: str | None = None  # why the last run ended
    config: dict[str, Any] = field(default_factory=dict)  # what the last run was told

    @classmethod
    def load(cls, path: Path) -> LoopState:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        known = {f.name for f in fields(cls)}
        series_raw = data.pop("series", {}) or {}
        state = cls(**{k: v for k, v in data.items() if k in known})
        if isinstance(series_raw, dict):
            for name, raw in series_raw.items():
                open_raw = raw.pop("open", None)
                ss = SeriesState(**{k: v for k, v in raw.items() if k != "open"})
                if open_raw:
                    ss.open = OpenTrade(**open_raw)
                state.series[name] = ss
        return state

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, default=str))
        tmp.replace(path)

    def for_series(self, name: str) -> SeriesState:
        return self.series.setdefault(name, SeriesState())

    @property
    def open_trades(self) -> list[tuple[str, OpenTrade]]:
        return [(name, ss.open) for name, ss in self.series.items() if ss.open is not None]

    def summary(self) -> str:
        opens = ", ".join(f"{t.side} x{t.count} {t.ticker}" for _, t in self.open_trades) or "none"
        return (
            f"trades={self.trades} wins={self.wins} losses={self.losses} "
            f"pnl={self.realized_pnl:+.2f} fees={self.fees_paid:.2f} open={opens}"
            + (f" halted={self.halted}" if self.halted else "")
        )


class DemoLoop:
    """One instance per run. ``clock`` and ``sleep`` are injectable for tests."""

    def __init__(
        self,
        client: KalshiClient,
        config: LoopConfig,
        state: LoopState | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        allow_production: bool = False,
    ) -> None:
        if client.is_prod and not allow_production:
            raise RefusedProduction("the demo loop only runs against the demo exchange")
        config.validate()
        self.client = client
        self.cfg = config
        self.live = bool(client.is_prod and not client.dry_run)
        self.state = state or LoopState.load(config.state_file)
        self.state.config = {
            "env": "dry-run" if client.dry_run else ("LIVE" if client.is_prod else "demo"),
            "series": list(config.series),
            "contracts": config.contracts,
            "dollars": config.dollars,
            "max_price": config.max_price,
            "loss_cap": config.loss_cap,
            "profit_target": config.profit_target,
            "max_trades": config.max_trades,
            "min_ttc": config.min_ttc,
            "interval": config.interval,
        }
        self.clock = clock
        self.sleep = sleep
        self._last_skip: dict[str, tuple[str, str]] = {}

    # ------------------------------------------------------------------ run

    def run(self, max_ticks: int | None = None) -> str:
        """Tick until stopped; returns the reason."""
        if self.client.dry_run:
            log.warning("KALSHI_DRY_RUN is on: fills are simulated at the limit price")
        log.info("demo loop start: %s", self.state.summary())
        ticks = 0
        try:
            while max_ticks is None or ticks < max_ticks:
                ticks += 1
                reason = self.tick()
                if reason:
                    return self._stop(reason)
                self.sleep(self.cfg.interval)
        except KeyboardInterrupt:
            return self._stop("interrupted")
        return "tick limit"

    def _stop(self, reason: str) -> str:
        for name, trade in self.state.open_trades:
            if not trade.filled:
                self._cancel_open(name, "stopping")
        self.state.stopped = reason
        self.state.save(self.cfg.state_file)
        log.info("demo loop stopped (%s): %s", reason, self.state.summary())
        return reason

    def tick(self) -> str | None:
        """One pass. Returns a stop reason, or None to keep going."""
        if self.cfg.stop_file.exists():
            return f"stop file {self.cfg.stop_file}"
        now = self.clock()
        self.state.last_tick_ts = now
        self.state.stopped = None
        for name in self.cfg.series:
            if self.state.for_series(name).open is not None:
                self._settle_if_closed(name, now)
        halt = self.state.halted or self._check_caps()
        if halt and not self.state.halted:
            self.state.halted = halt
            log.info("halting after open positions settle: %s", halt)
        if halt and not self.state.open_trades:
            self.state.save(self.cfg.state_file)
            return halt
        for name in self.cfg.series:
            ss = self.state.for_series(name)
            if ss.open is not None:
                self._manage_open(name, now)
            elif not halt:
                self._maybe_enter(name, now)
        self.state.save(self.cfg.state_file)
        return None

    # ------------------------------------------------------------------ steps

    def _check_caps(self) -> str | None:
        s = self.state
        if s.realized_pnl <= -self.cfg.loss_cap:
            return f"loss cap reached ({s.realized_pnl:+.2f} <= -{self.cfg.loss_cap:.2f})"
        if self.cfg.profit_target is not None and s.realized_pnl >= self.cfg.profit_target:
            return f"profit target reached ({s.realized_pnl:+.2f} >= {self.cfg.profit_target:.2f})"
        if self.cfg.max_trades is not None and s.trades >= self.cfg.max_trades:
            return f"max trades reached ({s.trades})"
        return None

    def _maybe_enter(self, name: str, now: float) -> None:
        ss = self.state.for_series(name)
        market = self._pick_market(name, ss, now)
        if market is None:
            return
        side = ss.next_side(self.cfg.first_side)
        price = market.yes_ask if side == "yes" else market.no_ask
        if price is None:
            self._skip(name, market.ticker, f"no {side} ask")
            return
        if price > self.cfg.max_price:
            self._skip(
                name, market.ticker, f"{side} ask {price:.3f} above max_price {self.cfg.max_price}"
            )
            return
        count = self.cfg.size(price)
        close_ts = market.close_time.timestamp() if market.close_time else now + 900
        order = self.client.create_order(
            market.ticker,
            side=side,
            action="buy",
            count=count,
            price=price,
            order_type="limit",
        )
        trade = OpenTrade(
            ticker=market.ticker,
            side=side,
            count=count,
            limit_price=price,
            order_id=None if isinstance(order, DryRunOrder) else order.order_id,
            close_ts=close_ts,
            placed_ts=now,
            simulated=isinstance(order, DryRunOrder),
            prev_side=ss.last_side,
        )
        if trade.simulated:
            trade.filled_count = float(count)
            trade.fill_price = price
        ss.open = trade
        ss.last_side = side
        ss.note_ticker(market.ticker)
        self.state.trades += 1
        self.state.save(self.cfg.state_file)
        log.info(
            "%sbuy %s x%d %s at %.3f (~$%.2f, %s)",
            "REAL MONEY: " if self.live else "",
            side,
            count,
            market.ticker,
            price,
            count * price,
            "simulated" if trade.simulated else f"order {trade.order_id}",
        )

    def _pick_market(self, name: str, ss: SeriesState, now: float) -> Market | None:
        candidates = []
        for m in self.client.get_markets(series_ticker=name, status="open"):
            if m.ticker in ss.seen_tickers or m.close_time is None:
                continue
            ttc = m.seconds_to_close(datetime.fromtimestamp(now, tz=UTC))
            if ttc is None or ttc < self.cfg.min_ttc:
                continue
            candidates.append((ttc, m))
        if not candidates:
            return None
        candidates.sort(key=lambda c: c[0])
        return candidates[0][1]

    def _skip(self, name: str, ticker: str, why: str) -> None:
        key = (ticker, why)
        if self._last_skip.get(name) != key:
            log.info("skip %s: %s", ticker, why)
            self._last_skip[name] = key

    def _manage_open(self, name: str, now: float) -> None:
        trade = self.state.for_series(name).open
        if trade is None or trade.filled:
            return
        self._refresh_fills(trade)
        if trade.filled:
            self.state.save(self.cfg.state_file)
            log.info(
                "filled %s x%.0f %s at %.3f",
                trade.side,
                trade.filled_count,
                trade.ticker,
                trade.fill_price,
            )
            return
        if now >= trade.close_ts - self.cfg.min_ttc:
            self._cancel_open(name, "unfilled inside the no-entry window")

    def _refresh_fills(self, trade: OpenTrade) -> None:
        if trade.order_id is None:
            return
        fills = [
            f
            for f in self.client.get_fills(ticker=trade.ticker, order_id=trade.order_id)
            if f.order_id == trade.order_id
        ]
        count = sum(f.count for f in fills)
        if count > 0:
            trade.filled_count = count
            trade.fill_price = sum(f.count * f.price for f in fills) / count
            if all(f.fee is not None for f in fills):
                trade.fee_paid = float(sum(f.fee for f in fills if f.fee is not None))

    def _drop_unfilled(self, name: str, ss: SeriesState) -> None:
        # the alternation and the trade count only advance on a fill
        self.state.trades -= 1
        if ss.open is not None:
            ss.last_side = ss.open.prev_side
        ss.open = None
        self.state.save(self.cfg.state_file)

    def _cancel_open(self, name: str, why: str) -> None:
        ss = self.state.for_series(name)
        trade = ss.open
        if trade is None:
            return
        if trade.order_id is not None:
            try:
                self.client.cancel_order(trade.order_id, ticker=trade.ticker)
            except Exception as exc:  # noqa: BLE001 - keep the loop alive; the order expires at close
                log.warning("cancel %s failed: %s", trade.order_id, exc)
            self._refresh_fills(trade)
            if trade.filled:
                log.info("order %s filled before cancel; holding", trade.order_id)
                self.state.save(self.cfg.state_file)
                return
        log.info("cancelled %s on %s: %s", trade.side, trade.ticker, why)
        self._drop_unfilled(name, ss)

    def _settle_if_closed(self, name: str, now: float) -> None:
        ss = self.state.for_series(name)
        trade = ss.open
        if trade is None or now < trade.close_ts + SETTLEMENT_GRACE_S:
            return
        if not trade.filled:
            self._refresh_fills(trade)
            if not trade.filled:
                log.info("%s closed with no fill; nothing to settle", trade.ticker)
                self._drop_unfilled(name, ss)
                return
        market = self.client.get_market(trade.ticker)
        if market.result not in SIDES:
            if now > trade.close_ts + SETTLEMENT_GIVE_UP_S:
                log.warning("%s still unsettled after 3 hours; check the exchange", trade.ticker)
            return
        price = trade.fill_price if trade.fill_price is not None else trade.limit_price
        won = market.result == trade.side
        gross = trade.filled_count * ((1 - price) if won else -price)
        fee = trade.fee_paid if trade.fee_paid is not None else order_fee(price, trade.filled_count)
        net = gross - fee
        s = self.state
        s.realized_pnl += net
        s.fees_paid += fee
        s.wins += int(won)
        s.losses += int(not won)
        s.history.append(
            {
                "series": name,
                "ticker": trade.ticker,
                "side": trade.side,
                "count": trade.filled_count,
                "price": price,
                "result": market.result,
                "won": won,
                "net": round(net, 4),
                "settled_ts": now,
            }
        )
        del s.history[:-200]
        ss.open = None
        s.save(self.cfg.state_file)
        log.info(
            "settled %s: %s, %s x%.0f at %.3f -> %+.2f (fee %.2f); %s",
            trade.ticker,
            market.result.upper(),
            trade.side,
            trade.filled_count,
            price,
            net,
            fee,
            s.summary(),
        )
