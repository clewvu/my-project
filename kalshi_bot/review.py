"""Where did the money go? A loss attribution over the live loop's own records.

Joins each booked result in the loop state (``state/live_loop.json``) with
the entry decision that produced it (``state/decisions.jsonl``), then cuts
the P&L by the things the strategy could have done differently: how the
position ended (sold or settled), which side, how confident the model was,
how far spot sat from the strike, how long was left, whether it fought the
trend, and how much of the damage is fees. Ends with concrete settings the
numbers support. Standard library only, so it runs anywhere the loop does.

    kalshi-bot review
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

MIN_ROWS_FOR_ADVICE = 8


def load_history(state_path: str | Path) -> list[dict[str, Any]]:
    p = Path(state_path)
    if not p.exists():
        return []
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return []
    return list(state.get("history") or [])


def load_decisions(path: str | Path | None) -> list[dict[str, Any]]:
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def attribute(
    history: list[dict[str, Any]], decisions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One row per booked result with the entry decision's inputs attached."""
    entries: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for d in decisions:
        if d.get("action") == "trade" and d.get("ticker") and d.get("side"):
            entries[(d["ticker"], d["side"])].append(d)
    out = []
    for h in history:
        key = (h.get("ticker"), h.get("side"))
        settled = float(h.get("settled_ts") or 0)
        before = [d for d in entries.get(key, []) if float(d.get("ts") or 0) <= settled]
        entry = before[-1] if before else None
        inputs = (entry or {}).get("inputs") or {}
        row = dict(h)
        row["how"] = "sold" if h.get("result") == "sold" else "settled"
        row["net"] = float(h.get("net") or 0.0)
        row["count"] = float(h.get("count") or 0.0)
        row["price"] = float(h.get("price") or 0.0)
        row["p_side"] = None
        p_yes = inputs.get("p_yes")
        if p_yes is not None:
            row["p_side"] = float(p_yes) if h.get("side") == "yes" else 1 - float(p_yes)
        row["edge"] = (entry or {}).get("edge")
        row["secs_to_close"] = inputs.get("secs_to_close")
        row["strike_bps"] = inputs.get("spot_vs_strike_bps")
        row["trend_bps"] = inputs.get("trend_bps")
        row["entry_reason"] = (entry or {}).get("reason")
        out.append(row)
    return out


def _bucket(value: float | None, edges: list[tuple[str, float, float]]) -> str:
    if value is None:
        return "unknown"
    for label, lo, hi in edges:
        if lo <= value < hi:
            return label
    return "unknown"


CONFIDENCE = [
    ("<0.55", 0, 0.55),
    ("0.55-0.65", 0.55, 0.65),
    ("0.65-0.80", 0.65, 0.80),
    (">=0.80", 0.80, 1.01),
]
TTC = [("<3 min", 0, 180), ("3-6 min", 180, 360), ("6-10 min", 360, 600), (">=10 min", 600, 1e9)]
DISTANCE = [("<5 bps", 0, 5), ("5-15 bps", 5, 15), ("15-40 bps", 15, 40), (">=40 bps", 40, 1e9)]


def cut(rows: list[dict[str, Any]], key: str, edges: list[tuple[str, float, float]] | None = None):
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        v = r.get(key)
        if edges is None:
            label = str(v) if v is not None else "unknown"
        else:
            label = _bucket(abs(v) if key == "strike_bps" and v is not None else v, edges)
        groups[label].append(r)
    order = [e[0] for e in edges] + ["unknown"] if edges else sorted(groups)
    return [(label, _stats(groups[label])) for label in order if label in groups]


def _stats(rows: list[dict[str, Any]]) -> dict[str, float]:
    n = len(rows)
    net = sum(r["net"] for r in rows)
    wins = sum(1 for r in rows if r["net"] > 0)
    return {
        "n": n,
        "net": net,
        "win_rate": wins / n if n else 0.0,
        "per_trade": net / n if n else 0.0,
    }


def fee_share(rows: list[dict[str, Any]]) -> tuple[float, float]:
    """(gross before fees, fees) estimated from the fee model on each leg."""
    from .fees import order_fee

    gross = fees = 0.0
    for r in rows:
        count, price = r["count"], r["price"]
        if count <= 0:
            continue
        f = order_fee(price, count)
        if r["how"] == "sold":
            f += order_fee(float(r.get("sold_at") or price), count)
        fees += f
        gross += r["net"] + f
    return gross, fees


def suggest(rows: list[dict[str, Any]]) -> list[str]:
    tips: list[str] = []
    if len(rows) < MIN_ROWS_FOR_ADVICE:
        return [
            f"only {len(rows)} results; the cuts below are anecdotes until there are 30 or more"
        ]
    total = sum(r["net"] for r in rows)
    gross, fees = fee_share(rows)
    if fees > 0 and gross > 0 and total < 0:
        tips.append(
            f"the trades were profitable before fees (+{gross:.2f}) and lost after them "
            f"(-{fees:.2f} fees): trade less often and only with larger edges"
        )
    sold = [r for r in rows if r["how"] == "sold"]
    if sold:
        s = _stats(sold)
        if s["net"] < 0:
            tips.append(
                f"sales lost {s['net']:+.2f} over {s['n']} exits; consider --stop-value 0 "
                "(never sell at a loss) or --no-exits"
            )
    for label, s in cut(rows, "p_side", CONFIDENCE):
        if label in ("<0.55", "0.55-0.65") and s["n"] >= 3 and s["net"] < 0:
            tips.append(
                f"entries with model confidence {label} lost {s['net']:+.2f} over {s['n']}; "
                "raise --min-confidence (0.65 or 0.70)"
            )
    for label, s in cut(rows, "secs_to_close", TTC):
        if s["n"] >= 3 and s["net"] < 0 and label == "<3 min":
            tips.append(
                f"entries under 3 minutes to close lost {s['net']:+.2f}; raise --min-ttc to 180"
            )
    for label, s in cut(rows, "strike_bps", DISTANCE):
        if s["n"] >= 3 and s["net"] < 0 and label in ("<5 bps", "5-15 bps"):
            tips.append(
                f"entries with spot {label} from the strike lost {s['net']:+.2f} over {s['n']}: "
                "that is where the model is noisiest; a higher --min-confidence avoids them"
            )
    against = [
        r
        for r in rows
        if r.get("trend_bps") is not None
        and (
            (r["side"] == "no" and r["trend_bps"] >= 10)
            or (r["side"] == "yes" and r["trend_bps"] <= -10)
        )
    ]
    if against:
        s = _stats(against)
        if s["net"] < 0:
            tips.append(
                f"{s['n']} entries fought a 10+ bps trend and lost {s['net']:+.2f}; "
                "keep --trend-bps on"
            )
    by_side = dict(cut(rows, "side"))
    for side, s in by_side.items():
        if s["n"] >= 5 and s["win_rate"] < 0.35:
            tips.append(
                f"{side.upper()} entries won {s['win_rate']:.0%} of {s['n']}: the market was "
                "trending against them; the trend filter is the guard, not a side ban"
            )
    if not tips:
        tips.append("no single cut explains the losses; this looks like variance plus fees")
    return tips


def report(state_path: str | Path, decisions_path: str | Path | None) -> str:
    history = load_history(state_path)
    rows = attribute(history, load_decisions(decisions_path))
    lines = [f"== results in {state_path}: {len(rows)}"]
    if not rows:
        lines.append("nothing booked yet")
        return "\n".join(lines)
    total = _stats(rows)
    gross, fees = fee_share(rows)
    lines.append(
        f"net {total['net']:+.2f} over {total['n']} results, win rate {total['win_rate']:.0%}, "
        f"{total['per_trade']:+.2f} per result; before fees {gross:+.2f}, fees {fees:.2f}"
    )
    matched = sum(1 for r in rows if r["p_side"] is not None)
    lines.append(f"entries matched to a decision-log row: {matched}/{len(rows)}")

    def table(title: str, cuts) -> None:
        lines.append(f"\n-- {title}")
        lines.append(f"{'bucket':<12}{'n':>5}{'net':>10}{'win%':>7}{'per':>8}")
        for label, s in cuts:
            lines.append(
                f"{label:<12}{s['n']:>5}{s['net']:>+10.2f}{s['win_rate']:>7.0%}{s['per_trade']:>+8.2f}"
            )

    table("how it ended", cut(rows, "how"))
    table("side", cut(rows, "side"))
    table("series", cut(rows, "series"))
    table("model confidence for the side bought", cut(rows, "p_side", CONFIDENCE))
    table("seconds to close at entry", cut(rows, "secs_to_close", TTC))
    table("spot distance from strike at entry", cut(rows, "strike_bps", DISTANCE))
    lines.append("\n== what the numbers support")
    for tip in suggest(rows):
        lines.append(f"* {tip}")
    return "\n".join(lines)
