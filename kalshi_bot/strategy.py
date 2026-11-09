"""Strategies: given a market snapshot and live spot, return at most one order intent.

The fair-value strategy prices a 15-minute up/down market from spot and
realized volatility:

    z      = ln(S / K) / (sigma_per_second * sqrt(tau))
    p_up   = Phi(z)

S is current spot, K the strike (the market's floor_strike), tau the seconds
until the middle of the settlement minute, and sigma the realized volatility of
spot over the last ``vol_window`` seconds. It buys YES when
``p_up - yes_ask > fee + margin`` and NO when ``(1 - p_up) - no_ask > fee + margin``.

Everything is deterministic and explainable; the full feature vector goes to
the decision log whether or not an order results.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from .fees import fee_per_contract
from .models import Market, Orderbook

SETTLEMENT_AVERAGE_S = 60.0  # settlement is the mean over the final minute


def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


class SpotHistory:
    """Rolling spot history per symbol for realized-vol estimation."""

    def __init__(self, max_seconds: float = 3600.0) -> None:
        self.max_seconds = max_seconds
        self._data: dict[str, deque[tuple[float, float]]] = {}

    def push(self, symbol: str, ts: float, price: float) -> None:
        d = self._data.setdefault(symbol, deque())
        if d and ts <= d[-1][0]:
            return
        d.append((ts, price))
        cutoff = ts - self.max_seconds
        while d and d[0][0] < cutoff:
            d.popleft()

    def seed(self, symbol: str, rows: list[tuple[float, float]]) -> None:
        for ts, price in sorted(rows):
            self.push(symbol, ts, price)

    def latest(self, symbol: str) -> tuple[float, float] | None:
        d = self._data.get(symbol)
        return d[-1] if d else None

    def realized_vol(
        self, symbol: str, window: float = 1800.0, grid: float = 10.0, now: float | None = None
    ) -> float | None:
        """Per-second volatility of log price from ``grid``-second returns over ``window``."""
        d = self._data.get(symbol)
        if not d or len(d) < 10:
            return None
        now = d[-1][0] if now is None else now
        start = now - window
        # sample on a grid: last price at or before each grid point
        pts = [(ts, p) for ts, p in d if ts >= start - grid]
        if len(pts) < 10:
            return None
        series: list[float] = []
        t = start
        i = 0
        last: float | None = None
        while t <= now:
            while i < len(pts) and pts[i][0] <= t:
                last = pts[i][1]
                i += 1
            if last is not None:
                series.append(math.log(last))
            t += grid
        if len(series) < 6:
            return None
        rets = [b - a for a, b in zip(series, series[1:], strict=False)]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
        return math.sqrt(var) / math.sqrt(grid)


@dataclass(frozen=True)
class Intent:
    side: str
    count: int
    price: float  # the ask we will pay
    edge: float  # fair prob of our side minus price minus fee


@dataclass
class Decision:
    action: str  # order | skip
    reason: str
    intent: Intent | None
    features: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FairValueParams:
    margin: float = 0.03  # required edge beyond fees, in probability points
    count: int = 2  # contracts per order before risk sizing
    vol_window: float = 1800.0
    min_sigma: float = 1e-6
    max_price: float = 0.90  # never pay more than this
    min_price: float = 0.05  # never buy lottery tickets below this
    taker_rate: float = 0.07
    max_spot_age: float = 10.0


class FairValueStrategy:
    name = "fair_value"

    def __init__(self, params: FairValueParams | None = None) -> None:
        self.params = params or FairValueParams()

    def decide(
        self,
        market: Market,
        book: Orderbook,
        spot: float | None,
        spot_ts: float | None,
        sigma: float | None,
        now: float,
    ) -> Decision:
        p = self.params
        secs = (
            market.seconds_to_close()
            if market.close_time is None
            else (market.close_time.timestamp() - now)
        )
        yes_ask, no_ask = book.best_yes_ask, book.best_no_ask
        feats: dict = {
            "secs_to_close": secs,
            "spot": spot,
            "strike": market.strike,
            "sigma": sigma,
            "yes_ask": yes_ask,
            "no_ask": no_ask,
            "yes_bid": book.best_yes_bid,
            "no_bid": book.best_no_bid,
            "spot_age": (now - spot_ts) if spot_ts else None,
        }
        if market.strike is None or spot is None or spot_ts is None:
            return Decision("skip", "missing strike or spot", None, feats)
        if now - spot_ts > p.max_spot_age:
            return Decision("skip", f"spot stale {now - spot_ts:.1f}s", None, feats)
        if sigma is None or sigma < p.min_sigma:
            return Decision("skip", "no volatility estimate yet", None, feats)
        if yes_ask is None or no_ask is None:
            return Decision("skip", "one-sided or empty book", None, feats)
        tau = max(1.0, secs - SETTLEMENT_AVERAGE_S / 2)
        z = math.log(spot / market.strike) / (sigma * math.sqrt(tau))
        p_up = norm_cdf(z)
        feats.update({"tau": tau, "z": z, "fair_p": p_up})
        candidates = []
        for side, ask, fair in (("yes", yes_ask, p_up), ("no", no_ask, 1 - p_up)):
            if not p.min_price <= ask <= p.max_price:
                continue
            edge = fair - ask - fee_per_contract(ask, p.taker_rate)
            candidates.append((edge, side, ask))
        if not candidates:
            return Decision("skip", "asks outside allowed price band", None, feats)
        edge, side, ask = max(candidates)
        feats.update({"best_side": side, "best_edge": edge})
        if edge < p.margin:
            return Decision("skip", f"edge {edge:+.3f} below margin {p.margin}", None, feats)
        return Decision("order", f"edge {edge:+.3f}", Intent(side, p.count, ask, edge), feats)
