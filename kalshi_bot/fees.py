"""Kalshi trading fees.

Kalshi charges takers a fee proportional to price x (1 - price), so it is
largest at 50 cents and shrinks toward the tails. The fee is computed per
order and rounded up to the next cent. Maker (resting) orders on most
markets pay no fee; a maker rate can be supplied for markets that charge one.

The default rate is the published general formula, 7%. Verify it against
Kalshi's current fee schedule for the specific series before trading.
"""

from __future__ import annotations

import math

TAKER_RATE = 0.07
MAKER_RATE = 0.0


def fee_per_contract(price: float, rate: float = TAKER_RATE) -> float:
    """Unrounded fee for one contract at ``price`` dollars. Use for expected-value math."""
    return rate * price * (1 - price)


def order_fee(price: float, count: float, rate: float = TAKER_RATE) -> float:
    """Fee charged on one order, rounded up to the cent like the exchange does."""
    raw = rate * count * price * (1 - price)
    return math.ceil(raw * 100 - 1e-9) / 100


def breakeven_win_rate(price: float, rate: float = TAKER_RATE) -> float:
    """Win probability needed for buying at ``price`` (as taker) to have zero expected value.

    Payoff on a win is 1 - price - fee, on a loss it is -price - fee.
    """
    fee = fee_per_contract(price, rate)
    return (price + fee) / 1.0
