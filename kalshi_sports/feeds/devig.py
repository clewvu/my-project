"""Odds conversions and de-vigging.

A sportsbook's quoted prices on the outcomes of one market sum to more than
one (the overround, or vig). De-vigging removes it to recover the book's
implied probabilities. Three standard methods; which one fits Kalshi best is
a fitted choice in the research stage, not a decision made here.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def american_to_decimal(odds: float) -> float:
    if odds == 0:
        raise ValueError("american odds cannot be 0")
    return 1 + odds / 100 if odds > 0 else 1 + 100 / abs(odds)


def decimal_to_american(dec: float) -> float:
    if dec <= 1:
        raise ValueError("decimal odds must exceed 1")
    return (dec - 1) * 100 if dec >= 2 else -100 / (dec - 1)


def implied(dec: float) -> float:
    """Raw implied probability (includes the book's margin)."""
    if dec <= 1:
        raise ValueError("decimal odds must exceed 1")
    return 1 / dec


def overround(decimals: Sequence[float]) -> float:
    """Sum of raw implied probabilities minus one; 0.045 means a 4.5% margin."""
    return sum(implied(d) for d in decimals) - 1


def devig_multiplicative(decimals: Sequence[float]) -> list[float]:
    """Scale every implied probability by the same factor so they sum to one."""
    raw = [implied(d) for d in decimals]
    total = sum(raw)
    return [r / total for r in raw]


def devig_power(decimals: Sequence[float], tol: float = 1e-10, iters: int = 100) -> list[float]:
    """Raise every implied probability to the power k with sum(p^k) = 1.

    Favourites keep more of their probability than in the multiplicative
    method, which matches how books shade longshots.
    """
    raw = [implied(d) for d in decimals]
    if sum(raw) <= 1.0:  # no margin to remove (or an arbitrage); fall back to scaling
        return devig_multiplicative(decimals)
    lo, hi = 0.5, 5.0
    for _ in range(iters):
        k = (lo + hi) / 2
        s = sum(r**k for r in raw)
        if abs(s - 1) < tol:
            break
        if s > 1:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    return [r**k for r in raw]


def devig_shin(decimals: Sequence[float], tol: float = 1e-10, iters: int = 100) -> list[float]:
    """Shin (1993): the margin is set against insider traders, which shades longshots most.

    Solves for z (the insider share) such that the corrected probabilities sum to one.
    """
    raw = [implied(d) for d in decimals]
    total = sum(raw)
    if len(raw) < 2:
        return [1.0]
    if total <= 1.0:
        return devig_multiplicative(decimals)
    lo, hi = 0.0, min(0.5, total - 1)

    def probs(z: float) -> list[float]:
        return [(math.sqrt(z * z + 4 * (1 - z) * (r * r) / total) - z) / (2 * (1 - z)) for r in raw]

    for _ in range(iters):
        z = (lo + hi) / 2
        s = sum(probs(z))
        if abs(s - 1) < tol:
            break
        if s > 1:
            lo = z
        else:
            hi = z
    p = probs((lo + hi) / 2)
    s = sum(p)
    return [x / s for x in p]


METHODS = {
    "multiplicative": devig_multiplicative,
    "power": devig_power,
    "shin": devig_shin,
}


def devig(decimals: Sequence[float], method: str = "multiplicative") -> list[float]:
    try:
        fn = METHODS[method]
    except KeyError:
        raise ValueError(
            f"unknown de-vig method {method!r}; choose from {sorted(METHODS)}"
        ) from None
    return fn(decimals)
