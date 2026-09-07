# Design: Kalshi sports trading automation (baseball first)

Status: proposal, 2026-09-07. Companion to `docs/research-brief.md` (crypto)
and `docs/HANDOFF.md`. Anything marked **verify** is a belief about Kalshi or
a data source that must be confirmed against production before code depends
on it; the sandbox cannot reach those hosts, so Cameron runs the probe
commands locally and pastes the output.

## 0. What the crypto bot taught us, and what changes for sports

The 15-minute crypto bot was built as research tooling first and then pushed
live before its gate passed. The live result (124 trades, net -47.19, 50%
wins, no edge before fees) is the honest baseline: the market was a fair coin
flip with a 1.75-cent toll on every contract, and the exits harvested the good
half of coin flips while losers were held to settlement.

Sports markets differ in ways that cut both directions:

| property | 15-minute crypto | MLB game markets |
| --- | --- | --- |
| lifetime of a market | 15 minutes | 12 to 48 hours pre-game, then 3 hours in play |
| what moves the price | one public spot feed everyone sees | lineups, pitchers, weather, injuries, and a $200B/year sportsbook industry pricing the same event |
| external reference price | Coinbase spot (public, sub-second) | sharp sportsbook lines (efficient, but paywalled or scraped) and free play-by-play state |
| liquidity | deep, 1-cent spread | thin to moderate; multi-cent spreads common, especially off-peak **verify** |
| settlement | index average, mechanical | official league result, with postponements, suspended games, and void rules |
| decisions per day | ~100 per series | ~15 games, a few markets each, so 30 to 60 candidate bets |
| correlation between positions | BTC and DOGE closing together | moneyline, run line, total, and series markets on the same game are strongly correlated |
| fee at 50 cents | 1.75 cents | same schedule (7% x p x (1-p), rounded up per order) **verify for sports** |

The consequence: the plausible edge in sports is *informational and
comparative* (Kalshi disagreeing with a better-informed price), not
*mechanical* (a formula on a public feed). That is good news, because a
comparative edge is measurable quickly through closing line value without
waiting for thousands of settlements, and bad news, because it depends on an
external data source that costs money or has terms of service.

## 1. Goal, bankroll, and honest expectations

Goal: an unattended trader on Kalshi's baseball markets that only stakes
money where a pre-registered test has shown positive expectation, sized so
that a bad month is survivable, and that keeps learning from its own
decisions.

Bankroll reality. The account holds about $140. A 1% position is $1.40, below
the practical minimum of a few contracts at 40 to 60 cents. The plan below
runs paper trading and CLV measurement at zero cost until a gate passes; when
it does, a bankroll of $500 or more is the smallest that lets fixed-fraction
sizing work. Until then the correct stake is zero.

Season timing. Today is 2026-09-07. The MLB regular season ends 2026-09-27
and the postseason runs through late October, so about seven weeks of
baseball remain this year, with far fewer games per day in October. Two
consequences: start the recorder now, and build the pipeline sport-agnostic
so NFL (in season) and NBA (from late October) feed the same tests. The
baseball-specific verdict may not be reachable until the 2027 season; the
sport-agnostic "Kalshi vs sharp consensus" verdict can be.

## 2. Edge hypotheses (pre-registered, in priority order)

Each hypothesis gets a test with a threshold set before data is seen. "No
edge found" is an acceptable outcome for each.

### H1. Consensus mispricing (primary)

Claim: when Kalshi's price disagrees with the de-vigged consensus of sharp
sportsbooks by more than fee plus margin, the consensus is right more often
than Kalshi.

- Reference: de-vigged implied probability from the sharpest books
  available (Pinnacle where obtainable, else a median of major books). De-vig
  method is a fitted choice: multiplicative, power, or Shin; pick on the
  training set by Brier score.
- Rule: buy the side whose consensus probability exceeds Kalshi's ask by at
  least fee plus margin (ladder 1 to 5 cents), pre-game only, no entry inside
  the last 30 minutes before first pitch unless the lineup is confirmed.
  Maker variant rests one tick inside the spread with expiry at first pitch.
- Primary statistic: **closing line value (CLV)** in probability points, the
  Kalshi or consensus closing price minus our entry price, per contract.
  CLV needs no settlement and has roughly a tenth of the variance of P&L, so a
  2-cent edge is detectable in a few hundred bets rather than a few thousand.
- Secondary: realised net P&L per contract after fees, hold to settlement.
- Gate: 300 qualifying signals, time-ordered 70/30 split, margin and de-vig
  fitted on the first 70%. Viable only if held-out mean CLV >= +1 cent with a
  95% cluster-bootstrap CI excluding zero (clusters are games) **and**
  held-out realised net is not significantly negative.

### H2. Internal consistency (arbitrage within Kalshi)

Claim: related Kalshi markets on the same game or series drift out of line
with each other (moneyline vs run line vs total via a simple runs model;
series winner vs the product of game prices; team futures vs game strips).

- Rule: when a combination of positions locks in a profit after fees at
  current asks, or a single leg is mispriced relative to the others by more
  than fee plus margin, trade the cheap leg.
- Gate: same as H1 but with realised net as the primary statistic (these
  positions are hedged, so P&L variance is low). 100 executions.
- Risk: legging. Both legs are needed; if the second fill fails the position
  is naked. Execution must be fill-or-kill on the second leg with an
  unwind rule.

### H3. In-play state model (phase 2, not before H1 has a verdict)

Claim: a win-probability model driven by free play-by-play state (inning,
outs, bases, score, batter and pitcher quality) updates faster or better than
Kalshi's in-play price.

- Reference: MLB Stats API live feed (`statsapi.mlb.com`, free, unofficial,
  roughly 5 to 20 seconds behind broadcast) **verify latency**.
- The honest concern: anyone with a faster feed (courtsiders, sportsbook
  traders) is on the other side of every in-play trade. The test must
  measure the *lag* between a state change and Kalshi's price move before any
  money is considered.
- Gate: as H1, plus a lag diagnostic showing Kalshi moves after our feed on
  the majority of scoring plays.

### H4. Market making on thin books (phase 3)

Claim: quoting both sides around a fair value from H1's consensus captures
spread on thin markets.

- Risk: adverse selection at news events (lineup changes, weather). Requires
  H1's reference price, an inventory limit, and a pull-quotes-on-news rule.
  Not before a live H1 record exists.

### Dropped or deferred

- Weather, lineup, and injury news as standalone signals: the sharp books
  already price them; use them only as *stale-data guards* (pull orders when
  the lineup changes).
- Scraping sportsbooks without permission: terms-of-service risk and
  brittleness. Prefer a licensed odds aggregator.
- Full Kelly, martingale, or any sizing that grows after losses.

## 3. Market facts to establish first (recorder audit for sports)

Before any strategy code, one afternoon of probing against production with
the existing client. Everything below is **verify**.

1. Series and tickers. `GET /series?category=Sports` (or listing markets by
   series) to find the MLB series: expected game winner, run line, total
   runs, first-five-innings, series winner, and futures. Record the exact
   series tickers, the ticker grammar (date, teams, side), and how the
   `title`, `subtitle`, `yes_sub_title`, and `expiration_value` fields
   describe the game and outcome. The existing `markets --series X --raw`
   command does this.
2. Exchange shard. The handoff records shard 3 as "Tennis & Baseball", so
   MLB orders draw on shard 3 and the `transfer` plumbing already works.
   Confirm from `exchange_index` on a live MLB market.
3. Lifecycle. When markets open (days ahead?), whether they stay open in
   play, and how they settle: `result`, `expiration_value`, timing after the
   final out, and the void rule for postponements and suspended games (the
   rules text in the market or event record).
4. Fees. Whether the 7% formula applies to sports and whether maker orders
   are charged. Read `fee_cost` on real fills of one contract.
5. Liquidity. Book depth and spread by time to first pitch and by market
   type, from snapshots. This decides whether maker or taker is the default
   and how many contracts a signal can absorb.
6. Rate limits and pagination for a few hundred open markets at once rather
   than two.

## 4. Architecture

Reuse the proven layers, generalise where the crypto assumptions leak, and
add what sports need. Packages:

```
kalshi_core/            extracted from kalshi_bot, sport- and asset-agnostic
  auth.py               unchanged
  client.py             unchanged API; add series listing and event endpoints
  models.py             Market gains: event_ticker grouping, rules text,
                        exchange_index, custom strike fields, market_type
  fees.py               unchanged; fee schedule per series once verified
  storage.py            MarketDataStore: markets, snapshots, trades
  execution.py          NEW: order state machine (see 4.3)
  risk.py               NEW: RiskEngine with persisted limits (see 4.4)
  portfolio.py          NEW: positions from fills, reconciled to exchange
  decisions.py          decision log / feature store (from strategy.DecisionLog)
  alerts.py, dashboard  unchanged, fed by the new loop

kalshi_crypto/          today's kalshi_bot minus the core (recorder, fairvalue,
                        whale, demo_loop, learn, sizing, review)

kalshi_sports/
  catalog.py            map Kalshi markets to games: league, date, home, away,
                        market type, line; team-name normalisation table
  feeds/
    odds.py             odds aggregator client -> normalised (book, market,
                        side, price, ts); de-vig functions
    mlb_stats.py        schedule, probable pitchers, lineups, live game state
    weather.py          optional; only for stale-data guards
  recorder.py           polls Kalshi books/trades for open sports markets,
                        plus odds and game state, into SQLite
  pricing.py            consensus probability per (game, market, side) with
                        freshness and dispersion
  strategies/
    consensus.py        H1
    consistency.py      H2
    inplay.py           H3 (later)
  backtest.py           replays the recorder database through the SAME
                        strategy and risk code as live (see 4.6)
  clv.py                closing-line-value report and gate verdict
  loop.py               the trading loop, built on kalshi_core.execution
  cli.py                kalshi-sports ...
```

### 4.1 Data model additions (SQLite, WAL, migrations as today)

- `events`: event_ticker, league, game_id (external), start_ts, home, away,
  status (scheduled, live, final, postponed), raw.
- `markets`: as today plus event_ticker, market_type (moneyline, runline,
  total, series, future), line (float, e.g. -1.5 or 8.5), side_label,
  exchange_index, rules_text, void_reason.
- `snapshots`: as today (best levels, depth, raw book), cadence adaptive:
  60 s pre-game, 5 s from 30 minutes before first pitch and in play.
- `odds`: ts, book, game_id, market_type, line, side, price_decimal,
  implied_p, raw. One row per book quote change.
- `consensus`: ts, game_id, market_type, line, side, p_devig, n_books,
  dispersion, method.
- `game_state`: ts, game_id, inning, half, outs, bases bitmask, home_score,
  away_score, batter_id, pitcher_id, raw.
- `decisions`: every evaluation, traded or not, with the full input vector
  (Kalshi bid/ask/depth, consensus p, dispersion, freshness, time to start,
  lineup confirmed flag, model p, signal, size, reason). This is the feature
  store for the learning loop and the CLV report.
- `orders`, `fills`, `positions`: mirrors of the exchange, written by the
  execution layer, reconciled periodically.
- `limits`: persisted risk counters (daily loss, per-game exposure,
  consecutive losses, breaker until) so a restart cannot reset them.

### 4.2 Strategy interface

Extend the existing `Strategy` protocol with context, and keep it pure (no
I/O inside `signal`):

```python
@dataclass(frozen=True)
class Context:
    now: float
    market: Market
    book: Orderbook
    event: Event                 # start time, status, lineup_confirmed
    consensus: Consensus | None  # p_devig, n_books, age_s, dispersion
    state: GameState | None      # in-play only
    position: Position | None    # what we already hold in this event

class Strategy(Protocol):
    def evaluate(self, ctx: Context) -> Decision: ...   # Signal | Skip | Exit
```

A `Decision` carries the reason string and every number that went into it,
so the decision log needs nothing else.

### 4.3 Execution layer (new; the crypto loop mixed this into the strategy loop)

An explicit order state machine: `intended -> sent -> acked -> partially
filled -> filled | cancelled | rejected | expired`, persisted per transition
with the exchange `order_id` and our `client_order_id` (deterministic:
hash of strategy, market, side, intent timestamp, so a retry after a crash
cannot double-send). Rules:

- Every order carries `expiration_time`; nothing rests past first pitch
  unless the strategy is in-play.
- Maker first, taker fallback after a wait, as today, but the fallback
  re-checks the signal at the current ask instead of chasing.
- On startup: fetch resting orders and positions from the exchange and
  adopt them before doing anything else. The exchange is the source of
  truth; the local state is a cache.
- Stale-data guard: no new order if the book snapshot, the consensus, or
  the game state is older than a per-strategy threshold; cancel resting
  orders if the consensus moves against them by more than half the margin.

### 4.4 Risk engine (new; the crypto loop's caps were per run-state)

`RiskEngine.check(intent, portfolio, limits) -> Allowed | Refused(reason)`,
called for every order, with counters in the `limits` table:

- Daily realised loss cap with manual reset (file flag or dashboard button
  that writes an audit row). Weekly cap too.
- Per-event exposure cap counting *correlated* markets together
  (moneyline plus run line plus total on the same game is one exposure).
- Max fraction of bankroll at risk across all open positions.
- Consecutive-loss breaker with a cooling period, as today.
- No entry after the lineup-change or postponement flag until re-evaluated.
- Size is fixed-fraction (quarter-Kelly on the *calibrated* edge, capped),
  and the only parameter the learning loop may change is size, only
  downward automatically; increases need a human click.
- Kill switch: `state/STOP` file, dashboard button, and a dead-man rule
  (no heartbeat from the feeds for 90 s pulls all resting orders).

### 4.5 Portfolio and settlement

Positions derive from fills; a reconciler compares them with
`GET /portfolio/positions` every two minutes and on start, warns once,
halts on a repeated mismatch (as the crypto loop does). Settlement handling
must cover: normal final, postponement (market voided or rolled to the new
date, per the rules text), suspended games resumed next day, and
doubleheaders (two markets, same teams, same date; the ticker grammar and
`start_ts` disambiguate).

### 4.6 Backtest equals live

The crypto bot's live rules (min hold, cooloff, trend filter, confidence
floor) were never added to its backtest, so the gate judged a different
strategy than the one running. Here the backtester replays recorded rows
through the same `Strategy.evaluate`, the same `RiskEngine`, and a fill
simulator that fills a taker at the recorded ask with depth check, and a
maker only if a later trade prints through the price. Any rule that exists
live exists in the backtest by construction.

### 4.7 Learning loop

Same shape as the crypto learner: scheduled (weekly), evaluated paired on
the same games, a promotion gate with a paper period, auto-revert on drift.
The first learned component is a calibration of the consensus edge (logit
of consensus p, Kalshi mid, dispersion, time to start, market type, league)
against the closing line and the result. Adversarial review of every
promotion is done in chat before it goes live.

### 4.8 Operations

Docker compose on a small Linux VPS with services `recorder`, `odds`,
`trader`, `learn`, `dashboard`; SQLite on a volume; nightly backup of the
database; structured JSON logs; the existing dashboard extended with an
exposure-by-game table, a CLV chart, and the pause/resume/stop controls.
Time zones: store UTC everywhere and display Eastern (game times and Kalshi
close times are published in Eastern).

## 5. Data sources and their costs

| need | source | cost | notes |
| --- | --- | --- | --- |
| Kalshi books, trades, settlements | Kalshi public API | free | existing client; more markets, adaptive cadence |
| schedule, probable pitchers, lineups, live state | MLB Stats API (`statsapi.mlb.com/api/v1`) | free, unofficial | rate-limit politely; **verify** field names against a live game |
| sportsbook odds | an odds aggregator API (e.g. The Odds API) | free tier ~500 requests/month, paid ~$30-100/month for the cadence needed | H1 cannot be tested without this or equivalent; budget for it |
| sharp reference | Pinnacle via the aggregator where offered | included above | if absent, median of major US books, de-vigged |
| weather | Open-Meteo | free | guard only |

Legal note for the record: Kalshi's sports event contracts are CFTC-listed
but their status is contested by several states; Cameron should confirm his
own eligibility. Odds data must come from a source whose terms allow
automated use.

## 6. Sample size and what "viable" costs

- Fee at 50 cents is 1.75 cents per contract; typical Kalshi spread on a
  moderate market is 2 to 4 cents **verify**. A taker needs about 3 cents
  of edge to break even; a maker about 1 cent plus fill risk.
- With CLV as the statistic (standard deviation roughly 4 to 6 cents on
  pre-game moneylines), detecting a 2-cent edge at 95% confidence needs on
  the order of 100 to 200 bets. Detecting the same edge in realised P&L
  needs about 2,400 contracts. This is why H1 gates on CLV first.
- Roughly 15 games a day, 3 to 5 market types each, of which perhaps a
  quarter show a qualifying discrepancy: 10 to 20 signals a day in
  September, so the H1 gate of 300 signals is 3 to 6 weeks of recording.
  Adding NFL and NBA roughly doubles it.

## 7. Phases and gates

1. **Probe and record (week 1).** Verify section 3 against production;
   write `kalshi_sports/catalog.py`, the sports recorder, and the odds
   ingester; start both on the laptop or VPS. Deliverable: a `record-stats`
   showing books, trades, odds, and game states accumulating for every MLB
   market, and a written answer to each **verify** item.
2. **Core extraction (week 1-2, in parallel).** Split `kalshi_core` out of
   `kalshi_bot` with the crypto tests still green; add `execution.py`,
   `risk.py`, `portfolio.py` with unit tests against recorded fixtures.
3. **H1 and H2 tests (weeks 2-5).** `kalshi-sports clv` and
   `kalshi-sports backtest` over the database; verdicts per the gates. Paper
   trading on production quotes runs from week 2 so the paper record grows
   while the recorder does.
4. **Decision (about week 6).** If H1 or H2 passes: fund to at least $500,
   live at minimum size with the risk engine, CLV tracked daily, size
   changes only through the promotion gate. If neither passes: keep
   recording through NFL and NBA and re-run; do not lower the bar.
5. **Phase 2 (2027 season or when H1 is live and profitable).** In-play
   model with the lag diagnostic; market making only after that.

## 8. Suggestions beyond the plan

- **Track CLV from day one, even by hand.** It is the single fastest
  signal of whether any bet has edge, and it works before settlement.
- **Treat postponements as a first-class state**, not an exception: a
  September rainout in the East is routine and the void rules decide
  whether a hedge survives.
- **Make the reference price pluggable.** If the odds aggregator changes
  price or terms, H1 should survive swapping the source.
- **One config file per strategy, typed and validated** (pydantic or
  dataclass with a validator), with the live defaults committed so the
  restart command needs no flags.
- **Record everything, trade nothing, for the first month.** The recorder
  is the asset; the trader is cheap once the data says what to do.
- **Keep the crypto bot's typed `TRADE` confirmation and the three gates.**
  Add a fourth for sports: a strategy may not go live without a
  `verdicts/<strategy>.json` file that the gate command wrote with VIABLE.

## 9. Open questions for Cameron

1. Budget for an odds feed (roughly $30 to $100 a month) versus a
   scrape-free alternative that would leave H1 untestable.
2. Whether to include NFL and NBA in the recorder from the start (recommended).
3. VPS now, or laptop until the first verdict.
4. Whether the crypto live loop keeps running meanwhile; its record of
   -47 on a $50 cap argues for paper mode only until its own gate passes.
