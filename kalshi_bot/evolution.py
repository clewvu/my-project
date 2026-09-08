"""A continually-evolving core for the fair-value strategy.

Variation -> selection -> heritability, run on a schedule:

* **variation**: mutate the reigning *champion* config into a population of
  *challengers* (perturbed margin, vol window, max price, min confidence).
* **selection**: score every variant on a time-ordered *training* split, pick
  the best by the lower bound of its bootstrapped net (never the point
  estimate, so a lucky handful of trades cannot win), then *validate* that
  winner on a held-out *test* split it never saw.
* **heritability**: a challenger is promoted to champion only if it clears the
  gate out-of-sample and beats the incumbent there. The champion is written to
  ``champion.json``; every generation is appended to ``evolution_history.jsonl``.

The objective is unbounded (maximize net edge); the *risk per step* is bounded
(nothing is promoted without surviving out-of-sample). Selection runs entirely
on recorded data -- it never sends an order. Turning a promoted champion loose
on real money stays a deliberate, separate switch.

    python -m kalshi_bot.evolution --every 3600
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from kalshi_bot import fairvalue

log = logging.getLogger(__name__)

VOL_WINDOWS = (900, 1800, 3600)
MIN_TRADES = 25          # a variant needs at least this many trades in a split to count
MIN_TTC = 120.0
POP = 8                  # challengers per generation
TRAIN_FRACTION = 0.7


@dataclass(frozen=True)
class Variant:
    vol_window: int = 1800
    margin: float = 0.02
    max_price: float = 0.60
    min_confidence: float = 0.65

    def clamped(self) -> "Variant":
        vw = min(VOL_WINDOWS, key=lambda w: abs(w - self.vol_window))
        return Variant(
            vol_window=vw,
            margin=round(min(0.08, max(0.005, self.margin)), 4),
            max_price=round(min(0.95, max(0.55, self.max_price)), 3),
            min_confidence=round(min(0.92, max(0.55, self.min_confidence)), 3),
        )


def mutate(champion: Variant, rng: random.Random, n: int) -> list[Variant]:
    """A population of small perturbations of the champion, plus the champion."""
    pop = [champion]
    for _ in range(n):
        pop.append(Variant(
            vol_window=rng.choice(VOL_WINDOWS),
            margin=champion.margin + rng.choice([-0.02, -0.01, -0.005, 0, 0.005, 0.01, 0.02]),
            max_price=champion.max_price + rng.choice([-0.1, -0.05, 0, 0.05, 0.1]),
            min_confidence=champion.min_confidence + rng.choice([-0.1, -0.05, 0, 0.05, 0.1]),
        ).clamped())
    # de-duplicate while keeping order
    seen, uniq = set(), []
    for v in pop:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def score(fv, variant: Variant, tickers: set) -> dict:
    """Backtest a variant on a ticker set; return trades and gated net (lower CI)."""
    t = fairvalue.backtest(fv, variant.vol_window, variant.margin, min_ttc=MIN_TTC,
                           max_price=variant.max_price)
    t = t[t["ticker"].isin(tickers)]
    t = t[t["p_model"] >= variant.min_confidence]  # p_model is already the traded side
    s = fairvalue.summarize(t)
    return {
        "trades": int(s["trades"]),
        "net": float(s["taker_net"]) if s["trades"] else 0.0,
        "lo": float(s["taker_lo"]) if s["trades"] else 0.0,
        "win_rate": float(s["win_rate"]) if s["trades"] else 0.0,
    }


def evolve_once(fv, champion: Variant, seed: int = 0) -> dict:
    """One generation. Returns a record with the champion, the promoted winner
    (if any), and every variant's train/test scores."""
    train, test = fairvalue.split_tickers(fv, TRAIN_FRACTION)
    rng = random.Random(seed)
    pop = mutate(champion, rng, POP)
    graded = []
    for v in pop:
        tr = score(fv, v, train)
        te = score(fv, v, test)
        graded.append({"variant": asdict(v), "train": tr, "test": te})
    # select on TRAIN by lower CI bound, among variants with enough training trades
    eligible = [g for g in graded if g["train"]["trades"] >= MIN_TRADES]
    champ_test = next((g["test"] for g in graded if g["variant"] == asdict(champion)), None)
    promoted = None
    if eligible:
        best = max(eligible, key=lambda g: g["train"]["lo"])
        te = best["test"]
        beats = champ_test is None or te["net"] > champ_test["net"]
        # gate: survives out-of-sample (lower CI > 0) with enough test trades, and
        # beats the incumbent there. Otherwise the champion stands.
        if te["trades"] >= MIN_TRADES and te["lo"] > 0 and beats:
            promoted = best["variant"]
    return {
        "champion_before": asdict(champion),
        "promoted": promoted,
        "champion_test": champ_test,
        "population": graded,
    }


def run(db: str, series: list[str] | None, every: float, generations: int,
        seed: int, state_dir: str) -> int:
    sd = Path(state_dir)
    champ_file = sd / "champion.json"
    hist_file = sd / "evolution_history.jsonl"
    champion = Variant()
    if champ_file.exists():
        try:
            champion = Variant(**{k: v for k, v in json.loads(champ_file.read_text()).items()
                                  if k in Variant.__dataclass_fields__})
        except (ValueError, TypeError):
            pass

    gen = 0
    while True:
        gen += 1
        try:
            fv = fairvalue.load(db, series=series or None, vol_windows=VOL_WINDOWS)
            rec = evolve_once(fv, champion, seed=seed + gen)
        except Exception:  # noqa: BLE001 - a bad generation must not kill the loop
            log.exception("evolution generation failed")
            rec = None
        if rec is not None:
            ct = rec.get("champion_test") or {}
            line = f"gen {gen}: champion margin={champion.margin} vw={champion.vol_window} " \
                   f"conf={champion.min_confidence} maxp={champion.max_price} | " \
                   f"OOS net={ct.get('net', 0):+.4f} lo={ct.get('lo', 0):+.4f} " \
                   f"n={ct.get('trades', 0)}"
            if rec["promoted"]:
                champion = Variant(**rec["promoted"]).clamped()
                line += f"  -> PROMOTED margin={champion.margin} vw={champion.vol_window} " \
                        f"conf={champion.min_confidence} maxp={champion.max_price}"
            else:
                line += "  (champion stands)"
            log.info(line)
            print(line)
            sd.mkdir(parents=True, exist_ok=True)
            champ_file.write_text(json.dumps(asdict(champion), indent=2))
            with hist_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"gen": gen, **rec}, default=str) + "\n")
        if generations and gen >= generations:
            return 0
        if every <= 0:
            return 0
        time.sleep(every)


def main() -> int:
    ap = argparse.ArgumentParser(description="Evolve the fair-value champion config.")
    ap.add_argument("--db", default="state/market_data.sqlite")
    ap.add_argument("--series", action="append")
    ap.add_argument("--every", type=float, default=0.0, help="seconds between generations (0 = once)")
    ap.add_argument("--generations", type=int, default=1, help="stop after N (0 = forever)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--state-dir", default="state")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    return run(args.db, args.series, args.every, args.generations, args.seed, args.state_dir)


if __name__ == "__main__":
    raise SystemExit(main())
