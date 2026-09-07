# Running both desks at once on one machine

This is the operating brief for the whole repository: what the two trading
loops are, where every file lives, how they share one Kalshi account and one
dashboard without touching each other, and the exact windows to open. It is
written so that either Claude session (crypto or sports) or Cameron can read
it cold. `docs/PROJECT_MAP.md` is the file-ownership table; this document is
the runbook.

## 1. The repository in one paragraph

`clewvu/my-project`, cloned at `C:\Users\lewiscc2\kalshi-bot` with a
virtualenv at `.venv`. One Python package install (`pip install -e ".[dev]"`)
provides two commands. `kalshi-bot` is the crypto desk: a live trader on
Kalshi's 15-minute BTC and DOGE markets, its recorder, learner, review, and
the Lewis Wealth Global dashboard. `kalshi-sports` is the sports desk: a
recorder for MLB, NFL, NBA and college game markets with sportsbook odds and
ESPN game state, and a trader that buys the side of a moneyline the sharp
books say is underpriced. Both live on the git branch
`claude/kalsi-crypto-bot-memory-2frkn0`. Both read the same `.env` and the
same Kalshi API key. Everything either loop writes goes under `state/`
(git-ignored), with sports files prefixed `sports_` or `SPORTS_`.

## 2. Two loops, one account, four shards

Kalshi runs several exchange instances ("shards"), each with its own balance.
An order draws only on the balance of the shard its market lives on, and
`kalshi-bot transfer --amount N --source S --to D` moves money between them
(the `status` command prints the breakdown).

| shard | what trades there | which loop |
| --- | --- | --- |
| 0 | NFL, NBA, college football and basketball games | sports |
| 2 | 15-minute crypto (`KXBTC15M`, `KXDOGE15M`) | crypto |
| 3 | MLB games | sports |
| 1 | combos | neither |

Rules that follow from this:

- Each loop funds and reads only its own shards. Neither sweeps money off
  the other's shard. Balance checks in each loop use the shard of the market
  being traded.
- Positions are recognised by series prefix. The sports trader ignores any
  exchange position that is not on a configured sports series when it
  reconciles at startup (it once adopted a `KXBTC15M` position; fixed). The
  crypto loop must equally ignore `KXMLB*`, `KXNFL*`, `KXNBA*`, `KXNCAA*`
  positions in its own reconciliation.
- Kalshi's rate limit is per account, not per process. Both loops throttle
  at 0.15 s between requests; the sports recorder additionally polls books
  only inside 24 hours of a game. If either side sees repeated 429s, raise
  `KALSHI_MIN_REQUEST_INTERVAL` in `.env` (both loops read it).

## 3. Files, side by side

| purpose | crypto desk (`kalshi-bot`) | sports desk (`kalshi-sports`) |
| --- | --- | --- |
| package | `kalshi_bot/` | `kalshi_sports/` |
| shared code | `kalshi_bot/client.py`, `models.py`, `auth.py`, `config.py`, `fees.py`, `alerts.py`, `sizing.py` (imported by sports, owned by crypto; change with care) | |
| recorded data | `state/market_data.sqlite` | `state/sports_data.sqlite` |
| loop state / status | `state/live_loop.json`, `state/paper_loop.json`, `state/demo_loop.json` | `state/sports_live.json`, `state/sports_paper.json` |
| decisions | `state/decisions.jsonl`, `state/paper_decisions.jsonl` | table `decisions` in the sports database |
| positions | inside the loop state JSON | table `positions` in the sports database |
| risk counters | inside the loop state JSON | table `limits` in the sports database |
| learned parameters | `state/params.json`, `state/learn_history.jsonl` | `state/sports_params.json` |
| alerts | `state/alerts.jsonl`, `state/paper_alerts.jsonl` | `state/sports_alerts.jsonl` |
| stop / pause | `state/STOP`, `state/PAUSE` | `state/SPORTS_STOP`, `state/SPORTS_PAUSE` |
| dashboard | tabs Overview, Trades, Analysis, Activity; `/api/state`, `/api/analysis`, `/api/decisions` | tab Sports; `/api/sports`, `/api/sports/{stop,clear-stop,pause,resume}` |
| docs | `docs/HANDOFF.md`, `docs/research-brief.md`, `docs/PHONE_AND_SERVER.md`, `deploy/README.md` | `docs/sports-design.md`, `docs/sports-strategy.md`, `docs/sports-phase1.md`, `docs/sports-trading.md` |
| tests | `tests/test_*.py` except sports | `tests/test_sports_*.py`, `tests/sports_fixtures.py`, `tests/test_sports_dashboard.py` |
| compose services | `live`, `paper`, `dashboard`, `recorder`, `learn` | `sports-recorder`, `sports-paper` |
| `.env` settings | `KALSHI_*`, `DASHBOARD_PASSWORD`, `DASHBOARD_BIND` | `ODDS_API_KEY`, `ODDS_API_REGIONS`, `ODDS_INTERVAL`, `SPORTS_LEAGUES`, `SCORES_INTERVAL_*` |

The dashboard is one process. `kalshi_bot/demo_ui.py` and
`kalshi_bot/dashboard_page.py` belong to the crypto desk, but they contain
the Sports tab and its three server methods (`sports_snapshot`,
`sports_control`, the `/api/sports` routes). A crypto change to those files
keeps that code; a sports change touches nothing else in them.

## 4. What each loop does, briefly

**Crypto (`kalshi-bot live-trade`).** Every 15-minute BTC and DOGE market:
a fair-value model from Coinbase spot and realised volatility, entering when
the model beats the ask by the fee plus a margin, with maker-first entries,
exits, churn control, a consecutive-loss breaker, fixed-fraction sizing that
scales by confidence tier once a tier has earned it, and an hourly learner
(`kalshi-bot learn`) that promotes parameters through a gate. Details in
`docs/HANDOFF.md` and the README.

**Sports (`kalshi-sports live-trade`).** Every 30 seconds: candidate
moneylines from the sports recorder's database, matched to sportsbook odds;
the de-vigged consensus of the books (Pinnacle, BetOnline and LowVig weighted
as sharp) against Kalshi's ask; buy when the gap clears the fee plus a
margin, books agree, odds are fresh, the game is inside 24 hours but not
within 15 minutes. One position per game, immediate-or-cancel taker order on
the live book. Persisted daily and total loss caps, exposure caps, a loss
breaker. At game start it marks closing-line value; at settlement it books
the result. Hourly it reviews CLV and tightens or, past a gate, loosens
size. Details in `docs/sports-trading.md` and `docs/sports-strategy.md`.

## 5. The windows

Activate the venv in every window: `cd C:\Users\lewiscc2\kalshi-bot;
.\.venv\Scripts\Activate.ps1`. All on the merged branch:

```powershell
git checkout claude/kalsi-crypto-bot-memory-2frkn0
git pull origin claude/kalsi-crypto-bot-memory-2frkn0
pip install -e ".[dev]"
```

| window | command | notes |
| --- | --- | --- |
| 1 | `kalshi-bot record` | crypto recorder: books, trades, settlements, spot |
| 2 | `kalshi-sports record` | sports recorder; startup line must say `odds=on` |
| 3 | `kalshi-bot --env prod live-trade --dollars 10 --real-money` | crypto trader; typed `TRADE` |
| 4 | `kalshi-sports --env prod live-trade --real-money --dollars 5 --max-dollars 10 --loss-cap 50` | sports trader; typed `TRADE`; fund shard 3 first for MLB |
| 5 | `kalshi-bot learn --every 3600` | crypto learner (the sports learner runs inside its trader) |
| 6 | `kalshi-bot demo-ui` | one dashboard for both at http://127.0.0.1:8765 |

Optional: `kalshi-sports paper-trade` instead of window 4 runs the sports
strategy with simulated fills. Both traders write a status file every tick;
the dashboard shows "no heartbeat" for a loop whose file has gone quiet.

Stopping: Ctrl-C in the window, or the dashboard buttons, which write
`state/STOP` (crypto) or `state/SPORTS_STOP` (sports). Pausing keeps a loop
managing its open positions but opens nothing new.

## 6. Git etiquette between the two sessions

- Both sessions commit to `claude/kalsi-crypto-bot-memory-2frkn0`. Always
  `git pull origin claude/kalsi-crypto-bot-memory-2frkn0` before pushing;
  a rejected push means pull, merge, run `pytest -q`, push again.
- Run `ruff check . && ruff format .` and `pytest -q` before every push.
  Both suites run together (about 290 tests); the suite runs in an isolated
  working directory so it never writes into the real `state/` folder.
- Each session edits only its own rows in `docs/PROJECT_MAP.md`. A change
  to a shared file (`client.py`, `models.py`, `alerts.py`, `sizing.py`) is
  announced in the commit message so the other session sees it.
- Cameron's clone must be on the merged branch; `git status` should say
  `On branch claude/kalsi-crypto-bot-memory-2frkn0`.

## 7. What to watch in the first days

- Crypto: its own review (`kalshi-bot review`) and the Analysis tab.
- Sports: `kalshi-sports positions` and `kalshi-sports learn --mode live`.
  Read CLV before P&L; at $5 a trade the first week's P&L is noise.
- Both: the Activity tabs for `warn` and `halt` events, the shard balances
  (`kalshi-bot status`), and the odds quota line in the sports recorder's
  log (20,000 credits a month; the recorder skips off-season leagues).
