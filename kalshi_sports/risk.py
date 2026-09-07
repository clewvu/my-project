"""Risk engine: hard limits between the strategy and the exchange, persisted in SQLite.

Counters live in the store's ``limits`` table so a restart cannot reset them.
The daily loss cap is keyed by the Eastern calendar date; the consecutive-loss
breaker pauses entries for a cooling period; the kill switch is a file
(``state/SPORTS_STOP``) and a pause file (``state/SPORTS_PAUSE``: keep booking
results, open nothing new). The crypto loop has its own ``state/STOP`` and
``state/PAUSE``; neither loop reacts to the other's files. Size is bounded per
trade, per game and as a share of bankroll.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .catalog import date_from_ts
from .storage import SportsDataStore

LIVE_MAX_DOLLARS = 20.0
LIVE_MAX_LOSS_CAP = 50.0


@dataclass(frozen=True)
class RiskLimits:
    daily_loss_cap: float = 25.0  # realised loss per Eastern day, then no new entries
    total_loss_cap: float = 50.0  # realised loss since the counters were reset
    max_trade_dollars: float = 10.0
    max_event_dollars: float = 10.0
    max_open_positions: int = 10
    max_open_dollars: float = 60.0
    max_bankroll_share: float = 0.05  # per trade
    max_consecutive_losses: int = 4
    loss_pause_s: float = 6 * 3600
    stop_file: str = "state/SPORTS_STOP"  # the crypto loop uses state/STOP; keep them apart
    pause_file: str = "state/SPORTS_PAUSE"


@dataclass(frozen=True)
class Intent:
    ticker: str
    event_ticker: str | None
    dollars: float
    mode: str


class RiskEngine:
    def __init__(self, store: SportsDataStore, limits: RiskLimits, mode: str) -> None:
        self.store = store
        self.limits = limits
        self.mode = mode
        self._key = f"{mode}:"

    def rekey(self, mode: str) -> None:
        """Switch the counters to another mode's namespace (dry-run runs of live)."""
        self.mode = mode
        self._key = f"{mode}:"

    # ------------------------------------------------------------ counters

    def _get(self, key: str, default: float = 0.0) -> float:
        v = self.store.limit_get(self._key + key)
        return float(v) if v is not None else default

    def _set(self, key: str, value: float | str) -> None:
        self.store.limit_set(self._key + key, value)

    def _roll_day(self, now: float) -> None:
        today = date_from_ts(now)
        if self.store.limit_get(self._key + "day") != today:
            self._set("day", today)
            self._set("daily_net", 0.0)

    def daily_net(self, now: float) -> float:
        self._roll_day(now)
        return self._get("daily_net")

    def total_net(self) -> float:
        return self._get("total_net")

    def loss_streak(self) -> int:
        return int(self._get("loss_streak"))

    def breaker_until(self) -> float:
        return self._get("breaker_until")

    def record_result(self, net: float, now: float) -> str | None:
        """Book a settled result. Returns a note when a breaker trips."""
        self._roll_day(now)
        self._set("daily_net", self._get("daily_net") + net)
        self._set("total_net", self._get("total_net") + net)
        if net < 0:
            streak = self.loss_streak() + 1
            self._set("loss_streak", streak)
            if streak >= self.limits.max_consecutive_losses:
                self._set("breaker_until", now + self.limits.loss_pause_s)
                self._set("loss_streak", 0)
                hours = self.limits.loss_pause_s / 3600
                return f"{streak} consecutive losses: pausing entries for {hours:.0f}h"
        else:
            self._set("loss_streak", 0)
        return None

    def reset_totals(self) -> None:
        for key in ("total_net", "loss_streak", "breaker_until"):
            self._set(key, 0.0)

    # ------------------------------------------------------------ switches

    def stop_requested(self) -> bool:
        return Path(self.limits.stop_file).exists()

    def paused(self) -> bool:
        return Path(self.limits.pause_file).exists()

    # ------------------------------------------------------------ the check

    def check(self, intent: Intent, now: float, bankroll: float | None) -> str | None:
        """None if the order may go; otherwise the reason it may not."""
        lim = self.limits
        if self.stop_requested():
            return "stop file present"
        if self.paused():
            return "paused"
        if now < self.breaker_until():
            return "loss breaker active"
        if self.daily_net(now) <= -lim.daily_loss_cap:
            return "daily loss cap reached"
        if self.total_net() <= -lim.total_loss_cap:
            return "total loss cap reached"
        if intent.dollars > lim.max_trade_dollars + 1e-9:
            return "trade exceeds per-trade cap"
        if bankroll is not None and intent.dollars > bankroll * lim.max_bankroll_share + 1e-9:
            return "trade exceeds bankroll share"
        if bankroll is not None and intent.dollars > bankroll:
            return "insufficient balance"
        exposure = self.store.event_exposure(intent.event_ticker, self.mode)
        if exposure + intent.dollars > lim.max_event_dollars + 1e-9:
            return "game exposure cap reached"
        open_rows = self.store.positions(status="open", mode=self.mode)
        if len(open_rows) >= lim.max_open_positions:
            return "too many open positions"
        if sum(r["dollars"] for r in open_rows) + intent.dollars > lim.max_open_dollars + 1e-9:
            return "open exposure cap reached"
        return None

    def describe(self, now: float) -> str:
        return (
            f"day {self.daily_net(now):+.2f}/{-self.limits.daily_loss_cap:.0f}, "
            f"total {self.total_net():+.2f}/{-self.limits.total_loss_cap:.0f}, "
            f"streak {self.loss_streak()}"
            + (", breaker on" if now < self.breaker_until() else "")
            + (", PAUSED" if self.paused() else "")
        )
