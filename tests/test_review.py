import dataclasses
import json

import pytest

from kalshi_bot import review
from kalshi_bot.demo_loop import LoopState

T0 = 1_800_000_000.0


def _files(tmp_path, n=12):
    state = LoopState()
    dec = tmp_path / "decisions.jsonl"
    with dec.open("w") as fh:
        for i in range(n):
            ticker = f"KXBTC15M-{i}"
            side = "no" if i % 2 else "yes"
            p_yes = 0.52 if i < 6 else 0.80  # first half coin flips, second half confident
            fh.write(
                json.dumps(
                    {
                        "ts": T0 + i * 900,
                        "action": "trade",
                        "ticker": ticker,
                        "side": side,
                        "edge": 0.05,
                        "reason": "fair value",
                        "inputs": {
                            "p_yes": p_yes if side == "yes" else 1 - p_yes,
                            "secs_to_close": 120 if i < 6 else 500,
                            "spot_vs_strike_bps": 3.0 if i < 6 else 30.0,
                            "trend_bps": 15.0 if side == "no" else 0.0,
                        },
                    }
                )
                + "\n"
            )
            won = i >= 6
            state.history.append(
                {
                    "series": "KXBTC15M",
                    "ticker": ticker,
                    "side": side,
                    "count": 10,
                    "price": 0.45,
                    "result": "sold" if i == 0 else ("yes" if won == (side == "yes") else "no"),
                    "sold_at": 0.40 if i == 0 else None,
                    "won": won,
                    "net": 4.0 if won else -5.0,
                    "settled_ts": T0 + i * 900 + 800,
                }
            )
    state.save(tmp_path / "live.json")
    return tmp_path / "live.json", dec


def test_attribution_joins_entries_and_cuts(tmp_path):
    state, dec = _files(tmp_path)
    rows = review.attribute(review.load_history(state), review.load_decisions(dec))
    assert len(rows) == 12 and all(r["p_side"] is not None for r in rows)
    assert rows[0]["how"] == "sold" and rows[1]["how"] == "settled"
    conf = dict(review.cut(rows, "p_side", review.CONFIDENCE))
    assert conf["<0.55"]["n"] == 6 and conf["<0.55"]["net"] == -30.0
    assert conf[">=0.80"]["win_rate"] == 1.0
    gross, fees = review.fee_share(rows)
    assert fees > 0 and gross > sum(r["net"] for r in rows)
    tips = review.suggest(rows)
    assert any("min-confidence" in t for t in tips)
    assert any("under 3 minutes" in t for t in tips)
    assert any("from the strike" in t for t in tips)
    text = review.report(state, dec)
    assert "model confidence" in text and "what the numbers support" in text
    assert "12/12" in text


def test_review_flags_harvested_winners_and_held_losers(tmp_path):
    state = LoopState()
    for i in range(20):
        sold = i % 2 == 0
        state.history.append(
            {
                "ticker": f"KXBTC15M-{i}",
                "side": "yes",
                "count": 10,
                "price": 0.5,
                "result": "sold" if sold else "no",
                "sold_at": 0.55 if sold else None,
                "won": sold,
                "net": 0.3 if sold else -5.2,
                "settled_ts": T0 + i * 900,
            }
        )
    state.save(tmp_path / "s.json")
    text = review.report(tmp_path / "s.json", None)
    assert "winners are being sold small" in text
    assert "break-even win rate" in text


def test_paper_trade_cli(monkeypatch, capsys):
    import kalshi_bot.cli as cli

    parser = cli.build_parser()
    args = parser.parse_args(["paper-trade", "--dollars", "5"])
    assert args.state_file == "state/paper_loop.json" and args.strategy == "fairvalue"
    assert args.default_env == "prod" and args.loss_cap == 50.0
    demo = dataclasses.replace(cli.Settings.from_env("/nonexistent"), env="demo")
    with pytest.raises(SystemExit):
        cli.cmd_paper_trade(demo, args)  # demo env
    prod = dataclasses.replace(demo, env="prod", dry_run=False)
    seen = {}

    class FakeLoop:
        def __init__(self, client, cfg, allow_production=False):
            seen["dry_run"] = client.dry_run
            seen["entry"] = cfg.entry
            seen["allow"] = allow_production
            self.state = LoopState()

        def run(self):
            return "test"

    class FakeClient:
        dry_run = True

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(cli, "_client", lambda settings, need_auth: FakeClient())
    monkeypatch.setattr("kalshi_bot.demo_loop.DemoLoop", FakeLoop)
    assert cli.cmd_paper_trade(prod, args) == 0
    assert seen == {"dry_run": True, "entry": "taker", "allow": True}
    assert "PAPER" in capsys.readouterr().out


def test_review_handles_missing_and_small(tmp_path):
    assert "nothing booked" in review.report(tmp_path / "none.json", None)
    (tmp_path / "bad.json").write_text("{nope")
    assert review.load_history(tmp_path / "bad.json") == []
    assert review.load_decisions(tmp_path / "missing.jsonl") == []
    state = LoopState()
    state.history.append(
        {
            "ticker": "KXBTC15M-1",
            "side": "yes",
            "count": 2,
            "price": 0.5,
            "result": "no",
            "won": False,
            "net": -1.0,
            "settled_ts": T0,
        }
    )
    state.save(tmp_path / "s.json")
    text = review.report(tmp_path / "s.json", None)
    assert "anecdotes" in text and "0/1" in text


def test_review_cli(tmp_path, capsys):
    import kalshi_bot.cli as cli

    state, dec = _files(tmp_path)
    args = cli.build_parser().parse_args(
        ["review", "--live-state", str(state), "--decisions", str(dec)]
    )
    assert args.func(None, args) == 0
    assert "what the numbers support" in capsys.readouterr().out
    cfg = cli._loop_config(cli.build_parser().parse_args(["live-trade", "--dollars", "5"]))
    assert cfg.min_confidence == 0.65
