"""Stake by confidence, earned by results.

Two pieces, both standard library so the loop never depends on the research
extras being installed:

* ``TrackRecord``: what the strategy's own live and paper results say about
  each confidence tier of the model. Loaded from the loop state files and
  the decision log (the same join ``kalshi_bot.review`` makes). It gives an
  empirical win rate per tier, a calibrated probability that shrinks the
  model's figure toward that win rate as evidence accumulates, and a gate:
  a tier may stake more than the base only after ``MIN_TIER_RESULTS``
  results with a positive net.
* ``kelly_dollars``: a quarter-Kelly stake for one trade from its calibrated
  probability and ask, bounded below by the base stake and above by the
  per-trade ceiling and a share of the bankroll.

The base stake is what an ungated tier gets. Nothing here can reduce the
stake below it, and nothing can raise it above ``max_dollars``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .fees import fee_per_contract

TIERS: list[tuple[str, float, float]] = [
    ("0.65-0.75", 0.65, 0.75),
    ("0.75-0.85", 0.75, 0.85),
    (">=0.85", 0.85, 1.01),
]
MIN_TIER_RESULTS = 20  # results a tier needs before it may scale above the base
PRIOR_WEIGHT = 20.0  # how many results it takes to trust the record as much as the model
KELLY_FRACTION = 0.25
MAX_BANKROLL_SHARE = 0.05


def tier_of(p: float) -> str | None:
    for label, lo, hi in TIERS:
        if lo <= p < hi:
            return label
    return None


def kelly_fraction(p: float, ask: float, fee_rate: float | None = None) -> float:
    """Full-Kelly fraction of bankroll for buying one side at ``ask`` with
    win probability ``p``, the entry fee counted as part of the stake. 0 when
    there is no edge."""
    if not 0 < ask < 1 or not 0 <= p <= 1:
        return 0.0
    fee = fee_per_contract(ask) if fee_rate is None else fee_rate * ask * (1 - ask)
    gain = 1.0 - ask - fee  # per contract if it wins
    loss = ask + fee  # per contract if it loses
    if gain <= 0:
        return 0.0
    edge = p * gain - (1 - p) * loss
    return max(0.0, edge / gain)


def kelly_dollars(
    p: float,
    ask: float,
    bankroll: float | None,
    *,
    base: float,
    max_dollars: float,
    fraction: float = KELLY_FRACTION,
    max_share: float = MAX_BANKROLL_SHARE,
) -> float:
    """Quarter-Kelly stake bounded to [base, max_dollars] and to ``max_share``
    of the bankroll; the base when the bankroll is unknown."""
    if bankroll is None or bankroll <= 0:
        return base
    f = min(kelly_fraction(p, ask) * fraction, max_share)
    dollars = bankroll * f
    return round(min(max_dollars, max(base, dollars)), 2)


@dataclass
class TierStats:
    n: int = 0
    wins: int = 0
    net: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0


@dataclass
class TrackRecord:
    """Per-tier results of the strategy's own trades, live and paper."""

    tiers: dict[str, TierStats] = field(default_factory=dict)
    loaded_ts: float | None = None
    results: int = 0

    @classmethod
    def load(
        cls,
        sources: list[tuple[str | Path, str | Path | None]],
        *,
        now: float | None = None,
    ) -> TrackRecord:
        """``sources`` pairs each loop state file with its own decision log."""
        from .review import attribute, load_decisions, load_history

        rec = cls(loaded_ts=time.time() if now is None else now)
        seen: set[tuple[str, str, float]] = set()
        cache: dict[str, list] = {}
        for path, decisions_path in sources:
            key = str(decisions_path)
            if key not in cache:
                cache[key] = load_decisions(decisions_path)
            for row in attribute(load_history(path), cache[key]):
                key = (
                    str(row.get("ticker")),
                    str(row.get("side")),
                    float(row.get("settled_ts") or 0),
                )
                if key in seen:
                    continue
                seen.add(key)
                rec.add(row.get("p_side"), row["net"])
        return rec

    def add(self, p_side: float | None, net: float) -> None:
        self.results += 1
        if p_side is None:
            return
        label = tier_of(float(p_side))
        if label is None:
            return
        t = self.tiers.setdefault(label, TierStats())
        t.n += 1
        t.wins += int(net > 0)
        t.net += float(net)

    def stats(self, p: float) -> TierStats | None:
        label = tier_of(p)
        return self.tiers.get(label) if label else None

    def allows_scaling(self, p: float) -> bool:
        """May this confidence tier stake more than the base?"""
        t = self.stats(p)
        return t is not None and t.n >= MIN_TIER_RESULTS and t.net > 0

    def calibrated(self, p: float, prior_weight: float = PRIOR_WEIGHT) -> float:
        """The model's probability shrunk toward the tier's realised win rate,
        weighted by how many results the tier has."""
        t = self.stats(p)
        if t is None or t.n == 0:
            return p
        return (t.n * t.win_rate + prior_weight * p) / (t.n + prior_weight)

    def describe(self) -> str:
        if not self.tiers:
            return f"{self.results} results, none in a confidence tier"
        parts = [
            f"{label}: {t.n} results, {t.win_rate:.0%} won, {t.net:+.2f}"
            f"{' (scaling on)' if t.n >= MIN_TIER_RESULTS and t.net > 0 else ''}"
            for label, t in sorted(self.tiers.items())
        ]
        return "; ".join(parts)
