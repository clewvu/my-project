# Sports phase 1: probe and record

Status: code landed 2026-09-07 (`kalshi_sports/`, `kalshi-sports` command);
probe run the same evening, findings in section 1a. Nothing here trades.
This file is the checklist for the first afternoon and the first week; paste
each command's output back into the chat.

## 0. Install

```powershell
git pull
pip install -e ".[dev]"
pytest -q            # 243 passing at the time of writing
kalshi-sports --help
```

`kalshi-sports` defaults to production (`--env prod`) because sports markets
are not listed on the demo exchange. It never needs the API key: every call
it makes is a public read.

## 1. Probe Kalshi (answers the design's "verify" items)

```powershell
kalshi-sports discover --list-series --events --rules
```

What to look for, in order:

1. **Which series exist.** The `/series` block lists everything Kalshi
   returns for the Sports category with a KX prefix or a league word in it.
   The `candidate series` block then shows how many open markets each of
   our guessed tickers has. A `0 open` or `ERROR` on a guess means the
   ticker is wrong; find the real one in the `/series` block and record it
   in `kalshi_sports/leagues.py` (`kalshi_series`).
2. **Ticker grammar.** For each market printed, compare the parsed
   `away@home side= line=` with the title. If the parser got a team wrong,
   paste three raw markets (`--raw --show 3`) and I will fix `catalog.py`.
3. **Shard.** `shard=` should be 3 (Tennis & Baseball) for MLB per the
   crypto handoff; note what NFL, NBA and college show. Funding a shard is
   already automated in `kalshi-bot transfer`.
4. **Rules.** The `rules:` line says how postponements, suspended games and
   doubleheaders settle. Paste one per league.
5. **Start times.** With `--events`, `game_date` and the event's
   `strike_date` should agree. If the event record has no start time,
   the recorder falls back to slow cadence until the market's close nears.

If `GET /series` fails or returns nothing useful, run:

```powershell
kalshi-bot markets --series KXMLBGAME --raw
```

and paste the first market: the `event_ticker`, `title`, `yes_sub_title`,
`rules_primary` and `exchange_index` fields are what I need.

## 1a. What the probe confirmed (2026-09-07)

Cameron ran `discover --list-series --events --rules` and `scores-test mlb`.
Everything below is now coded rather than guessed.

- **Series.** `KXMLBGAME`, `KXMLBSPREAD`, `KXMLBTOTAL` (76 / 110 / 192 open);
  `KXNFLGAME`, `KXNFLSPREAD`, `KXNFLTOTAL` (64 / 300+ / 300+);
  `KXNBAGAME` (6 open, preseason; spread and total series exist but were
  empty); `KXNCAAFGAME`, `KXNCAAFSPREAD`, `KXNCAAFTOTAL` (300+ each).
  College basketball is `KXNCAAMBGAME`, `KXNCAAMBSPREAD`, `KXNCAAMBTOTAL`
  (off-season, empty). Kalshi also lists first-five-innings, first-half,
  quarter, team-total and player markets per league; not recorded yet.
- **Ticker grammar.** `KXMLBGAME-26SEP101610TEXSEA-TEX`: date, then for MLB
  the start time in Eastern (`1610`), then AWAY HOME, then the YES side.
  NFL, NBA and college omit the time. Spread sides append a digit
  (`DET6` for "wins by over 5.5"); totals use the integer above the line
  (`14` for 13.5). The line itself is `floor_strike`.
- **Rules text** always names the matchup and schedule: "the Texas vs
  Seattle professional baseball game originally scheduled for Sep 10, 2026
  at 4:10 PM EDT". Away is first. NFL and college rules give the date only,
  so their start is approximate (13:00 Eastern) until ESPN's schedule
  refines it; the recorder does this automatically.
- **Team names on Kalshi** are city-style with a disambiguating initial:
  "Los Angeles D", "New York G", "San Jose St."; ticker abbreviations are
  the sportsbook style (LAD, NYG, SJSU). Both are stored.
- **Shards.** MLB is exchange shard 3; NFL, NBA and college are shard 0.
- **Close time** is three days after the game, not the start.
- **Scale.** Well over a thousand open markets with college football in;
  one burst of requests drew a 429. The recorder therefore takes a light
  snapshot of every market from the list call (no per-market request) and
  polls books and trades only inside 24 hours of the start.
- **ESPN** works without a key; abbreviations match the team tables
  (WSH, SD, KC, ATH) and start times are exact.
- **College spreads** are wide (a 0.09 bid against a 0.85 ask on a
  Mountain West spread): most college markets will fail the liquidity
  filter, which is fine; the moneylines on the big games are the target.

## 2. Test the two external feeds

```powershell
kalshi-sports scores-test mlb
kalshi-sports scores-test ncaaf --show 5 --raw
```

ESPN needs no key. Check that games list with home/away, abbreviations and
a `pre` / `in` / `post` state. The `--raw` block shows the `situation`
record (outs, runners, possession) that the in-play work will use later.

The odds feed needs a key from the-odds-api.com. The free tier is 500
requests a month; with five leagues the recorder's default of one poll per
league every 30 minutes is about 7,200 a month, so either set
`ODDS_INTERVAL=21600` on the free tier (one poll per league every six hours,
enough for the pre-game consensus but not for line-movement features) or
take the $30-a-month tier. Put the key in `.env` as `ODDS_API_KEY=` and run:

```powershell
kalshi-sports odds-test mlb
```

It costs one request and prints the games, the books returned, and the
remaining quota. If `pinnacle` is not among the books, tell me: the sharp
weighting in `consensus.py` then falls back to an unweighted average and we
should look at a source that carries Pinnacle.

## 3. Start recording

```powershell
kalshi-sports record
```

Leave it running in its own window (or add the `sports-recorder` service in
`deploy/docker-compose.yml` on the server). It writes
`state/sports_data.sqlite`, separate from the crypto database. The first
tick lists every series (a few seconds), writes a light snapshot for every
open market, then polls books and trades only for games inside 24 hours,
every five minutes far out and every five seconds in the last half hour and
in play. The market lists refresh every five minutes (`--discover-every`);
the book window is `--book-window 24` hours.

Then, after an hour and again after a day:

```powershell
kalshi-sports record-stats
kalshi-sports record-dump
kalshi-sports compare --league mlb --games
```

`record-stats` shows `unparsed_teams` per series (should be zero) and how
many events have an exact start time (MLB from the ticker, the rest once
ESPN has been polled on game day). `compare` prints the first evidence table: Kalshi bid and ask, the
de-vigged consensus for the YES team, the edge on each side net of fee,
how many books and how far apart they are, and how old the quotes are. It
is a report of a gap, not a signal; the gate for H1 is defined in
`docs/sports-design.md` section 2 and needs weeks of these rows.

## 4. What phase 1 delivers, and what it does not

Delivered:

- League registry (MLB, NFL, NBA, NCAA football, NCAA men's basketball)
  with candidate Kalshi series, The Odds API keys and ESPN paths.
- Market catalogue: tolerant parsing of ticker, title and event into
  league, date, away, home, YES side and line; team-name normalisation.
- Recorder with per-market cadence, settlement capture, odds and game
  state feeds, deduplicated state rows, error isolation.
- De-vig (multiplicative, power, Shin), sharp-weighted consensus with
  dispersion and age, game matching across the three sources, and the
  `compare` report.
- 37 tests, all HTTP mocked.

Not yet (phase 2 and 3 in the design): the core extraction from
`kalshi_bot`, the execution state machine, the persisted risk engine, the
feature store and the CLV report, the backtester. None of those is useful
before the recorder has data and the grammar is confirmed.
