"""The self-improvement loop: scheduled retraining, gated promotion, drift control.

This is the "section 6" design from the research brief made concrete, with
the rules Cameron and Claude agreed on:

* **No online learning.** The live strategy never changes its own mind
  between trades. It reads a parameter file that this module rewrites on a
  schedule (hourly by default), from recorded data, and only after a gate.
* **Candidates are evaluated paired on the same windows.** A candidate set of
  parameters (volatility window, margin, probability calibration) is fitted
  on the first 70% of recorded markets and judged on the last 30%, the same
  split and the same clustered bootstrap as ``kalshi_bot.fairvalue``.
* **Promotion gate.** A candidate replaces the incumbent only when its
  held-out taker net is at least +1 cent per contract with a 95% interval
  above zero, on at least 60 held-out trades, and it is not worse than the
  incumbent on those same windows. Otherwise the incumbent stays, and if
  there is no incumbent the strategy keeps its command-line defaults.
* **Size is the only live-adjustable knob, and only downward.** Live fills
  are compared with what the model expected (the decision log joined with
  the loop's settlements). When realised results fall clearly short of the
  model's own probabilities, ``size_scale`` drops; when they fall far short,
  ``halt`` is set and the strategy skips every market until the next healthy
  evaluation. Recovery is automatic on the next cycle that passes.
* **Everything is written down.** ``state/params.json`` holds the active
  parameters; ``state/learn_history.jsonl`` holds every evaluation, promoted
  or not, so the choices can be audited afterwards.

The "simple residual model" is Platt scaling: p' = sigmoid(a + b * logit(p)).
Two numbers, fitted by Newton's method, no libraries beyond numpy.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import fairvalue as fvmod

log = logging.getLogger(__name__)

MIN_HELD_OUT_TRADES = 60
VIABLE_NET = 0.01
MIN_LIVE_TRADES_FOR_DRIFT = 30
DRIFT_Z_SHRINK = -2.0  # realised wins this many standard errors below expected: halve size
DRIFT_Z_HALT = -3.0  # this far below: stop trading until the next healthy cycle
MIN_SIZE_SCALE = 0.25


# ---------------------------------------------------------------- parameters


@dataclass
class Params:
    vol_window: float = 1800.0
    margin: float = 0.02
    calib_a: float = 0.0  # Platt intercept
    calib_b: float = 1.0  # Platt slope
    size_scale: float = 1.0
    halt: bool = False
    updated: float | None = None
    source: str = "defaults"
    note: str = ""
    evaluation: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path | None) -> Params | None:
        if path is None or not Path(path).exists():
            return None
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError) as exc:
            log.warning("params file %s unreadable: %s", path, exc)
            return None
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, default=str))
        for attempt in range(20):
            try:
                tmp.replace(p)
                return
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
        p.write_text(json.dumps(asdict(self), indent=2, default=str))

    def calibrate(self, p: float) -> float:
        return apply_calibration(p, self.calib_a, self.calib_b)


def _logit(p: np.ndarray | float) -> np.ndarray | float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def apply_calibration(p: Any, a: float, b: float) -> Any:
    z = a + b * _logit(np.asarray(p, dtype=float))
    out = 1.0 / (1.0 + np.exp(-z))
    return float(out) if np.ndim(out) == 0 else out


def fit_calibration(p: np.ndarray, y: np.ndarray, iterations: int = 25) -> tuple[float, float]:
    """Platt scaling by Newton's method with a light ridge toward (0, 1)."""
    x = np.asarray(_logit(np.asarray(p, dtype=float)), dtype=float)
    y = np.asarray(y, dtype=float)
    if len(x) < 20 or y.min() == y.max():
        return (0.0, 1.0)
    a, b = 0.0, 1.0
    lam = 1e-2
    for _ in range(iterations):
        z = a + b * x
        q = 1.0 / (1.0 + np.exp(-z))
        w = q * (1 - q)
        g_a = np.sum(q - y) + lam * a
        g_b = np.sum((q - y) * x) + lam * (b - 1.0)
        h_aa = np.sum(w) + lam
        h_ab = np.sum(w * x)
        h_bb = np.sum(w * x * x) + lam
        det = h_aa * h_bb - h_ab * h_ab
        if det <= 1e-12:
            break
        da = (h_bb * g_a - h_ab * g_b) / det
        db = (h_aa * g_b - h_ab * g_a) / det
        a -= da
        b -= db
        if abs(da) < 1e-8 and abs(db) < 1e-8:
            break
    if not (np.isfinite(a) and np.isfinite(b)) or b <= 0:
        return (0.0, 1.0)
    return (float(a), float(b))


# ---------------------------------------------------------------- retraining


@dataclass
class Candidate:
    vol_window: int
    margin: float
    calib_a: float
    calib_b: float
    train: dict[str, Any]
    held_out: dict[str, Any]

    @property
    def passes_gate(self) -> bool:
        h = self.held_out
        return (
            h["trades"] >= MIN_HELD_OUT_TRADES
            and h["taker_net"] >= VIABLE_NET
            and h["taker_lo"] > 0
        )


def _with_calibration(fv: fvmod.FairValueData, a: float, b: float) -> fvmod.FairValueData:
    snaps = fv.snapshots.copy()
    for w in fv.vol_windows:
        col = f"p_{w}"
        if col in snaps:
            snaps[col] = apply_calibration(snaps[col].to_numpy(), a, b)
    return fvmod.FairValueData(
        snapshots=snaps,
        markets=fv.markets,
        spot=fv.spot,
        trades=fv.trades,
        vol_windows=fv.vol_windows,
    )


def fit_candidate(
    fv: fvmod.FairValueData, train: set, test: set, horizon: float = 300.0
) -> Candidate | None:
    """Fit calibration on the training markets, pick (window, margin) there,
    and report both splits. None when nothing produced enough training trades."""
    best: Candidate | None = None
    for w in fv.vol_windows:
        col = f"p_{w}"
        snap = fvmod.at_horizon(fv.snapshots[fv.snapshots["ticker"].isin(train)], horizon)
        snap = snap.dropna(subset=[col, "won_yes"])
        a, b = fit_calibration(snap[col].to_numpy(), snap["won_yes"].to_numpy())
        calibrated = _with_calibration(fv, a, b)
        for m in fvmod.MARGINS:
            t = fvmod.backtest(calibrated, w, m)
            tr = fvmod.summarize(t[t["ticker"].isin(train)])
            if tr["trades"] < fvmod.MIN_TRAIN_TRADES or not np.isfinite(tr["taker_lo"]):
                continue
            ho = fvmod.summarize(t[t["ticker"].isin(test)])
            cand = Candidate(int(w), float(m), a, b, tr, ho)
            key = (tr["taker_lo"], tr["taker_net"])
            if best is None or key > (best.train["taker_lo"], best.train["taker_net"]):
                best = cand
    return best


def evaluate_incumbent(fv: fvmod.FairValueData, params: Params, test: set) -> dict[str, Any] | None:
    if int(params.vol_window) not in fv.vol_windows:
        return None
    calibrated = _with_calibration(fv, params.calib_a, params.calib_b)
    t = fvmod.backtest(calibrated, int(params.vol_window), params.margin)
    return fvmod.summarize(t[t["ticker"].isin(test)])


def retrain(
    db_path: str | Path | None,
    incumbent: Params | None,
    fv: fvmod.FairValueData | None = None,
) -> tuple[Params | None, dict[str, Any]]:
    """One retraining pass over the recorder database (or preloaded data).

    Returns (promoted params or None, an evaluation record for the history).
    """
    if fv is None:
        fv = fvmod.load(str(db_path))
    n_settled = len(fv.settled_markets)
    record: dict[str, Any] = {"kind": "retrain", "settled_markets": n_settled}
    if n_settled < 50:
        record["outcome"] = f"too little data ({n_settled} settled markets)"
        return None, record
    train, test = fvmod.split_tickers(fv)
    cand = fit_candidate(fv, train, test)
    if cand is None:
        record["outcome"] = "no configuration produced enough training trades"
        return None, record
    record["candidate"] = {
        "vol_window": cand.vol_window,
        "margin": cand.margin,
        "calib_a": round(cand.calib_a, 4),
        "calib_b": round(cand.calib_b, 4),
        "train": _slim(cand.train),
        "held_out": _slim(cand.held_out),
        "passes_gate": cand.passes_gate,
    }
    if not cand.passes_gate:
        record["outcome"] = "candidate failed the promotion gate; incumbent kept"
        return None, record
    if incumbent is not None and incumbent.source != "defaults":
        inc = evaluate_incumbent(fv, incumbent, test)
        record["incumbent_held_out"] = _slim(inc) if inc else None
        if inc and np.isfinite(inc["taker_net"]) and cand.held_out["taker_net"] <= inc["taker_net"]:
            record["outcome"] = "candidate passed the gate but does not beat the incumbent"
            return None, record
    promoted = Params(
        vol_window=float(cand.vol_window),
        margin=cand.margin,
        calib_a=cand.calib_a,
        calib_b=cand.calib_b,
        size_scale=incumbent.size_scale if incumbent else 1.0,
        halt=False,
        updated=time.time(),
        source="retrain",
        note=(
            f"promoted: held-out net {cand.held_out['taker_net']:+.3f}/contract "
            f"[{cand.held_out['taker_lo']:+.3f}, {cand.held_out['taker_hi']:+.3f}] "
            f"over {cand.held_out['trades']} trades"
        ),
        evaluation=record["candidate"],
    )
    record["outcome"] = "promoted"
    return promoted, record


def _slim(s: dict[str, Any] | None) -> dict[str, Any] | None:
    if not s:
        return None
    keys = (
        "trades",
        "win_rate",
        "implied",
        "model_p",
        "excess",
        "taker_net",
        "taker_lo",
        "taker_hi",
    )
    return {k: (round(float(s[k]), 4) if isinstance(s[k], float) else s[k]) for k in keys if k in s}


# ---------------------------------------------------------------- drift


def live_trades(decisions_path: str | Path, state_path: str | Path) -> pd.DataFrame:
    """Live fair-value trades joined with their settlements: one row per
    settled trade with the model's probability for the side taken."""
    dpath, spath = Path(decisions_path), Path(state_path)
    if not dpath.exists() or not spath.exists():
        return pd.DataFrame(columns=["ticker", "side", "p_side", "won", "net"])
    rows = []
    for line in dpath.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("action") != "trade" or r.get("strategy") != "fairvalue":
            continue
        p_yes = (r.get("inputs") or {}).get("p_yes")
        if p_yes is None:
            continue
        rows.append(
            {
                "ticker": r["ticker"],
                "side": r["side"],
                "p_side": p_yes if r["side"] == "yes" else 1 - p_yes,
            }
        )
    if not rows:
        return pd.DataFrame(columns=["ticker", "side", "p_side", "won", "net"])
    dec = pd.DataFrame(rows).drop_duplicates("ticker", keep="last")
    try:
        hist = json.loads(spath.read_text()).get("history") or []
    except (OSError, ValueError):
        hist = []
    settled = pd.DataFrame(hist)
    if settled.empty:
        return pd.DataFrame(columns=["ticker", "side", "p_side", "won", "net"])
    settled = settled[["ticker", "won", "net"]].drop_duplicates("ticker", keep="last")
    out = dec.merge(settled, on="ticker", how="inner")
    out["won"] = out["won"].astype(float)
    return out


def drift_check(trades: pd.DataFrame) -> dict[str, Any]:
    """Compare realised wins with the sum of the model's own probabilities."""
    n = int(len(trades))
    if n < MIN_LIVE_TRADES_FOR_DRIFT:
        return {"kind": "drift", "n": n, "status": "insufficient", "z": None}
    p = trades["p_side"].to_numpy(dtype=float)
    expected = float(p.sum())
    realised = float(trades["won"].sum())
    se = float(np.sqrt(np.sum(p * (1 - p)))) or 1.0
    z = (realised - expected) / se
    status = "ok"
    if z <= DRIFT_Z_HALT:
        status = "halt"
    elif z <= DRIFT_Z_SHRINK:
        status = "shrink"
    return {
        "kind": "drift",
        "n": n,
        "expected_wins": round(expected, 2),
        "realised_wins": realised,
        "z": round(float(z), 2),
        "net_per_contract": round(float(trades["net"].sum() / max(1, n)), 4),
        "status": status,
    }


def apply_drift(params: Params, drift: dict[str, Any]) -> Params:
    status = drift.get("status")
    if status == "halt":
        params.halt = True
        params.size_scale = max(MIN_SIZE_SCALE, params.size_scale * 0.5)
        params.note = f"halted on drift z={drift['z']}"
    elif status == "shrink":
        params.halt = False
        params.size_scale = max(MIN_SIZE_SCALE, params.size_scale * 0.5)
        params.note = f"size halved on drift z={drift['z']}"
    elif status == "ok":
        params.halt = False
        # recovery is gradual: one step back toward full size per healthy cycle
        params.size_scale = min(1.0, params.size_scale * 1.5) if params.size_scale < 1 else 1.0
    return params


# ---------------------------------------------------------------- one cycle


def run_cycle(
    *,
    db_path: str | Path,
    params_path: str | Path,
    history_path: str | Path,
    decisions_path: str | Path | None = None,
    live_state_path: str | Path | None = None,
    fv: fvmod.FairValueData | None = None,
) -> Params:
    incumbent = Params.load(params_path)
    promoted, record = retrain(db_path, incumbent, fv=fv)
    params = promoted or incumbent or Params()
    if promoted is None and incumbent is None:
        params.note = record.get("outcome", "")
    if decisions_path and live_state_path:
        drift = drift_check(live_trades(decisions_path, live_state_path))
        record["drift"] = drift
        params = apply_drift(params, drift)
    params.updated = time.time()
    params.save(params_path)
    record["time"] = datetime.now(UTC).isoformat(timespec="seconds")
    record["active"] = {
        "vol_window": params.vol_window,
        "margin": params.margin,
        "calib_a": round(params.calib_a, 4),
        "calib_b": round(params.calib_b, 4),
        "size_scale": params.size_scale,
        "halt": params.halt,
        "source": params.source,
    }
    hp = Path(history_path)
    hp.parent.mkdir(parents=True, exist_ok=True)
    with hp.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    log.info("learn: %s; active %s", record.get("outcome"), record["active"])
    return params


def describe(record_or_params: Params, record: dict[str, Any] | None = None) -> str:
    p = record_or_params
    lines = [
        "== active parameters",
        f"source={p.source} vol_window={p.vol_window:.0f}s margin={p.margin:.3f} "
        f"calibration=(a {p.calib_a:+.3f}, b {p.calib_b:.3f}) size_scale={p.size_scale:.2f} "
        f"halt={p.halt}",
    ]
    if p.note:
        lines.append(f"note: {p.note}")
    if record:
        lines.append("\n== this cycle")
        lines.append(f"settled markets: {record.get('settled_markets')}")
        lines.append(f"outcome: {record.get('outcome')}")
        c = record.get("candidate")
        if c:
            lines.append(
                f"candidate: window {c['vol_window']}s margin {c['margin']:.2f} "
                f"calib ({c['calib_a']:+.3f}, {c['calib_b']:.3f})"
            )
            lines.append(f"  train:    {c['train']}")
            gate = "PASS" if c["passes_gate"] else "fail"
            lines.append(f"  held out: {c['held_out']}  gate={gate}")
        if record.get("incumbent_held_out") is not None:
            lines.append(f"  incumbent on same windows: {record['incumbent_held_out']}")
        d = record.get("drift")
        if d:
            lines.append(f"live drift: {d}")
    return "\n".join(lines)
