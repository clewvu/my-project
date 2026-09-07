"""Consensus-gap strategy (hypothesis H1 in docs/sports-design.md), as a pure function.

Buy the side of a Kalshi moneyline whose de-vigged sharp-consensus probability
beats the ask by the fee plus a margin, when the books agree with each other,
the quotes are fresh, and the game is inside the lead window. Every input and
the reason for every decision come back in the ``Decision`` so the log doubles
as the feature store.

Parameters live in a JSON file the learning loop may rewrite; the trader
reloads them when the file changes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from kalshi_bot.fees import fee_per_contract

MAX_MARGIN = 0.10
MIN_MARGIN = 0.01


@dataclass(frozen=True)
class Params:
    margin: float = 0.03  # cents of edge required after the fee
    min_books: int = 3
    min_sharp: int = 1
    max_dispersion: float = 0.04  # std dev of book probabilities
    max_odds_age_s: float = 900.0
    max_quote_age_s: float = 120.0
    min_lead_s: float = 15 * 60  # do not enter inside 15 minutes of the start
    max_lead_s: float = 24 * 3600
    min_price: float = 0.15  # avoid the tails where the fee model and the books are noisiest
    max_price: float = 0.85
    max_spread: float = 0.06
    size_scale: float = 1.0  # multiplies the base stake; only the learner changes it
    method: str = "multiplicative"
    version: int = 1
    note: str = "defaults"

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=1))
        tmp.replace(p)

    @classmethod
    def load(cls, path: str | Path | None) -> Params:
        if path is None or not Path(path).exists():
            return cls()
        data = json.loads(Path(path).read_text())
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def tightened(
        self, *, margin_step: float = 0.01, size_factor: float = 0.5, note: str
    ) -> Params:
        return replace(
            self,
            margin=min(MAX_MARGIN, round(self.margin + margin_step, 3)),
            size_scale=max(0.25, round(self.size_scale * size_factor, 3)),
            version=self.version + 1,
            note=note,
        )

    def loosened(self, *, size_factor: float = 1.25, max_scale: float, note: str) -> Params:
        return replace(
            self,
            size_scale=min(max_scale, round(self.size_scale * size_factor, 3)),
            version=self.version + 1,
            note=note,
        )


@dataclass(frozen=True)
class Context:
    ticker: str
    league: str
    side_team: str | None
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    quote_age_s: float
    p_yes: float | None  # consensus probability that YES wins
    n_books: int
    sharp_books: int
    dispersion: float
    odds_age_s: float | None
    secs_to_start: float | None
    event_exposure: float  # dollars already at risk on this game
    start_exact: bool = True


@dataclass(frozen=True)
class Decision:
    action: str  # buy_yes | buy_no | skip
    reason: str
    edge: float | None = None
    price: float | None = None  # ask on the chosen side
    p_side: float | None = None  # consensus probability of the chosen side
    inputs: dict[str, Any] | None = None

    @property
    def is_entry(self) -> bool:
        return self.action in ("buy_yes", "buy_no")

    @property
    def side(self) -> str | None:
        return {"buy_yes": "yes", "buy_no": "no"}.get(self.action)


class ConsensusGapStrategy:
    def __init__(self, params: Params | None = None) -> None:
        self.params = params or Params()

    def evaluate(self, ctx: Context) -> Decision:
        p = self.params
        inputs = asdict(ctx)

        def skip(reason: str) -> Decision:
            return Decision("skip", reason, inputs=inputs)

        if ctx.secs_to_start is None:
            return skip("start time unknown")
        if not ctx.start_exact:
            return skip("start time approximate")
        if ctx.secs_to_start < p.min_lead_s:
            return skip("inside no-entry window")
        if ctx.secs_to_start > p.max_lead_s:
            return skip("too early")
        if ctx.event_exposure > 0:
            return skip("already positioned on this game")
        if ctx.quote_age_s > p.max_quote_age_s:
            return skip("kalshi quote stale")
        if ctx.p_yes is None:
            return skip("no consensus")
        if ctx.odds_age_s is None or ctx.odds_age_s > p.max_odds_age_s:
            return skip("odds stale")
        if ctx.n_books < p.min_books:
            return skip("too few books")
        if ctx.sharp_books < p.min_sharp:
            return skip("no sharp book")
        if ctx.dispersion > p.max_dispersion:
            return skip("books disagree")
        if ctx.yes_ask is None or ctx.no_ask is None:
            return skip("no ask")
        if ctx.yes_bid is not None and ctx.yes_ask - ctx.yes_bid > p.max_spread:
            return skip("spread too wide")

        edge_yes = ctx.p_yes - ctx.yes_ask - fee_per_contract(ctx.yes_ask)
        edge_no = (1 - ctx.p_yes) - ctx.no_ask - fee_per_contract(ctx.no_ask)
        inputs["edge_yes"], inputs["edge_no"], inputs["margin"] = edge_yes, edge_no, p.margin
        if edge_yes >= edge_no:
            action, edge, price, p_side = "buy_yes", edge_yes, ctx.yes_ask, ctx.p_yes
        else:
            action, edge, price, p_side = "buy_no", edge_no, ctx.no_ask, 1 - ctx.p_yes
        if edge < p.margin:
            return Decision(
                "skip", "edge below margin", edge=edge, price=price, p_side=p_side, inputs=inputs
            )
        if not (p.min_price <= price <= p.max_price):
            return Decision(
                "skip", "price outside range", edge=edge, price=price, p_side=p_side, inputs=inputs
            )
        return Decision(
            action, "consensus gap", edge=edge, price=price, p_side=p_side, inputs=inputs
        )
