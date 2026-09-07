# Project map: which files belong to which project

This repository holds two projects that share a Kalshi client. They are
kept apart by package, command, state files, docs and tests, so that work
on one never touches the other.

## Crypto 15-minute desk (`kalshi-bot`)

The live trader for BTC and DOGE 15-minute markets, its paper twin, the
market-data recorder, the learner, the review and the Lewis Wealth Global
dashboard.

| What | Where |
| --- | --- |
| Package | `kalshi_bot/` (every module) |
| Command | `kalshi-bot` (`kalshi_bot/cli.py`) |
| Strategy, loop, sizing | `kalshi_bot/strategy.py`, `demo_loop.py`, `sizing.py`, `fees.py` |
| Research | `kalshi_bot/analysis.py`, `fairvalue.py`, `whale.py`, `quoting.py`, `learn.py`, `review.py` |
| Recorder | `kalshi_bot/recorder.py`, `spot.py`, `spot_ws.py`, `storage.py` |
| Dashboard | `kalshi_bot/demo_ui.py`, `dashboard_page.py`, `alerts.py` |
| State (git-ignored) | `state/live_loop.json`, `state/paper_loop.json`, `state/demo_loop.json`, `state/decisions.jsonl`, `state/paper_decisions.jsonl`, `state/alerts.jsonl`, `state/paper_alerts.jsonl`, `state/params.json`, `state/learn_history.jsonl`, `state/market_data.sqlite`, `state/STOP`, `state/PAUSE` |
| Docs | `README.md` (all sections above "Sports research"), `docs/HANDOFF.md`, `docs/research-brief.md`, `docs/PHONE_AND_SERVER.md`, `deploy/README.md` |
| Tests | `tests/test_*.py` except `tests/test_sports_*.py` and `tests/sports_fixtures.py` |
| Server services | `live`, `paper`, `dashboard`, `recorder`, `learn` in `deploy/docker-compose.yml` |
| Settings | the top block of `.env.example` and `deploy/.env.server` (`KALSHI_*`, `STRATEGY`, `MARGIN`, `TRADE_DOLLARS`, `LOSS_CAP`, `PROFIT_TARGET`, `DASHBOARD_*`) |

## Sports desk (`kalshi-sports`)

Research and trading on Kalshi's game markets (MLB, NFL, NBA, college
football and basketball): catalog, recorder, sportsbook odds and ESPN feeds,
the consensus comparison, and since 2026-09-07 a trading loop (paper and
live) with a persisted risk engine and an hourly learning cycle. Its live
loop places real orders only behind `kalshi-sports live-trade`'s gates.

| What | Where |
| --- | --- |
| Package | `kalshi_sports/` (every module) |
| Command | `kalshi-sports` (`kalshi_sports/cli.py`) |
| Strategy, loop, risk, learning | `kalshi_sports/strategy.py`, `trader.py`, `risk.py`, `learn.py` |
| Research | `kalshi_sports/catalog.py`, `teams.py`, `leagues.py`, `matching.py`, `consensus.py`, `compare.py`, `feeds/devig.py` |
| Recorder and feeds | `kalshi_sports/recorder.py`, `storage.py`, `feeds/odds.py`, `feeds/scores.py`, `config.py` |
| Dashboard | the "Sports" tab in `kalshi_bot/dashboard_page.py` and `sports_snapshot`, `sports_control`, `/api/sports` in `kalshi_bot/demo_ui.py` (the only sports code inside `kalshi_bot/`) |
| State (git-ignored) | `state/sports_data.sqlite` (recorder tables plus `decisions`, `positions`, `limits`), `state/sports_live.json`, `state/sports_paper.json`, `state/sports_params.json`, `state/sports_alerts.jsonl`, `state/SPORTS_STOP`, `state/SPORTS_PAUSE` |
| Docs | `README.md` section "Sports (kalshi-sports)", `docs/sports-design.md`, `docs/sports-strategy.md`, `docs/sports-phase1.md`, `docs/sports-trading.md`, `docs/RUNNING_BOTH.md` |
| Tests | `tests/test_sports_*.py`, `tests/sports_fixtures.py` |
| Server services | `sports-recorder`, `sports-paper` in `deploy/docker-compose.yml` |
| Settings | the sports block in `.env.example` and `deploy/.env.server` (`ODDS_API_KEY`, `ODDS_API_REGIONS`, `ODDS_INTERVAL`, `SPORTS_LEAGUES`, `SCORES_INTERVAL_*`) |

## Shared

`kalshi_bot/client.py`, `kalshi_bot/models.py`, `kalshi_bot/auth.py`,
`kalshi_bot/config.py`, `kalshi_bot/fees.py`, `kalshi_bot/alerts.py`
(the `AlertLog` class, pointed at a sports file) and `kalshi_bot/sizing.py`
(`kelly_dollars`) are used by both. The sports package imports them and
adds nothing to them beyond read-only series and event endpoints. One
dashboard process (`kalshi-bot demo-ui`) serves both desks: the crypto tabs
read the crypto files, the Sports tab reads the sports files. `pyproject.toml` installs both commands.
`deploy/Dockerfile` builds one image that carries both packages.

## Rules that keep them apart

* A sports change never edits a file in the crypto rows above, and the
  reverse, except the shared files listed and the sports parts of the two
  dashboard files, which a crypto change must leave in place.
* The two loops share one Kalshi account but different exchange shards:
  crypto on shard 2, MLB on shard 3, the other sports on shard 0. The
  sports trader ignores non-sports positions when it reconciles; the crypto
  loop must likewise not touch positions on `KX<LEAGUE>...` series.
* Stop and pause files are per desk: `state/STOP` and `state/PAUSE` stop
  the crypto loop only; `state/SPORTS_STOP` and `state/SPORTS_PAUSE` the
  sports loop only.
* A new state file takes the project's prefix: `sports_` for sports;
  crypto files keep their existing names.
* Tests run in an isolated working directory (`tests/conftest.py`), so
  neither suite can write into the real `state/` folder.
