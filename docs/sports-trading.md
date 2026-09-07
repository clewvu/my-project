# Sports trading runbook: paper, live, learn, dashboard

Status: built 2026-09-07 at Cameron's request to run the sports strategy with
real money now, ahead of the pre-registered gate in `docs/sports-design.md`.
The gate still stands as the measure of whether this has edge; the live loop
is the forward test with money on, at small size, behind hard limits.

## What runs

`kalshi-sports paper-trade` and `kalshi-sports live-trade` run the same loop
(`kalshi_sports/trader.py`) with the consensus-gap strategy
(`kalshi_sports/strategy.py`): buy the side of a Kalshi moneyline whose
de-vigged sharp-book consensus beats the ask by the fee plus a margin, when
at least three books quote it, at least one is sharp (Pinnacle, BetOnline,
LowVig), the books agree within 4 points, the odds are under 15 minutes old,
the game starts inside 24 hours but not within 15 minutes, the price is
between 0.15 and 0.85, and the spread is at most 6 cents. One position per
game. Taker order at the ask, immediate-or-cancel, on the live book only
after the recorder's quote and a fresh odds pull both agree.

The loop needs the recorder running on the same database: it reads the
recorder's markets, events, snapshots and odds, and pulls odds itself only
to confirm a signal (at most once per league per five minutes).

Settlement comes from the recorder's markets table, or from the exchange four
hours after the start. At the game's start the loop marks two closing prices
per position: Kalshi's mid and the consensus probability. Their difference
from the entry is the closing-line value (CLV), the fastest honest measure of
edge (`docs/sports-strategy.md` section 1).

## Files: sports versus crypto

Everything the sports desk writes is prefixed or separate. Nothing is shared
with the crypto loop, and neither loop's stop or pause file affects the other.

| purpose | crypto loop (`kalshi-bot`) | sports desk (`kalshi-sports`) |
| --- | --- | --- |
| package, command | `kalshi_bot/`, `kalshi-bot` | `kalshi_sports/`, `kalshi-sports` |
| recorded data | `state/market_data.sqlite` | `state/sports_data.sqlite` |
| loop state / status | `state/live_loop.json`, `state/paper_loop.json` | `state/sports_live.json`, `state/sports_paper.json` |
| strategy parameters | `state/params.json` | `state/sports_params.json` |
| decisions and positions | `state/decisions.jsonl`, loop state | tables `decisions`, `positions`, `limits` in the sports database |
| alerts | `state/alerts.jsonl` | `state/sports_alerts.jsonl` |
| stop / pause | `state/STOP`, `state/PAUSE` | `state/SPORTS_STOP`, `state/SPORTS_PAUSE` |
| dashboard | top of the page, `/api/state` | "Sports desk" block, `/api/sports` |
| docs | `docs/HANDOFF.md`, `docs/research-brief.md` | `docs/sports-*.md` |
| tests | `tests/test_*.py` | `tests/test_sports_*.py` |

## Gates on `live-trade`

All of these must hold or the command exits before doing anything:

1. `--env prod` (the default for `kalshi-sports`) and `KALSHI_DRY_RUN=false`.
2. API credentials present; `ODDS_API_KEY` present.
3. `--real-money` on the command line.
4. `--dollars` and `--max-dollars` at most $20; `--loss-cap` at most $50.
5. The recorder's newest snapshot is under 15 minutes old.
6. The typed word `TRADE` at the prompt (`--yes` skips it, for servers).

Then the client is built with `allow_live=True`, which is the only way an
order reaches production. Every other command uses a client that refuses.

## Risk engine (persisted, `kalshi_sports/risk.py`)

Counters live in the sports database's `limits` table, so a restart cannot
reset them. Defaults, all adjustable on the command line:

- daily realised loss cap $25 (Eastern day), total loss cap $50 (`--reset-limits` clears)
- per trade and per game at most `--max-dollars` ($10), at most 5% of the shard's balance
- at most 10 open positions and $60 open at once
- 4 consecutive losses pause new entries for 6 hours
- `state/SPORTS_STOP` ends the loop; `state/SPORTS_PAUSE` keeps it booking
  settlements and marking CLV but opens nothing new (the dashboard buttons
  write these files)

Sizing: base stake times the learned `size_scale`, capped by `--max-dollars`
and by a quarter-Kelly stake on the calibrated probability. Kalshi shards:
MLB orders draw on shard 3, the other leagues on shard 0; the command prints
each shard's balance and flags one that cannot cover a trade
(`kalshi-bot transfer` moves funds).

## Self-learning (`kalshi_sports/learn.py`)

Every hour the loop reviews settled positions: CLV against the consensus and
against Kalshi's close with bootstrap confidence intervals, net after fees,
and the same by league, price, edge and lead-time bucket. It changes
`state/sports_params.json` only through a gate, and the loop reloads the
file within a tick:

- fewer than 30 settled results: hold
- CLV versus consensus significantly negative: margin +0.01, size halved
- last 20 results net negative with at most 6 wins: size halved
- 50 or more results, CLV significantly positive, net positive: size ×1.25,
  never above `--max-dollars` / `--dollars`
- otherwise hold

`kalshi-sports learn --mode live` prints the review and the proposal;
`--apply` writes it. Increases to the margin's floor or the loss caps are
never automatic.

## Commands

```powershell
kalshi-sports paper-trade                       # simulated fills on live quotes
kalshi-sports --env prod live-trade --real-money --dollars 5 --max-dollars 10 --loss-cap 50
kalshi-sports positions                         # open and settled, P&L, CLV
kalshi-sports learn --mode live [--apply]       # review and parameter proposal
kalshi-bot demo-ui                              # dashboard; the Sports desk block is at the bottom
```

Windows: run the recorder, the trader and the dashboard in three PowerShell
windows with the venv activated in each.

## What to expect

Most candidates are skipped, and the decision log says why. In September
the leagues with markets are MLB and NFL; a handful of signals a day is
normal. The first results say little: at $5 a trade the first week's net is
noise. Read the CLV first. If it sits negative with a confidence interval
below zero after 30 results, the learner will already have tightened; that
is also the point to stop and reconsider, as the design says.
