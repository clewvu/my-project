"""Self-learning for the sports trader: review results, adjust parameters through a gate.

What it can honestly do with early data:

* measure closing-line value (CLV) against the sharp consensus and against
  Kalshi's own close, with a bootstrap confidence interval, overall and by
  bucket (league, price, edge, lead time);
* tighten automatically when the evidence is bad: raise the margin and halve
  the size when CLV is significantly negative, halve the size on a losing run;
* loosen only through a gate: raise size (never above the configured ceiling,
  never the margin) when at least 50 settled results show CLV significantly
  positive and a positive net.

Everything else (a learned calibration model, pattern search over the feature
store) needs hundreds of settled results and is described in
docs/sports-strategy.md; it is not attempted here.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from .storage import SportsDataStore
from .strategy import MAX_MARGIN, Params

MIN_RESULTS = 30
MIN_RESULTS_TO_LOOSEN = 50
DRIFT_WINDOW = 20


@dataclass
class Bucket:
    n: int = 0
    wins: int = 0
    net: float = 0.0
    clv: list[float] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def mean_clv(self) -> float | None:
        return statistics.fmean(self.clv) if self.clv else None


@dataclass
class Review:
    n: int
    wins: int
    net: float
    fees: float
    clv_consensus: list[float]
    clv_kalshi: list[float]
    last_net: float
    last_wins: int
    last_n: int
    by_league: dict[str, Bucket]
    by_price: dict[str, Bucket]
    by_edge: dict[str, Bucket]
    by_lead: dict[str, Bucket]

    @staticmethod
    def ci(values: list[float], seed: int = 7, reps: int = 2000) -> tuple[float, float] | None:
        """Bootstrap 95% interval of the mean; None with fewer than five values."""
        if len(values) < 5:
            return None
        rng = random.Random(seed)
        means = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(reps))
        return means[int(0.025 * reps)], means[int(0.975 * reps) - 1]


def _bucket(value: float | None, edges: list[tuple[str, float, float]]) -> str:
    if value is None:
        return "?"
    for label, lo, hi in edges:
        if lo <= value < hi:
            return label
    return "?"


PRICE_EDGES = [
    ("<0.35", 0, 0.35),
    ("0.35-0.50", 0.35, 0.50),
    ("0.50-0.65", 0.50, 0.65),
    (">=0.65", 0.65, 1.01),
]
EDGE_EDGES = [
    ("<0.04", -1, 0.04),
    ("0.04-0.06", 0.04, 0.06),
    ("0.06-0.10", 0.06, 0.10),
    (">=0.10", 0.10, 9),
]
LEAD_EDGES = [
    ("<1h", 0, 3600),
    ("1-6h", 3600, 6 * 3600),
    ("6-24h", 6 * 3600, 24 * 3600),
    (">24h", 24 * 3600, 9e9),
]


def review(store: SportsDataStore, mode: str) -> Review:
    rows = store.positions(status="settled", mode=mode)
    by_league: dict[str, Bucket] = {}
    by_price: dict[str, Bucket] = {}
    by_edge: dict[str, Bucket] = {}
    by_lead: dict[str, Bucket] = {}
    clv_c: list[float] = []
    clv_k: list[float] = []
    net = fees = 0.0
    wins = 0
    for r in rows:
        n = float(r["net"] or 0.0)
        net += n
        fees += float(r["fee"] or 0.0)
        wins += int(n > 0)
        if r["clv_consensus"] is not None:
            clv_c.append(float(r["clv_consensus"]))
        if r["clv_kalshi"] is not None:
            clv_k.append(float(r["clv_kalshi"]))
        lead = (r["start_ts"] - r["opened_ts"]) if r["start_ts"] else None
        for table, key in (
            (by_league, r["league"] or "?"),
            (by_price, _bucket(r["price"], PRICE_EDGES)),
            (by_edge, _bucket(r["edge_entry"], EDGE_EDGES)),
            (by_lead, _bucket(lead, LEAD_EDGES)),
        ):
            b = table.setdefault(key, Bucket())
            b.n += 1
            b.wins += int(n > 0)
            b.net += n
            if r["clv_consensus"] is not None:
                b.clv.append(float(r["clv_consensus"]))
    last = rows[-DRIFT_WINDOW:]
    return Review(
        n=len(rows),
        wins=wins,
        net=round(net, 2),
        fees=round(fees, 2),
        clv_consensus=clv_c,
        clv_kalshi=clv_k,
        last_net=round(sum(float(r["net"] or 0) for r in last), 2),
        last_wins=sum(1 for r in last if (r["net"] or 0) > 0),
        last_n=len(last),
        by_league=by_league,
        by_price=by_price,
        by_edge=by_edge,
        by_lead=by_lead,
    )


def propose(params: Params, rv: Review, *, max_scale: float) -> tuple[Params | None, str]:
    """A parameter change and why, or (None, why not)."""
    if rv.n < MIN_RESULTS:
        return None, f"hold: {rv.n} settled results, need {MIN_RESULTS} before adjusting"
    ci = Review.ci(rv.clv_consensus)
    if ci is not None and ci[1] < 0:
        new_margin = min(MAX_MARGIN, params.margin + 0.01)
        note = f"CLV vs consensus negative (CI {ci[0]:+.3f}..{ci[1]:+.3f}) after {rv.n}"
        return params.tightened(note=note), (
            f"tighten: CLV vs consensus significantly negative; "
            f"margin -> {new_margin:.2f}, size halved"
        )
    drifting = rv.last_n >= DRIFT_WINDOW and rv.last_net < 0 and rv.last_wins <= DRIFT_WINDOW * 0.3
    if drifting:
        note = f"drift: last {rv.last_n} net {rv.last_net:+.2f}"
        return params.tightened(margin_step=0.0, note=note), (
            f"drift: last {rv.last_n} results net {rv.last_net:+.2f} "
            f"with {rv.last_wins} wins; size halved"
        )
    can_loosen = (
        rv.n >= MIN_RESULTS_TO_LOOSEN
        and ci is not None
        and ci[0] > 0
        and rv.net > 0
        and params.size_scale < max_scale
    )
    if can_loosen:
        note = f"CLV positive (CI {ci[0]:+.3f}..{ci[1]:+.3f}), net {rv.net:+.2f} after {rv.n}"
        new_scale = min(max_scale, params.size_scale * 1.25)
        return params.loosened(max_scale=max_scale, note=note), (
            f"loosen: CLV significantly positive and net {rv.net:+.2f}; "
            f"size scale -> {new_scale:.2f}"
        )
    return None, "hold: evidence does not clear a gate either way"


def run_cycle(
    store: SportsDataStore, params_path: str | Path, mode: str, *, now: float, max_scale: float
) -> str | None:
    params = Params.load(params_path)
    rv = review(store, mode)
    new, why = propose(params, rv, max_scale=max_scale)
    if new is None:
        return None
    new.save(params_path)
    store.limit_set(f"{mode}:last_learn", now)
    return f"params v{new.version}: {why}"


def format_review(rv: Review) -> str:
    def ci_text(values: list[float]) -> str:
        if not values:
            return "-"
        m = statistics.fmean(values)
        ci = Review.ci(values)
        text = f"{m * 100:+.2f}c"
        if ci:
            text += f" (95% {ci[0] * 100:+.2f}..{ci[1] * 100:+.2f})"
        return text

    lines = [
        f"settled {rv.n}, won {rv.wins} ({rv.wins / rv.n:.0%})" if rv.n else "settled 0",
        f"net {rv.net:+.2f} after fees {rv.fees:.2f}",
        f"CLV vs consensus {ci_text(rv.clv_consensus)}   "
        f"CLV vs Kalshi close {ci_text(rv.clv_kalshi)}",
        f"last {rv.last_n}: net {rv.last_net:+.2f}, {rv.last_wins} wins",
    ]
    for title, table in (
        ("by league", rv.by_league),
        ("by price", rv.by_price),
        ("by edge", rv.by_edge),
        ("by lead", rv.by_lead),
    ):
        if table:
            lines.append(title + ":")
            for key, b in sorted(table.items()):
                clv = f"{b.mean_clv * 100:+.2f}c" if b.mean_clv is not None else "-"
                lines.append(
                    f"  {key:<10} n={b.n:<4} won {b.win_rate:.0%}  net {b.net:+.2f}  CLV {clv}"
                )
    return "\n".join(lines)
