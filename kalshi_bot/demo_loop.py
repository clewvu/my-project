"""The trading loop: one entry per market per series, hold to settlement.

The loop asks a strategy (``kalshi_bot.strategy``) which side to buy, if
any, in each open market of each configured series (BTC and DOGE 15-minute
markets by default), sizes the order, places it, tracks the fill, books the
settlement, and enforces the caps. ``alternate`` buys YES then NO on
successive markets and exists to test plumbing; ``fairvalue`` is the model
from the research brief and stays out unless it sees an edge over the fee.

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

Stopping and pausing
--------------------
Ctrl-C, or create the stop file (``state/STOP`` by default). Both cancel
resting orders; an already-filled position is held to settlement and booked
on the next run. State lives in a JSON file so a restart cannot reset the
caps. ``--reset`` clears it. The pause file (``state/PAUSE``) is softer:
while it exists the loop keeps ticking, manages and settles what it holds,
but opens nothing new; removing it resumes. The dashboard writes both.

Reconciliation
--------------
Every ``reconcile_s`` seconds (and on the first tick) the loop compares its
filled positions with the exchange's. A position on one of our series that
the loop does not know about, or a filled trade the exchange does not
show, is flagged; if it is still there at the next check the loop halts,
because its P&L can no longer be trusted. Simulated fills are exempt.

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

from .alerts import AlertLog
from .client import DryRunOrder, KalshiClient
from .fees import order_fee
from .models import Market
from .strategy import DecisionLog, Skip, Strategy, build_strategy

log = logging.getLogger(__name__)

SIDES = ("yes", "no")
DEFAULT_SERIES = ("KXBTC15M", "KXDOGE15M")
SETTLEMENT_GRACE_S = 30.0
SETTLEMENT_GIVE_UP_S = 3 * 3600.0


class RefusedProduction(Exception):
    """The demo loop was pointed at production."""


def price_tick(price: float) -> float:
    """Kalshi's grid: 0.001 below 10c and above 90c, 0.01 in between."""
    return 0.01 if 0.10 <= price < 0.90 else 0.001


def maker_price(bid: float | None, ask: float) -> float | None:
    """One tick inside the spread on our side, or None when there is no room
    (no bid, or bid + tick would reach the ask: then just take)."""
    if bid is None:
        return None
    candidate = round(bid + price_tick(bid), 4)
    if candidate >= ask - 1e-9:
        return None
    return candidate


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
    strategy: str = "alternate"  # alternate | fairvalue
    margin: float = 0.02  # fairvalue: edge required beyond the fee, dollars
    vol_window: float = 1800.0  # fairvalue: seconds of spot history behind sigma
    spot_db: Path | None = Path("state/market_data.sqlite")  # seeds spot history if present
    decision_log: Path | None = Path("state/decisions.jsonl")
    params_path: Path | None = Path("state/params.json")  # written by `kalshi-bot learn`
    exits: bool = True  # let the strategy sell before settlement
    exit_margin: float = 0.02  # fairvalue: sell when the bid beats model value by this
    take_profit: float = 0.0  # alternate: sell when the bid is this far above entry (0 = off)
    stop_loss: float = 0.0  # alternate: sell when the bid is this far below entry (0 = off)
    max_entries: int = 1  # entries per market (a re-entry needs the previous one sold)
    free_entries: int = 2  # entries beyond this need the market to be in profit so far
    risk_fraction: float | None = None  # of bankroll per trade; None = the learning loop's
    max_dollars: float = 20.0  # ceiling per trade under fixed-fraction sizing
    bankroll_refresh_s: float = 300.0
    entry: str = "taker"  # taker: pay the ask. maker: rest one tick inside, then take
    maker_wait_s: float = 20.0  # how long a maker order may rest before the taker fallback
    pause_file: Path = Path("state/PAUSE")  # while present: no new entries, keep ticking
    alerts_path: Path | None = Path("state/alerts.jsonl")  # the dashboard's event feed
    reconcile_s: float = 120.0  # compare positions with the exchange this often; 0 = never
    spot_source: str = "auto"  # fairvalue spot: auto (fresh DB tick, else REST) | db | rest
    spot_smooth_s: float = 10.0  # fairvalue: model spot is the mean over this many seconds
    # churn control: a position is held at least min_hold_s before an exit may
    # fire; a market sold out of waits reentry_cooloff_s before another entry;
    # allow_flip permits buying the other side of a market already traded
    min_hold_s: float = 60.0
    reentry_cooloff_s: float = 120.0
    allow_flip: bool = False
    # consecutive-loss breaker: after this many losing results in a row (sales
    # and settlements alike) no entries for loss_pause_s; 0 disables
    max_consecutive_losses: int = 3
    loss_pause_s: float = 1800.0

    def validate(self) -> None:
        if self.max_entries < 1 or self.free_entries < 1:
            raise ValueError("max_entries and free_entries must be at least 1")
        if self.max_entries > 6:
            raise ValueError("max_entries is capped at 6 per market")
        if self.entry not in ("taker", "maker"):
            raise ValueError("entry must be taker or maker")
        if self.strategy not in ("alternate", "fairvalue"):
            raise ValueError("strategy must be alternate or fairvalue")
        if self.margin < 0 or self.vol_window < 300:
            raise ValueError("margin must be >= 0 and vol_window at least 300 seconds")
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
        if min(self.min_hold_s, self.reentry_cooloff_s, self.loss_pause_s, self.spot_smooth_s) < 0:
            raise ValueError(
                "min_hold_s, reentry_cooloff_s, loss_pause_s, spot_smooth_s must be >= 0"
            )
        if self.max_consecutive_losses < 0:
            raise ValueError("max_consecutive_losses must be >= 0 (0 disables the breaker)")

    def size(self, price: float, scale: float = 1.0, dollars: float | None = None) -> int:
        """Contracts for one trade. ``scale`` (0..1) is the learning loop's downward knob;
        ``dollars`` overrides the configured amount (fixed-fraction sizing)."""
        scale = min(1.0, max(0.0, scale))
        dollars = self.dollars if dollars is None else dollars
        if dollars is None:
            return max(1, math.floor(self.contracts * scale + 1e-9))
        return max(1, math.floor(dollars * scale / price + 1e-9))


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
    maker: bool = False  # resting one tick inside the spread rather than taking the ask
    taker_price: float | None = None  # the ask at the signal, for the taker fallback
    fill_ts: float | None = None  # when the fill was first seen; the hold clock starts here

    @property
    def filled(self) -> bool:
        return self.filled_count > 0


@dataclass
class SeriesState:
    last_side: str | None = None
    open: OpenTrade | None = None
    seen_tickers: list[str] = field(default_factory=list)
    entries: dict[str, int] = field(default_factory=dict)  # ticker -> entries made
    market_pnl: dict[str, float] = field(default_factory=dict)  # ticker -> realised so far
    last_exit_ts: dict[str, float] = field(default_factory=dict)  # ticker -> last sale
    sides_traded: dict[str, str] = field(default_factory=dict)  # ticker -> first side bought

    def next_side(self, first_side: str) -> str:
        if self.last_side is None:
            return first_side
        return "no" if self.last_side == "yes" else "yes"

    def note_ticker(self, ticker: str) -> None:
        self.entries[ticker] = self.entries.get(ticker, 0) + 1
        if ticker not in self.seen_tickers:
            self.seen_tickers.append(ticker)
            del self.seen_tickers[:-100]
        for old in list(self.entries)[:-100]:
            self.entries.pop(old, None)
            self.market_pnl.pop(old, None)
            self.last_exit_ts.pop(old, None)
            self.sides_traded.pop(old, None)

    def book(self, ticker: str, net: float) -> None:
        self.market_pnl[ticker] = self.market_pnl.get(ticker, 0.0) + net

    def entries_allowed(self, ticker: str, max_entries: int, free_entries: int) -> bool:
        """Re-entry policy: the first ``free_entries`` need only a signal; beyond
        that, and up to ``max_entries``, the market must be in profit so far."""
        made = self.entries.get(ticker, 0)
        if made >= max_entries:
            return False
        if made < free_entries:
            return True
        return self.market_pnl.get(ticker, 0.0) > 0


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
    paused: bool = False  # the pause file was present at the last tick
    reconciled_ts: float | None = None  # last successful check against the exchange
    breaker_until: float | None = None  # consecutive-loss breaker: no entries until then
    loss_streak: int = 0  # losing results in a row, sales and settlements alike
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

    def save(self, path: Path, attempts: int = 20) -> None:
        """Write atomically, retrying the swap: on Windows it fails while a reader
        (the dashboard, a sync client, an antivirus scan) has the file open."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(self), indent=2, default=str)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(payload)
        for attempt in range(attempts):
            try:
                tmp.replace(path)
                return
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
        log.warning("could not swap %s into place; writing it directly", tmp)
        try:
            path.write_text(payload)
        except OSError as exc:
            log.warning("state save skipped: %s", exc)

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
        strategy: Strategy | None = None,
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
        self.strategy: Strategy = strategy or build_strategy(
            config.strategy,
            first_side=config.first_side,
            max_price=config.max_price,
            margin=config.margin,
            vol_window_s=config.vol_window,
            spot_db=config.spot_db,
            spot_source=config.spot_source,
            series=config.series,
            now=clock(),
            params_path=config.params_path,
            exit_margin=config.exit_margin,
            take_profit=config.take_profit,
            stop_loss=config.stop_loss,
            spot_smooth_s=config.spot_smooth_s,
        )
        self.decisions = DecisionLog(config.decision_log)
        self.state.config["strategy"] = self.strategy.name
        if config.strategy == "fairvalue":
            self.state.config["margin"] = config.margin
            self.state.config["vol_window"] = config.vol_window
        self.bankroll: Any = None  # Balance, refreshed every bankroll_refresh_s
        self._bankroll_ts: float | None = None
        self.alerts = AlertLog(config.alerts_path)
        self._reconcile_ts: float | None = None
        self._mismatches: dict[str, str] = {}  # ticker -> problem seen at the last check
        self._alert_src = "live" if self.live else self.state.config["env"]
        per_trade = f"${config.dollars:.2f}" if config.dollars else f"{config.contracts} ct"
        self.alerts.record(
            "info",
            self._alert_src,
            f"loop started: {self.strategy.name}, {', '.join(config.series)}, "
            f"{per_trade} per trade, loss cap ${config.loss_cap:.2f}",
            now=clock(),
        )

    # ------------------------------------------------------------------ sizing

    def _refresh_bankroll(self, now: float) -> None:
        if self._bankroll_ts is not None and now - self._bankroll_ts < self.cfg.bankroll_refresh_s:
            return
        self._bankroll_ts = now
        try:
            self.bankroll = self.client.get_balance()
        except Exception as exc:  # noqa: BLE001 - sizing falls back to --dollars
            log.debug("balance refresh failed: %s", exc)

    def trade_dollars(self, market: Market) -> float | None:
        """Dollars for the next trade: a fraction of the bankroll on the market's
        shard when fixed-fraction sizing is active, else the configured amount."""
        fraction = self.cfg.risk_fraction
        if fraction is None:
            fraction = float(getattr(self.strategy, "risk_fraction", 0.0) or 0.0)
        if fraction <= 0 or self.bankroll is None:
            return self.cfg.dollars
        available = self.bankroll.on_shard(getattr(market, "exchange_index", None))
        dollars = available * fraction
        if self.cfg.dollars is not None:
            dollars = max(dollars, 0.0)
        return round(min(self.cfg.max_dollars, dollars), 2) if dollars > 0 else self.cfg.dollars

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
        level = "halt" if self.state.halted or "cap" in reason else "info"
        self.alerts.record(level, self._alert_src, f"loop stopped: {reason}", now=self.clock())
        log.info("demo loop stopped (%s): %s", reason, self.state.summary())
        return reason

    def tick(self) -> str | None:
        """One pass. Returns a stop reason, or None to keep going."""
        if self.cfg.stop_file.exists():
            return f"stop file {self.cfg.stop_file}"
        now = self.clock()
        self.state.last_tick_ts = now
        self.state.stopped = None
        paused = self.cfg.pause_file.exists()
        if paused != self.state.paused:
            self.state.paused = paused
            self.alerts.record(
                "warn" if paused else "info",
                self._alert_src,
                "paused from the dashboard: holding positions, opening nothing new"
                if paused
                else "resumed",
                now=now,
            )
        self.strategy.prepare(now)
        self._refresh_bankroll(now)
        for name in self.cfg.series:
            if self.state.for_series(name).open is not None:
                self._settle_if_closed(name, now)
        self._reconcile(now)
        halt = self.state.halted or self._check_caps()
        if halt and not self.state.halted:
            self.state.halted = halt
            self.alerts.record("halt", self._alert_src, f"halting: {halt}", now=now)
            log.info("halting after open positions settle: %s", halt)
        if halt and not self.state.open_trades:
            self.state.save(self.cfg.state_file)
            return halt
        if self.state.breaker_until is not None and now >= self.state.breaker_until:
            self.state.breaker_until = None
            self.alerts.record(
                "info", self._alert_src, "loss breaker cleared; entries resume", now=now
            )
        blocked = halt or paused or self.state.breaker_until is not None
        for name in self.cfg.series:
            ss = self.state.for_series(name)
            if ss.open is not None:
                self._manage_open(name, now)
            elif not blocked:
                self._maybe_enter(name, now)
        self.state.save(self.cfg.state_file)
        return None

    def _book_result(self, net: float, now: float) -> None:
        """Track the losing streak and trip the breaker; called on every booked result."""
        s = self.state
        s.loss_streak = 0 if net > 0 else s.loss_streak + 1
        limit = self.cfg.max_consecutive_losses
        if limit > 0 and self.cfg.loss_pause_s > 0 and s.loss_streak >= limit:
            if s.breaker_until is None or s.breaker_until < now:
                s.breaker_until = now + self.cfg.loss_pause_s
                self.alerts.record(
                    "warn",
                    self._alert_src,
                    f"{s.loss_streak} losses in a row: no new entries for "
                    f"{self.cfg.loss_pause_s / 60:.0f} minutes (open positions still managed)",
                    now=now,
                )

    # ------------------------------------------------------------------ reconciliation

    def _reconcile(self, now: float) -> None:
        """Compare filled positions with the exchange; halt on a repeated mismatch."""
        if self.cfg.reconcile_s <= 0 or self.client.dry_run or self.state.halted:
            return
        if self._reconcile_ts is not None and now - self._reconcile_ts < self.cfg.reconcile_s:
            return
        self._reconcile_ts = now
        try:
            positions = self.client.get_positions(settlement_status="unsettled")
        except Exception as exc:  # noqa: BLE001 - a failed check is reported, not fatal
            self.alerts.record("warn", self._alert_src, f"reconciliation skipped: {exc}", now=now)
            return
        ours: dict[str, float] = {}
        for _name, trade in self.state.open_trades:
            if trade.filled and not trade.simulated:
                signed = trade.filled_count if trade.side == "yes" else -trade.filled_count
                ours[trade.ticker] = ours.get(trade.ticker, 0.0) + signed
        # a market the loop already booked can linger on the exchange until Kalshi
        # settles it a few minutes after close; that is not an unknown position
        booked = {t for ss in self.state.series.values() for t in ss.seen_tickers if t not in ours}
        exchange: dict[str, float] = {}
        for pos in positions:
            if abs(pos.position) < 1e-9 or pos.ticker.split("-")[0] not in self.cfg.series:
                continue
            if pos.ticker in booked:
                continue
            exchange[pos.ticker] = float(pos.position)
        problems: dict[str, str] = {}
        for ticker, qty in exchange.items():
            if ticker not in ours:
                problems[ticker] = (
                    f"exchange holds {qty:+.0f} on {ticker} that this loop did not open"
                )
            elif abs(ours[ticker] - qty) > 0.5:
                problems[ticker] = f"{ticker}: loop has {ours[ticker]:+.0f}, exchange {qty:+.0f}"
        for ticker, qty in ours.items():
            if ticker not in exchange:
                problems[ticker] = f"{ticker}: loop holds {qty:+.0f} but the exchange shows none"
        repeated = {t: p for t, p in problems.items() if t in self._mismatches}
        self._mismatches = problems
        if repeated:
            self.state.halted = "reconciliation mismatch: " + "; ".join(repeated.values())
            self.alerts.record("halt", self._alert_src, self.state.halted, now=now)
            return
        for text in problems.values():
            self.alerts.record(
                "warn",
                self._alert_src,
                f"reconciliation: {text} (halts if still there next check)",
                now=now,
            )
        if not problems:
            self.state.reconciled_ts = now

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
        outcome = self.strategy.signal(market, ss.last_side, now)
        if isinstance(outcome, Skip):
            self._skip(name, market.ticker, outcome.reason)
            self.decisions.record(
                now=now, strategy=self.strategy.name, series=name, market=market, outcome=outcome
            )
            return
        side, price = outcome.side, outcome.price
        first_side = ss.sides_traded.get(market.ticker)
        if first_side is not None and first_side != side and not self.cfg.allow_flip:
            why = f"would flip to {side} after buying {first_side} in this market"
            self._skip(name, market.ticker, why)
            self.decisions.record(
                now=now, strategy=self.strategy.name, series=name, market=market, outcome=Skip(why)
            )
            return
        count = self.cfg.size(
            price, getattr(self.strategy, "size_scale", 1.0), dollars=self.trade_dollars(market)
        )
        close_ts = market.close_time.timestamp() if market.close_time else now + 900
        post_price, maker = price, False
        if self.cfg.entry == "maker":
            bid = market.yes_bid if side == "yes" else market.no_bid
            inside = maker_price(bid, price)
            if inside is not None:
                post_price, maker = inside, True
        order = self.client.create_order(
            market.ticker,
            side=side,
            action="buy",
            count=count,
            price=post_price,
            order_type="limit",
        )
        trade = OpenTrade(
            ticker=market.ticker,
            side=side,
            count=count,
            limit_price=post_price,
            order_id=None if isinstance(order, DryRunOrder) else order.order_id,
            close_ts=close_ts,
            placed_ts=now,
            simulated=isinstance(order, DryRunOrder),
            prev_side=ss.last_side,
            maker=maker,
            taker_price=price,
        )
        if trade.simulated:
            trade.filled_count = float(count)
            trade.fill_price = post_price
            trade.fill_ts = now
        ss.open = trade
        ss.last_side = side
        ss.note_ticker(market.ticker)
        ss.sides_traded.setdefault(market.ticker, side)
        self.state.trades += 1
        self.state.save(self.cfg.state_file)
        self.decisions.record(
            now=now,
            strategy=self.strategy.name,
            series=name,
            market=market,
            outcome=outcome,
            count=count,
            order_id=trade.order_id,
        )
        log.info(
            "%sbuy %s x%d %s at %.3f%s (~$%.2f, %s): %s",
            "REAL MONEY: " if self.live else "",
            side,
            count,
            market.ticker,
            post_price,
            f" (maker; ask {price:.3f})" if maker else "",
            count * post_price,
            "simulated" if trade.simulated else f"order {trade.order_id}",
            outcome.reason,
        )

    def _pick_market(self, name: str, ss: SeriesState, now: float) -> Market | None:
        candidates = []
        for m in self.client.get_markets(series_ticker=name, status="open"):
            if m.close_time is None:
                continue
            if not ss.entries_allowed(m.ticker, self.cfg.max_entries, self.cfg.free_entries):
                continue
            last_exit = ss.last_exit_ts.get(m.ticker)
            if last_exit is not None and now - last_exit < self.cfg.reentry_cooloff_s:
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
        if trade is None:
            return
        if trade.filled:
            held = now - (trade.fill_ts if trade.fill_ts is not None else trade.placed_ts)
            if self.cfg.exits and now < trade.close_ts and held >= self.cfg.min_hold_s:
                self._maybe_exit(name, trade, now)
            return
        self._refresh_fills(trade)
        if trade.filled:
            trade.fill_ts = now
            if trade.filled_count + 0.001 < trade.count and trade.order_id is not None:
                # partial fill: cancel the resting remainder so nothing lingers
                try:
                    self.client.cancel_order(trade.order_id, ticker=trade.ticker)
                except Exception as exc:  # noqa: BLE001
                    log.warning("cancel remainder of %s failed: %s", trade.order_id, exc)
                self._refresh_fills(trade)
                trade.count = int(round(trade.filled_count))
            self.state.save(self.cfg.state_file)
            self.alerts.record(
                "info",
                self._alert_src,
                f"filled {trade.side.upper()} x{trade.filled_count:.0f} {trade.ticker} "
                f"at {trade.fill_price:.3f}{' (maker)' if trade.maker else ''}",
                now=now,
                ticker=trade.ticker,
            )
            return
        if now >= trade.close_ts - self.cfg.min_ttc:
            self._cancel_open(name, "unfilled inside the no-entry window")
            return
        if trade.maker and now - trade.placed_ts >= self.cfg.maker_wait_s:
            self._maker_to_taker(name, trade, now)

    def _maker_to_taker(self, name: str, trade: OpenTrade, now: float) -> None:
        """A maker order has rested long enough: cancel it and, if the strategy
        still wants the trade, take the current ask instead."""
        ss = self.state.for_series(name)
        if trade.order_id is not None:
            try:
                self.client.cancel_order(trade.order_id, ticker=trade.ticker)
            except Exception as exc:  # noqa: BLE001
                log.warning("cancel maker %s failed: %s", trade.order_id, exc)
            self._refresh_fills(trade)
            if trade.filled:
                log.info("maker order %s filled before cancel; holding", trade.order_id)
                trade.count = int(round(trade.filled_count))
                self.state.save(self.cfg.state_file)
                return
        try:
            market = self.client.get_market(trade.ticker)
        except Exception as exc:  # noqa: BLE001
            log.warning("quote for %s failed: %s", trade.ticker, exc)
            self._drop_unfilled(name, ss)
            return
        outcome = self.strategy.signal(market, trade.prev_side, now)
        if isinstance(outcome, Skip) or outcome.side != trade.side:
            log.info(
                "maker %s on %s unfilled and the signal is gone; dropping", trade.side, trade.ticker
            )
            self._drop_unfilled(name, ss)
            return
        order = self.client.create_order(
            trade.ticker,
            side=trade.side,
            action="buy",
            count=trade.count,
            price=outcome.price,
            order_type="limit",
        )
        trade.maker = False
        trade.limit_price = outcome.price
        trade.placed_ts = now
        trade.order_id = None if isinstance(order, DryRunOrder) else order.order_id
        trade.simulated = isinstance(order, DryRunOrder)
        if trade.simulated:
            trade.filled_count = float(trade.count)
            trade.fill_price = outcome.price
        self.state.save(self.cfg.state_file)
        log.info(
            "%smaker unfilled after %.0fs; taking %s x%d %s at %.3f (%s)",
            "REAL MONEY: " if self.live else "",
            self.cfg.maker_wait_s,
            trade.side,
            trade.count,
            trade.ticker,
            outcome.price,
            "simulated" if trade.simulated else f"order {trade.order_id}",
        )

    def _maybe_exit(self, name: str, trade: OpenTrade, now: float) -> None:
        """Ask the strategy whether to sell the filled position now, and do it."""
        try:
            market = self.client.get_market(trade.ticker)
        except Exception as exc:  # noqa: BLE001 - quotes can fail; try again next tick
            log.warning("quote for %s failed: %s", trade.ticker, exc)
            return
        entry = trade.fill_price if trade.fill_price is not None else trade.limit_price
        exit_rule = getattr(self.strategy, "exit", None)
        decision = exit_rule(market, trade.side, entry, now) if exit_rule else None
        if decision is None:
            return
        count = int(trade.filled_count)
        if count < 1:
            return
        order = self.client.create_order(
            trade.ticker,
            side=trade.side,
            action="sell",
            count=count,
            price=decision.price,
            order_type="limit",
            time_in_force="immediate_or_cancel",
        )
        if isinstance(order, DryRunOrder):
            sold, sell_price, order_id = float(count), decision.price, None
        else:
            order_id = order.order_id
            sold = max(0.0, order.count - order.remaining_count)
            sell_price = order.price or decision.price
            if sold <= 0:
                fills = [
                    f
                    for f in self.client.get_fills(ticker=trade.ticker, order_id=order_id)
                    if f.order_id == order_id
                ]
                sold = sum(f.count for f in fills)
                if sold > 0:
                    sell_price = sum(f.count * f.price for f in fills) / sold
        self.decisions.record(
            now=now,
            strategy=self.strategy.name,
            series=name,
            market=market,
            outcome=decision,
            count=int(sold),
            order_id=order_id,
        )
        if sold <= 0:
            log.info(
                "exit %s %s at %.3f did not fill; holding", trade.side, trade.ticker, decision.price
            )
            return
        self._book_sale(name, trade, sold, sell_price, now, decision.reason)

    def _book_sale(
        self, name: str, trade: OpenTrade, sold: float, sell_price: float, now: float, why: str
    ) -> None:
        entry = trade.fill_price if trade.fill_price is not None else trade.limit_price
        entry_fee_total = (
            trade.fee_paid if trade.fee_paid is not None else order_fee(entry, trade.filled_count)
        )
        entry_fee = entry_fee_total * (sold / trade.filled_count)
        sell_fee = order_fee(sell_price, sold)
        gross = sold * (sell_price - entry)
        net = gross - entry_fee - sell_fee
        s = self.state
        s.realized_pnl += net
        s.fees_paid += entry_fee + sell_fee
        s.wins += int(net > 0)
        s.losses += int(net <= 0)
        s.history.append(
            {
                "series": name,
                "ticker": trade.ticker,
                "side": trade.side,
                "count": sold,
                "price": entry,
                "sold_at": sell_price,
                "result": "sold",
                "won": net > 0,
                "net": round(net, 4),
                "settled_ts": now,
            }
        )
        del s.history[:-200]
        ss = s.for_series(name)
        ss.book(trade.ticker, net)
        ss.last_exit_ts[trade.ticker] = now
        self._book_result(net, now)
        remaining = trade.filled_count - sold
        if remaining > 0.001:
            trade.filled_count = remaining
            trade.count = int(round(remaining))
            if trade.fee_paid is not None:
                trade.fee_paid = entry_fee_total - entry_fee
        else:
            ss.open = None
        s.save(self.cfg.state_file)
        self.alerts.record(
            "info",
            self._alert_src,
            f"sold {trade.side.upper()} x{sold:.0f} {trade.ticker} at {sell_price:.3f} "
            f"(entry {entry:.3f}) -> {net:+.2f}; {why}",
            now=now,
            ticker=trade.ticker,
            net=round(net, 4),
        )
        log.info("%s%s", "REAL MONEY: " if self.live else "", s.summary())

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
        ss.book(trade.ticker, net)
        ss.open = None
        self._book_result(net, now)
        s.save(self.cfg.state_file)
        self.alerts.record(
            "info",
            self._alert_src,
            f"settled {trade.ticker} {market.result.upper()}: {trade.side.upper()} "
            f"x{trade.filled_count:.0f} at {price:.3f} -> {net:+.2f} (fee {fee:.2f})",
            now=now,
            ticker=trade.ticker,
            net=round(net, 4),
        )
        log.info("%s", s.summary())
