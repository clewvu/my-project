"""Demo-only alternating trader.

A deliberately dumb strategy for exercising the order path on Kalshi's demo
(paper money) exchange: in each successive 15-minute market it buys one side,
alternating YES ("up") and NO ("down"), holds to settlement, and books the
result. It exists to test plumbing, not to make money; the research modules
decide whether anything has an edge.

Bounds
------
* ``max_price``: never pay more than this per contract, which caps the loss
  on any single trade at ``contracts * max_price`` plus the fee.
* ``loss_cap``: stop once cumulative realised P&L (after fees) falls to
  ``-loss_cap`` dollars.
* ``profit_target``: stop once cumulative realised P&L reaches this.
* ``max_trades``: stop after this many trades.
* ``min_ttc``: no entries, and any unfilled order cancelled, inside this
  many seconds of close.

Stopping
--------
Ctrl-C, or create the stop file (``state/STOP`` by default). Both cancel
resting orders; an already-filled position is held to settlement and booked
on the next run. State (P&L, trade count, last side, open position) lives in
a JSON file so a restart cannot reset the caps. ``--reset`` clears it.

Safety
------
The loop refuses to run against production, full stop. It never sets
``allow_live``. With ``KALSHI_DRY_RUN=true`` it still runs but simulates
fills at the limit price instead of sending orders, which is the way to try
it before configuring a demo key's real paper orders.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .client import DryRunOrder, KalshiClient
from .fees import order_fee
from .models import Market

log = logging.getLogger(__name__)

SIDES = ("yes", "no")
SETTLEMENT_GRACE_S = 30.0
SETTLEMENT_GIVE_UP_S = 3 * 3600.0


class RefusedProduction(Exception):
    """The demo loop was pointed at production."""


@dataclass
class LoopConfig:
    series: str = "KXBTC15M"
    contracts: int = 1
    max_price: float = 0.60
    loss_cap: float = 5.0
    profit_target: float = 10.0
    max_trades: int | None = None
    min_ttc: float = 120.0
    interval: float = 5.0
    first_side: str = "yes"
    stop_file: Path = Path("state/STOP")
    state_file: Path = Path("state/demo_loop.json")

    def validate(self) -> None:
        if self.contracts < 1:
            raise ValueError("contracts must be at least 1")
        if not 0 < self.max_price < 1:
            raise ValueError("max_price must be between 0 and 1 dollars")
        if self.loss_cap <= 0 or self.profit_target <= 0:
            raise ValueError("loss_cap and profit_target must be positive dollars")
        if self.first_side not in SIDES:
            raise ValueError("first_side must be yes or no")
        if self.min_ttc < 0 or self.interval <= 0:
            raise ValueError("min_ttc must be >= 0 and interval > 0")


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

    @property
    def filled(self) -> bool:
        return self.filled_count > 0


@dataclass
class LoopState:
    last_side: str | None = None
    trades: int = 0
    wins: int = 0
    losses: int = 0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    open: OpenTrade | None = None
    seen_tickers: list[str] = field(default_factory=list)
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
        open_trade = data.pop("open", None)
        state = cls(**data)
        if open_trade:
            state.open = OpenTrade(**open_trade)
        return state

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.replace(path)

    def next_side(self, first_side: str) -> str:
        if self.last_side is None:
            return first_side
        return "no" if self.last_side == "yes" else "yes"

    def note_ticker(self, ticker: str) -> None:
        if ticker not in self.seen_tickers:
            self.seen_tickers.append(ticker)
            del self.seen_tickers[:-100]

    def summary(self) -> str:
        pos = f"{self.open.side} x{self.open.count} {self.open.ticker}" if self.open else "none"
        return (
            f"trades={self.trades} wins={self.wins} losses={self.losses} "
            f"pnl={self.realized_pnl:+.2f} fees={self.fees_paid:.2f} open={pos}"
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
    ) -> None:
        if client.is_prod:
            raise RefusedProduction("the demo loop only runs against the demo exchange")
        config.validate()
        self.client = client
        self.cfg = config
        self.state = state or LoopState.load(config.state_file)
        self.state.config = {
            "env": "dry-run" if client.dry_run else "demo",
            "series": config.series,
            "contracts": config.contracts,
            "max_price": config.max_price,
            "loss_cap": config.loss_cap,
            "profit_target": config.profit_target,
            "max_trades": config.max_trades,
            "min_ttc": config.min_ttc,
            "interval": config.interval,
        }
        self.clock = clock
        self.sleep = sleep
        self._last_skip: tuple[str, str] | None = None

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
        if self.state.open and not self.state.open.filled:
            self._cancel_open("stopping")
        self.state.stopped = reason
        self.state.save(self.cfg.state_file)
        log.info("demo loop stopped (%s): %s", reason, self.state.summary())
        return reason

    def tick(self) -> str | None:
        """One pass. Returns a stop reason, or None to keep going."""
        if self.cfg.stop_file.exists():
            return f"stop file {self.cfg.stop_file}"
        if self.state.halted:
            return self.state.halted
        now = self.clock()
        self.state.last_tick_ts = now
        self.state.stopped = None
        if self.state.open:
            self._settle_if_closed(now)
        halt = self._check_caps()
        if halt:
            self.state.halted = halt
            self.state.save(self.cfg.state_file)
            return halt
        if self.state.open:
            self._manage_open(now)
        else:
            self._maybe_enter(now)
        self.state.save(self.cfg.state_file)
        return None

    # ------------------------------------------------------------------ steps

    def _check_caps(self) -> str | None:
        s = self.state
        if s.open is not None:
            return None  # judge the caps once the open trade has settled
        if s.realized_pnl <= -self.cfg.loss_cap:
            return f"loss cap reached ({s.realized_pnl:+.2f} <= -{self.cfg.loss_cap:.2f})"
        if s.realized_pnl >= self.cfg.profit_target:
            return f"profit target reached ({s.realized_pnl:+.2f} >= {self.cfg.profit_target:.2f})"
        if self.cfg.max_trades is not None and s.trades >= self.cfg.max_trades:
            return f"max trades reached ({s.trades})"
        return None

    def _maybe_enter(self, now: float) -> None:
        market = self._pick_market(now)
        if market is None:
            return
        side = self.state.next_side(self.cfg.first_side)
        price = market.yes_ask if side == "yes" else market.no_ask
        if price is None:
            self._skip(market.ticker, f"no {side} ask")
            return
        if price > self.cfg.max_price:
            self._skip(
                market.ticker, f"{side} ask {price:.3f} above max_price {self.cfg.max_price}"
            )
            return
        close_ts = market.close_time.timestamp() if market.close_time else now + 900
        order = self.client.create_order(
            market.ticker,
            side=side,
            action="buy",
            count=self.cfg.contracts,
            price=price,
            order_type="limit",
        )
        trade = OpenTrade(
            ticker=market.ticker,
            side=side,
            count=self.cfg.contracts,
            limit_price=price,
            order_id=None if isinstance(order, DryRunOrder) else order.order_id,
            close_ts=close_ts,
            placed_ts=now,
            simulated=isinstance(order, DryRunOrder),
        )
        if trade.simulated:
            trade.filled_count = float(self.cfg.contracts)
            trade.fill_price = price
        self.state.open = trade
        self.state.last_side = side
        self.state.trades += 1
        self.state.note_ticker(market.ticker)
        self.state.save(self.cfg.state_file)
        log.info(
            "buy %s x%d %s at %.3f (%s)",
            side,
            self.cfg.contracts,
            market.ticker,
            price,
            "simulated" if trade.simulated else f"order {trade.order_id}",
        )

    def _pick_market(self, now: float) -> Market | None:
        candidates = []
        for m in self.client.get_markets(series_ticker=self.cfg.series, status="open"):
            if m.ticker in self.state.seen_tickers or m.close_time is None:
                continue
            ttc = m.seconds_to_close(datetime.fromtimestamp(now, tz=UTC))
            if ttc is None or ttc < self.cfg.min_ttc:
                continue
            candidates.append((ttc, m))
        if not candidates:
            return None
        candidates.sort(key=lambda c: c[0])
        return candidates[0][1]

    def _skip(self, ticker: str, why: str) -> None:
        key = (ticker, why)
        if key != self._last_skip:
            log.info("skip %s: %s", ticker, why)
            self._last_skip = key

    def _manage_open(self, now: float) -> None:
        trade = self.state.open
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
            self._cancel_open("unfilled inside the no-entry window")

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

    def _cancel_open(self, why: str) -> None:
        trade = self.state.open
        if trade is None:
            return
        if trade.order_id is not None:
            try:
                self.client.cancel_order(trade.order_id)
            except Exception as exc:  # noqa: BLE001 - keep the loop alive; the order expires at close
                log.warning("cancel %s failed: %s", trade.order_id, exc)
            self._refresh_fills(trade)
            if trade.filled:
                log.info("order %s filled before cancel; holding", trade.order_id)
                self.state.save(self.cfg.state_file)
                return
        log.info("cancelled %s on %s: %s", trade.side, trade.ticker, why)
        # the alternation and the trade count only advance on a fill
        self.state.trades -= 1
        self.state.last_side = _previous_side(self.state)
        self.state.open = None
        self.state.save(self.cfg.state_file)

    def _settle_if_closed(self, now: float) -> None:
        trade = self.state.open
        if trade is None or now < trade.close_ts + SETTLEMENT_GRACE_S:
            return
        if not trade.filled:
            self._refresh_fills(trade)
            if not trade.filled:
                log.info("%s closed with no fill; nothing to settle", trade.ticker)
                self.state.trades -= 1
                self.state.last_side = _previous_side(self.state)
                self.state.open = None
                self.state.save(self.cfg.state_file)
                return
        market = self.client.get_market(trade.ticker)
        if market.result not in SIDES:
            if now > trade.close_ts + SETTLEMENT_GIVE_UP_S:
                log.warning("%s still unsettled after 3 hours; check the exchange", trade.ticker)
            return
        price = trade.fill_price if trade.fill_price is not None else trade.limit_price
        won = market.result == trade.side
        gross = trade.filled_count * ((1 - price) if won else -price)
        fee = order_fee(price, trade.filled_count)
        net = gross - fee
        s = self.state
        s.realized_pnl += net
        s.fees_paid += fee
        s.wins += int(won)
        s.losses += int(not won)
        s.history.append(
            {
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
        s.open = None
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


def _previous_side(state: LoopState) -> str | None:
    """Undo one alternation step after a trade that never filled."""
    if state.trades == 0:
        return None
    return "no" if state.last_side == "yes" else "yes"
