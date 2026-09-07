import json

import pytest

from kalshi_bot import sizing
from kalshi_bot.demo_loop import LoopState

T0 = 1_800_000_000.0


def test_kelly_fraction_and_dollars():
    # 80% at 45c: gain 0.5327, loss 0.4673 -> edge 0.3327 -> f* 0.62
    f = sizing.kelly_fraction(0.80, 0.45)
    assert 0.6 < f < 0.65
    assert sizing.kelly_fraction(0.45, 0.45) == 0.0  # no edge after the fee
    assert sizing.kelly_fraction(0.9, 0.0) == 0.0 and sizing.kelly_fraction(0.9, 1.0) == 0.0
    # quarter Kelly of 0.62 on $200 is $31, capped by 5% of bankroll ($10) then max $20
    assert sizing.kelly_dollars(0.80, 0.45, 200.0, base=5.0, max_dollars=20.0) == 10.0
    assert sizing.kelly_dollars(0.80, 0.45, 1000.0, base=5.0, max_dollars=20.0) == 20.0
    assert sizing.kelly_dollars(0.66, 0.60, 200.0, base=5.0, max_dollars=20.0) == 5.64  # thin
    assert sizing.kelly_dollars(0.66, 0.60, 100.0, base=5.0, max_dollars=20.0) == 5.0  # base
    assert sizing.kelly_dollars(0.80, 0.45, None, base=5.0, max_dollars=20.0) == 5.0


def test_track_record_gate_and_calibration(tmp_path):
    rec = sizing.TrackRecord()
    assert rec.calibrated(0.70) == 0.70 and not rec.allows_scaling(0.70)
    for i in range(sizing.MIN_TIER_RESULTS):
        rec.add(0.70, 1.0 if i % 2 == 0 else -0.5)  # 50% won, positive net
    assert rec.allows_scaling(0.70) and not rec.allows_scaling(0.80)
    # 20 results at 50% shrink a 0.70 claim halfway toward 0.50
    assert rec.calibrated(0.70) == pytest.approx(0.60)
    rec.add(0.90, -3.0)
    assert not rec.allows_scaling(0.90)
    rec.add(None, 1.0)
    assert rec.results == sizing.MIN_TIER_RESULTS + 2
    assert "scaling on" in rec.describe()
    losing = sizing.TrackRecord()
    for _ in range(30):
        losing.add(0.70, -1.0)
    assert not losing.allows_scaling(0.70)  # enough results, negative net


def _files(tmp_path, n, p_yes, net):
    live = LoopState()
    paper = LoopState()
    dec = tmp_path / "decisions.jsonl"
    with dec.open("w") as fh:
        for i in range(n):
            ticker = f"KXBTC15M-{i}"
            fh.write(
                json.dumps(
                    {
                        "ts": T0 + i * 900,
                        "action": "trade",
                        "ticker": ticker,
                        "side": "yes",
                        "inputs": {"p_yes": p_yes},
                    }
                )
                + "\n"
            )
            row = {
                "ticker": ticker,
                "side": "yes",
                "count": 10,
                "price": 0.5,
                "result": "yes",
                "won": True,
                "net": net,
                "settled_ts": T0 + i * 900 + 800,
            }
            (live if i % 2 else paper).history.append(row)
    live.save(tmp_path / "live.json")
    paper.save(tmp_path / "paper.json")
    return [tmp_path / "live.json", tmp_path / "paper.json"], dec


def test_track_record_loads_live_and_paper_without_double_counting(tmp_path):
    paths, dec = _files(tmp_path, 24, 0.72, 2.0)
    rec = sizing.TrackRecord.load([(p, dec) for p in paths], now=T0)
    assert rec.results == 24 and rec.tiers["0.65-0.75"].n == 24
    assert rec.allows_scaling(0.70)
    again = sizing.TrackRecord.load([(p, dec) for p in paths + [paths[0]]], now=T0)
    assert again.results == 24  # the same file twice is not twice the evidence
    assert sizing.TrackRecord.load([(tmp_path / "missing.json", None)], now=T0).results == 0
