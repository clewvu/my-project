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
from .models import Market, Orderbook, price_tick

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

    def span(self, symbol: str) -> float:
        """Seconds of history held for a symbol."""
        d = self._data.get(symbol)
        return d[-1][0] - d[0][0] if d else 0.0

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
    price: float  # limit price we post or pay
    edge: float  # fair prob of our side minus price minus fee
    exec_mode: str = "taker"  # taker | maker
    fair: float = 0.0  # fair prob of our side at decision time


def kelly_count(
    fair: float, price: float, fee: float, fraction: float, bankroll: float, cap: int
) -> int:
    """Contracts from a fraction of the Kelly stake for a binary bet at ``price``.

    Full Kelly fraction of bankroll for a bet paying 1 at probability ``fair``:
    f* = (fair - cost) / (1 - cost) with cost = price + fee. Clamped to [0, cap].
    """
    cost = price + fee
    if cost >= 1 or fair <= cost or fraction <= 0:
        return 0
    f_star = (fair - cost) / (1 - cost)
    dollars = fraction * f_star * bankroll
    return max(0, min(cap, int((dollars + 1e-9) // cost)))


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
    dollars: float = 0.0  # if > 0: stake per order; contracts = floor(dollars / ask)
    vol_window: float = 1800.0
    min_sigma: float = 1e-6
    max_price: float = 0.90  # never pay more than this
    min_price: float = 0.05  # never buy lottery tickets below this
    taker_rate: float = 0.07
    max_spot_age: float = 10.0
    # Calibration map learned offline by `kalshi-bot learn`:
    #   logit(p_cal) = cal_a + cal_b * logit(p_model). Identity by default.
    cal_a: float = 0.0
    cal_b: float = 1.0
    # Execution: "taker" crosses the spread at the ask; "maker" rests one tick inside
    # the spread (or joins the bid) and pays no fee; "hybrid" posts as maker and falls
    # back to taking if the resting order expires unfilled while the edge persists.
    exec_mode: str = "maker"
    maker_rate: float = 0.0
    rest_seconds: float = 20.0
    max_spread: float = 0.04  # skip books wider than this (dollars)
    # Sizing: 0 = fixed `count`; otherwise fraction of Kelly on the fee-adjusted edge,
    # converted to contracts against `bankroll`. Risk limits still cap the result.
    kelly_fraction: float = 0.0
    bankroll: float = 200.0
    max_kelly_count: int = 10
    # Model shaping, both learnable by `kalshi-bot learn`:
    #   favourites_only: never buy a side priced under 0.50 (the data showed the model
    #   overestimates cheap sides); sigma_scale: multiply realized vol (< 1 sharpens).
    favourites_only: bool = False
    sigma_scale: float = 1.0
    # Strategy-level time filter (the risk engine's no-entry window still applies on top).
    min_secs_to_close: float = 0.0

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, d: dict) -> FairValueParams:
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def calibrate(p: float, a: float, b: float) -> float:
    p = min(1 - 1e-6, max(1e-6, p))
    z = a + b * math.log(p / (1 - p))
    return 1 / (1 + math.exp(-z))


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
        force_taker: bool = False,
        *,
        settlement_average_s: float = SETTLEMENT_AVERAGE_S,
        extra_margin: float = 0.0,
    ) -> Decision:
        """``settlement_average_s`` is 60 for crypto (one-minute average) and 0 for
        series that settle on a single candle close; ``extra_margin`` is added to the
        required edge when spot is a proxy rather than the settlement feed."""
        p = self.params
        margin = p.margin + extra_margin
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
        if secs is not None and secs < p.min_secs_to_close:
            return Decision("skip", f"{secs:.0f}s to close < {p.min_secs_to_close:g}", None, feats)
        if now - spot_ts > p.max_spot_age:
            return Decision("skip", f"spot stale {now - spot_ts:.1f}s", None, feats)
        if sigma is None or sigma < p.min_sigma:
            return Decision("skip", "no volatility estimate yet", None, feats)
        if yes_ask is None or no_ask is None:
            return Decision("skip", "one-sided or empty book", None, feats)
        tau = max(1.0, secs - settlement_average_s / 2)
        sigma_eff = sigma * p.sigma_scale
        z = math.log(spot / market.strike) / (sigma_eff * math.sqrt(tau))
        p_model = norm_cdf(z)
        p_up = calibrate(p_model, p.cal_a, p.cal_b)
        feats.update(
            {
                "tau": tau,
                "z": z,
                "fair_p_model": p_model,
                "fair_p": p_up,
                "cal_a": p.cal_a,
                "cal_b": p.cal_b,
            }
        )
        spread = round(yes_ask - (book.best_yes_bid or 0.0), 4) if book.best_yes_bid else None
        feats["spread"] = spread
        if spread is not None and spread > p.max_spread:
            return Decision("skip", f"spread {spread:.3f} wider than {p.max_spread}", None, feats)
        mode = "taker" if p.exec_mode == "taker" or force_taker else "maker"
        candidates = []
        for side, ask, bid, fair in (
            ("yes", yes_ask, book.best_yes_bid, p_up),
            ("no", no_ask, book.best_no_bid, 1 - p_up),
        ):
            if mode == "maker":
                # improve the best bid by one tick; if that would cross, join the bid
                tick = price_tick(bid if bid is not None else ask)
                base = bid if bid is not None else round(ask - tick, 4)
                # improve the bid by one tick if that still sits below the ask, else join it
                price = round(base + tick, 4) if ask - base > tick + 1e-9 else base
                if price is None or price <= 0:
                    continue
                fee = fee_per_contract(price, p.maker_rate)
            else:
                price = ask
                fee = fee_per_contract(price, p.taker_rate)
            if not p.min_price <= price <= p.max_price:
                continue
            edge = fair - price - fee
            candidates.append((edge, side, price, fee, fair))
        if not candidates:
            return Decision("skip", "prices outside allowed band", None, feats)
        edge, side, price, fee, fair = max(candidates)
        feats.update({"best_side": side, "best_edge": edge, "exec_mode": mode, "post_price": price})
        if edge < margin:
            return Decision("skip", f"edge {edge:+.3f} below margin {margin:g}", None, feats)
        if p.favourites_only and price < 0.5:
            return Decision("skip", f"underdog at {price:.2f}; favourites only", None, feats)
        if p.dollars > 0:
            count = max(1, int(p.dollars / price + 1e-9))
        elif p.kelly_fraction > 0:
            count = kelly_count(fair, price, fee, p.kelly_fraction, p.bankroll, p.max_kelly_count)
            feats["kelly_count"] = count
            if count < 1:
                return Decision("skip", f"edge {edge:+.3f} too small for one contract", None, feats)
        else:
            count = p.count
        return Decision(
            "order", f"{mode} edge {edge:+.3f}", Intent(side, count, price, edge, mode, fair), feats
        )
