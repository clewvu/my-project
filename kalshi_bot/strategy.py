"""Strategies for the trading loop, and the decision log they write.

A strategy answers one question per open market per tick: buy which side at
what price, or stay out, and why. The loop owns everything else (sizing,
caps, fills, settlement).

Two strategies exist:

* ``alternate``: YES then NO then YES on successive markets. No edge; it is
  the plumbing test from the first live run.
* ``fairvalue``: the model from docs/research-brief.md section 3, live. Spot
  from Coinbase, realised volatility over the last 30 minutes, fair value
  p = Phi(ln(S/K) / (sigma sqrt(tau))). Buy the side whose fair value beats
  its ask by the taker fee plus a margin; otherwise stay out, which is most
  of the time. Guards: no spot older than ``spot_stale_s``, no sigma until at
  least half the vol window has data, no ask above ``max_price``.

The decision log (``state/decisions.jsonl``) gets one JSON line per trade and
one per skip reason change, with every input the strategy used: spot,
strike, sigma, seconds to close, both asks, model probability, edges. It is
the record for judging the model afterwards against what actually settled.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .fees import fee_per_contract
from .models import Market

log = logging.getLogger(__name__)

SPOT_SYMBOLS = {
    "KXBTC15M": "BTC-USD",
    "KXETH15M": "ETH-USD",
    "KXSOL15M": "SOL-USD",
    "KXDOGE15M": "DOGE-USD",
    "KXXRP15M": "XRP-USD",
}
SETTLEMENT_WINDOW_S = 60.0
VOL_STEP_S = 5.0
VOL_MIN_FRACTION = 0.5


# ---------------------------------------------------------------- results


@dataclass
class Signal:
    side: str  # yes | no
    price: float  # the ask to pay, dollars
    reason: str
    edge: float | None = None  # fair value minus ask minus fee, when a model produced it
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass
class Skip:
    reason: str
    inputs: dict[str, Any] = field(default_factory=dict)


class Strategy(Protocol):
    name: str

    def prepare(self, now: float) -> None:
        """Called once per tick before any market is looked at."""

    def signal(self, market: Market, last_side: str | None, now: float) -> Signal | Skip: ...


# ---------------------------------------------------------------- math (scalar)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def effective_tau(secs_to_close: float) -> float:
    """Variance-equivalent horizon for the one-minute settlement average."""
    t = float(secs_to_close)
    if t >= SETTLEMENT_WINDOW_S:
        tau = (t - SETTLEMENT_WINDOW_S) + SETTLEMENT_WINDOW_S / 3.0
    else:
        tau = max(t, 0.0) / 3.0
    return max(tau, 1.0)


def fair_value(spot: float, strike: float, sigma: float, secs_to_close: float) -> float:
    if spot <= 0 or strike <= 0:
        return float("nan")
    if sigma <= 0:
        return 1.0 if spot >= strike else 0.0
    z = math.log(spot / strike) / (sigma * math.sqrt(effective_tau(secs_to_close)))
    return norm_cdf(z)


# ---------------------------------------------------------------- spot history


class SpotHistory:
    """Rolling spot prices per symbol, enough for a realised-vol estimate."""

    def __init__(self, keep_s: float = 7200.0) -> None:
        self.keep_s = keep_s
        self._points: dict[str, deque[tuple[float, float]]] = {}

    def push(self, symbol: str, ts: float, price: float) -> None:
        if price <= 0:
            return
        q = self._points.setdefault(symbol, deque())
        if q and ts <= q[-1][0]:
            return
        q.append((ts, price))
        cutoff = ts - self.keep_s
        while q and q[0][0] < cutoff:
            q.popleft()

    def latest(self, symbol: str) -> tuple[float, float] | None:
        q = self._points.get(symbol)
        return q[-1] if q else None

    def sigma(
        self,
        symbol: str,
        window_s: float,
        now: float,
        step_s: float = VOL_STEP_S,
        min_fraction: float = VOL_MIN_FRACTION,
    ) -> float | None:
        """RMS of ``step_s``-second log returns over the last ``window_s`` seconds,
        per square-root second; None until at least ``min_fraction`` of the
        window has returns. Same estimator as ``fairvalue.realized_vol``."""
        q = self._points.get(symbol)
        if not q:
            return None
        start = now - window_s
        pts = [(t, p) for t, p in q if t >= start]
        if len(pts) < 3:
            return None
        n_steps = int(window_s / step_s)
        grid_t = start + step_s
        idx = 0
        last: float | None = None
        last_t = -math.inf
        prev_log: float | None = None
        sq = 0.0
        count = 0
        for _ in range(n_steps):
            while idx < len(pts) and pts[idx][0] <= grid_t:
                last, last_t = pts[idx][1], pts[idx][0]
                idx += 1
            fresh = last is not None and grid_t - last_t <= 3 * step_s
            cur_log = math.log(last) if fresh and last is not None else None
            if cur_log is not None and prev_log is not None:
                sq += (cur_log - prev_log) ** 2
                count += 1
            prev_log = cur_log
            grid_t += step_s
        if count < max(2, int(n_steps * min_fraction)):
            return None
        return math.sqrt(sq / count / step_s)

    def bootstrap_from_db(self, db_path: str | Path, symbol: str, since: float) -> int:
        """Seed history from the recorder's ``spot`` table. Returns rows loaded."""
        path = Path(db_path)
        if not path.exists():
            return 0
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "SELECT ts, price FROM spot WHERE symbol = ? AND ts >= ? ORDER BY ts",
                    (symbol, since),
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error as exc:
            log.warning("spot bootstrap from %s failed: %s", path, exc)
            return 0
        for ts, price in rows:
            self.push(symbol, float(ts), float(price))
        return len(rows)


# ---------------------------------------------------------------- strategies


class AlternatingStrategy:
    name = "alternate"

    def __init__(self, first_side: str = "yes", max_price: float = 0.60) -> None:
        self.first_side = first_side
        self.max_price = max_price

    def prepare(self, now: float) -> None:
        return None

    def signal(self, market: Market, last_side: str | None, now: float) -> Signal | Skip:
        side = self.first_side if last_side is None else ("no" if last_side == "yes" else "yes")
        price = market.yes_ask if side == "yes" else market.no_ask
        if price is None:
            return Skip(f"no {side} ask")
        if price > self.max_price:
            return Skip(f"{side} ask {price:.3f} above max_price {self.max_price}")
        return Signal(side=side, price=price, reason="alternate", inputs={"ask": price})


class FairValueStrategy:
    name = "fairvalue"

    def __init__(
        self,
        spot_feed: Any,  # object with fetch() -> {symbol: price}
        *,
        margin: float = 0.02,
        vol_window_s: float = 1800.0,
        max_price: float = 0.60,
        spot_stale_s: float = 10.0,
        taker_rate: float | None = None,
        history: SpotHistory | None = None,
        clock: Any = time.time,
    ) -> None:
        self.feed = spot_feed
        self.margin = margin
        self.vol_window_s = vol_window_s
        self.max_price = max_price
        self.spot_stale_s = spot_stale_s
        self.taker_rate = taker_rate
        self.history = history or SpotHistory(keep_s=max(7200.0, 2 * vol_window_s))
        self.clock = clock
        self.last_fetch_ts: float | None = None

    def bootstrap(self, db_path: str | Path, series: tuple[str, ...], now: float) -> None:
        for name in series:
            symbol = SPOT_SYMBOLS.get(name)
            if symbol:
                n = self.history.bootstrap_from_db(db_path, symbol, now - 2 * self.vol_window_s)
                if n:
                    log.info("%s: seeded %d spot rows from %s", symbol, n, db_path)

    def prepare(self, now: float) -> None:
        try:
            prices = self.feed.fetch()
        except Exception as exc:  # noqa: BLE001 - a bad tick must not stop the loop
            log.warning("spot fetch failed: %s", exc)
            return
        for symbol, price in prices.items():
            self.history.push(symbol, now, float(price))
        self.last_fetch_ts = now

    def evaluate(self, market: Market, now: float) -> dict[str, Any]:
        """All model inputs and outputs for one market, without deciding."""
        out: dict[str, Any] = {
            "ticker": market.ticker,
            "yes_ask": market.yes_ask,
            "no_ask": market.no_ask,
            "strike": market.strike,
        }
        symbol = SPOT_SYMBOLS.get(market.series_ticker)
        latest = self.history.latest(symbol) if symbol else None
        if latest is None:
            out["skip"] = "no spot"
            return out
        spot_ts, spot = latest
        out["spot"] = spot
        out["spot_age_s"] = round(now - spot_ts, 1)
        if now - spot_ts > self.spot_stale_s:
            out["skip"] = f"spot {now - spot_ts:.0f}s old"
            return out
        if market.strike is None or market.close_time is None:
            out["skip"] = "market has no strike or close time"
            return out
        ttc = market.close_time.timestamp() - now
        out["secs_to_close"] = round(ttc, 1)
        sigma = self.history.sigma(symbol, self.vol_window_s, now)  # type: ignore[arg-type]
        if sigma is None:
            out["skip"] = "not enough spot history for volatility yet"
            return out
        out["sigma"] = sigma
        out["ann_vol"] = sigma * math.sqrt(365 * 86400)
        p = fair_value(spot, market.strike, sigma, ttc)
        out["p_yes"] = p
        out["spot_vs_strike_bps"] = (spot - market.strike) / market.strike * 1e4
        for side, ask, p_side in (("yes", market.yes_ask, p), ("no", market.no_ask, 1 - p)):
            if ask is None:
                continue
            fee = (
                fee_per_contract(ask, self.taker_rate)
                if self.taker_rate is not None
                else fee_per_contract(ask)
            )
            out[f"edge_{side}"] = p_side - ask - fee
        return out

    def signal(self, market: Market, last_side: str | None, now: float) -> Signal | Skip:
        ev = self.evaluate(market, now)
        if "skip" in ev:
            return Skip(ev["skip"], inputs=ev)
        candidates = [
            (ev[f"edge_{side}"], side)
            for side in ("yes", "no")
            if f"edge_{side}" in ev and ev[f"edge_{side}"] is not None
        ]
        if not candidates:
            return Skip("no asks", inputs=ev)
        edge, side = max(candidates)
        ask = market.yes_ask if side == "yes" else market.no_ask
        assert ask is not None
        if edge < self.margin:
            return Skip(f"best edge {edge:+.3f} ({side}) below margin {self.margin:.3f}", inputs=ev)
        if ask > self.max_price:
            return Skip(f"{side} ask {ask:.3f} above max_price {self.max_price}", inputs=ev)
        return Signal(
            side=side,
            price=ask,
            edge=edge,
            reason=f"fair value {ev['p_yes']:.3f} vs {side} ask {ask:.3f}, edge {edge:+.3f}",
            inputs=ev,
        )


# ---------------------------------------------------------------- decision log


class DecisionLog:
    """Append-only JSON lines: every trade, and every change of skip reason
    per market, with the strategy's inputs. Doubles as the feature store."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self._last_skip: dict[str, str] = {}

    def record(
        self,
        *,
        now: float,
        strategy: str,
        series: str,
        market: Market,
        outcome: Signal | Skip,
        count: int | None = None,
        order_id: str | None = None,
    ) -> None:
        if self.path is None:
            return
        if isinstance(outcome, Skip):
            if self._last_skip.get(market.ticker) == outcome.reason:
                return
            self._last_skip[market.ticker] = outcome.reason
        row: dict[str, Any] = {
            "ts": now,
            "time": datetime.fromtimestamp(now, tz=UTC).isoformat(timespec="seconds"),
            "strategy": strategy,
            "series": series,
            "ticker": market.ticker,
            "action": "trade" if isinstance(outcome, Signal) else "skip",
            "reason": outcome.reason,
        }
        if isinstance(outcome, Signal):
            row.update({"side": outcome.side, "price": outcome.price, "edge": outcome.edge})
            row["count"] = count
            row["order_id"] = order_id
        row["inputs"] = {k: v for k, v in outcome.inputs.items() if k != "ticker"}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except OSError as exc:
            log.warning("decision log write failed: %s", exc)


def build_strategy(
    name: str,
    *,
    first_side: str = "yes",
    max_price: float = 0.60,
    margin: float = 0.02,
    vol_window_s: float = 1800.0,
    spot_feed: Any = None,
    spot_db: str | Path | None = None,
    series: tuple[str, ...] = (),
    now: float | None = None,
) -> Strategy:
    if name == "alternate":
        return AlternatingStrategy(first_side=first_side, max_price=max_price)
    if name == "fairvalue":
        if spot_feed is None:
            from .spot import SpotFeed

            symbols = [SPOT_SYMBOLS[s] for s in series if s in SPOT_SYMBOLS]
            spot_feed = SpotFeed(symbols)
        strat = FairValueStrategy(
            spot_feed, margin=margin, vol_window_s=vol_window_s, max_price=max_price
        )
        if spot_db is not None:
            strat.bootstrap(spot_db, series, now if now is not None else time.time())
        return strat
    raise ValueError(f"unknown strategy {name!r}; use alternate or fairvalue")
