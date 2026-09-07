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

## Sports research (`kalshi-sports`)

Research on Kalshi's game markets (MLB, NFL, NBA, college football and
basketball): catalog, recorder, sportsbook odds and ESPN feeds, a consensus
comparison. It places no orders.

| What | Where |
| --- | --- |
| Package | `kalshi_sports/` (every module) |
| Command | `kalshi-sports` (`kalshi_sports/cli.py`) |
| State (git-ignored) | `state/sports_data.sqlite` |
| Docs | `README.md` section "Sports research", `docs/sports-design.md`, `docs/sports-strategy.md`, `docs/sports-phase1.md` |
| Tests | `tests/test_sports_*.py`, `tests/sports_fixtures.py` |
| Server service | `sports-recorder` in `deploy/docker-compose.yml` |
| Settings | the block below the "SPORTS RESEARCH" banner in `.env.example` and `deploy/.env.server` (`ODDS_API_KEY`, `ODDS_API_REGIONS`, `ODDS_INTERVAL`, `SPORTS_LEAGUES`, `SCORES_INTERVAL_*`) |

## Shared

`kalshi_bot/client.py`, `kalshi_bot/models.py`, `kalshi_bot/auth.py`,
`kalshi_bot/config.py` and `kalshi_bot/fees.py` are used by both. The
sports package imports them and adds nothing to them beyond read-only
series and event endpoints. `pyproject.toml` installs both commands.
`deploy/Dockerfile` builds one image that carries both packages.

## Rules that keep them apart

* A sports change never edits a file in the crypto rows above, and the
  reverse, except the shared files listed.
* A new state file takes the project's prefix: `sports_` for sports;
  crypto files keep their existing names.
* Tests run in an isolated working directory (`tests/conftest.py`), so
  neither suite can write into the real `state/` folder.
