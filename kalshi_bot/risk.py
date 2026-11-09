"""Hard risk limits. Every order passes through ``RiskEngine.check`` first.

Nothing here is an alert: a failed check rejects the order. State lives in
SQLite so a restart cannot reset the daily loss cap or the halt flag.

Limits (all configurable in ``RiskLimits``):

* daily loss cap: realised P&L since 00:00 UTC below -cap halts trading until
  ``kalshi-bot risk-reset`` is run.
* max contracts per order and one open position per market.
* max fraction of bankroll committed across open positions.
* consecutive-loss breaker: N settled losses in a row pauses entries for a
  cooling-off period.
* no entry inside the last ``no_entry_seconds`` of a window.
* stale data: no entry if spot or book is older than ``max_data_age``.
* kill switch: the presence of ``kill_file`` blocks everything.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .storage import MarketDataStore


@dataclass(frozen=True)
class RiskLimits:
    bankroll: float = 200.0  # dollars; paper bankroll or live reference
    daily_loss_cap: float = 20.0  # dollars
    max_contracts_per_order: int = 5
    max_open_positions: int = 3
    max_bankroll_fraction: float = 0.25  # of bankroll committed at once
    consecutive_loss_limit: int = 4
    cooloff_seconds: float = 1800.0
    no_entry_seconds: float = 120.0
    max_data_age: float = 10.0
    kill_file: str = "state/KILL"


@dataclass(frozen=True)
class OrderIntent:
    ticker: str
    series: str
    side: str  # yes | no
    count: int
    price: float  # dollars per contract (what we pay)
    secs_to_close: float
    spot_age: float | None
    book_age: float | None


@dataclass
class Verdict:
    allowed: bool
    reason: str = "ok"
    adjusted_count: int | None = None


@dataclass
class RiskState:
    day: str
    day_pnl: float = 0.0
    consecutive_losses: int = 0
    halted: bool = False
    halt_reason: str = ""
    cooloff_until: float = 0.0
    notes: list[str] = field(default_factory=list)


def _today(now: float) -> str:
    return datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%d")


class RiskEngine:
    def __init__(self, store: MarketDataStore, limits: RiskLimits, mode: str) -> None:
        self.store = store
        self.limits = limits
        self.mode = mode  # paper | live; state is kept per mode
        self.state = self._load(time.time())

    # ------------------------------------------------------------ persistence

    def _key(self, name: str) -> str:
        return f"risk:{self.mode}:{name}"

    def _load(self, now: float) -> RiskState:
        """Load persisted state as-is; ``_roll_day`` resets day P&L when the UTC day changes."""
        get = self.store.get_meta
        st = RiskState(day=get(self._key("day")) or _today(now))
        st.day_pnl = float(get(self._key("day_pnl")) or 0.0)
        st.consecutive_losses = int(get(self._key("consecutive_losses")) or 0)
        st.halted = (get(self._key("halted")) or "0") == "1"
        st.halt_reason = get(self._key("halt_reason")) or ""
        st.cooloff_until = float(get(self._key("cooloff_until")) or 0.0)
        return st

    def _save(self) -> None:
        st = self.state
        self.store.set_meta(
            {
                self._key("day"): st.day,
                self._key("day_pnl"): f"{st.day_pnl:.4f}",
                self._key("consecutive_losses"): str(st.consecutive_losses),
                self._key("halted"): "1" if st.halted else "0",
                self._key("halt_reason"): st.halt_reason,
                self._key("cooloff_until"): f"{st.cooloff_until:.1f}",
            }
        )

    def _roll_day(self, now: float) -> None:
        day = _today(now)
        if day != self.state.day:
            self.state.day = day
            self.state.day_pnl = 0.0
            self._save()

    # ------------------------------------------------------------ events

    def record_settlement(self, pnl: float, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._roll_day(now)
        st = self.state
        st.day_pnl += pnl
        if pnl < 0:
            st.consecutive_losses += 1
        elif pnl > 0:
            st.consecutive_losses = 0
        if st.day_pnl <= -self.limits.daily_loss_cap and not st.halted:
            st.halted = True
            st.halt_reason = (
                f"daily loss cap hit ({st.day_pnl:+.2f} <= -{self.limits.daily_loss_cap})"
            )
        if st.consecutive_losses >= self.limits.consecutive_loss_limit:
            st.cooloff_until = now + self.limits.cooloff_seconds
            st.consecutive_losses = 0
        self._save()

    def halt(self, reason: str) -> None:
        self.state.halted = True
        self.state.halt_reason = reason
        self._save()

    def reset(self) -> None:
        """Manual reset: clears the halt, the cool-off and the loss streak. Day P&L stays."""
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.cooloff_until = 0.0
        self.state.consecutive_losses = 0
        self._save()

    # ------------------------------------------------------------ the check

    def check(
        self,
        intent: OrderIntent,
        open_positions: list[dict],
        now: float | None = None,
    ) -> Verdict:
        now = time.time() if now is None else now
        self._roll_day(now)
        lim, st = self.limits, self.state
        if Path(lim.kill_file).exists():
            return Verdict(False, f"kill switch present: {lim.kill_file}")
        if st.halted:
            return Verdict(False, f"halted: {st.halt_reason}")
        if now < st.cooloff_until:
            return Verdict(
                False, f"cooling off after loss streak for {st.cooloff_until - now:.0f}s"
            )
        if st.day_pnl <= -lim.daily_loss_cap:
            self.halt(f"daily loss cap hit ({st.day_pnl:+.2f})")
            return Verdict(False, st.halt_reason)
        if intent.side not in ("yes", "no"):
            return Verdict(False, f"bad side {intent.side}")
        if not 0 < intent.price < 1:
            return Verdict(False, f"bad price {intent.price}")
        if intent.secs_to_close < lim.no_entry_seconds:
            return Verdict(False, f"inside no-entry window ({intent.secs_to_close:.0f}s to close)")
        if intent.spot_age is None or intent.spot_age > lim.max_data_age:
            return Verdict(False, f"spot stale ({intent.spot_age})")
        if intent.book_age is None or intent.book_age > lim.max_data_age:
            return Verdict(False, f"book stale ({intent.book_age})")
        if any(p["ticker"] == intent.ticker for p in open_positions):
            return Verdict(False, "already have a position in this market")
        if len(open_positions) >= lim.max_open_positions:
            return Verdict(False, f"max open positions ({lim.max_open_positions}) reached")
        committed = sum(p["count"] * p["price"] for p in open_positions)
        budget = lim.bankroll * lim.max_bankroll_fraction - committed
        if budget <= 0:
            return Verdict(False, f"bankroll fraction limit reached (committed {committed:.2f})")
        count = min(intent.count, lim.max_contracts_per_order, int(budget // intent.price))
        if count < 1:
            return Verdict(False, f"budget {budget:.2f} buys no contracts at {intent.price}")
        return Verdict(True, "ok", adjusted_count=count)
