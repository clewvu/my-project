# Sports strategy: evidence, connections, and a self-learning pattern seeker

Status: design, 2026-09-07. This is the strategy layer that sits on top of
`docs/sports-design.md`. It states what the evidence says about where edge
exists in sports markets, what data connections that edge requires, and how
the system learns and hunts for patterns without fooling itself. Every
claim about Kalshi specifically is a hypothesis until the recorder says
otherwise.

## 1. What the evidence says

The sports betting literature and practitioner experience agree on a small
number of durable facts. They are the foundation; the strategy is built on
them rather than on a new idea.

1. **The sharp closing line is the best public forecast.** Odds at the
   sharpest books (Pinnacle is the reference) just before the start are
   very close to unbiased probabilities after the margin is removed.
   Nobody beats them consistently in the long run on public information.
   Consequence: the consensus of sharp books is our fair-value model, and
   our own models are judged by whether they add anything to it, not by
   whether they predict outcomes on their own.
2. **Closing line value predicts profit.** Bettors who consistently get a
   better price than the close win over time; bettors who do not, lose,
   regardless of short-run results. CLV has far lower variance than P&L,
   so it is the fastest honest measure of edge. It is the primary metric
   of every strategy here.
3. **Recreational flow is biased in known directions.** Casual money
   overweights favourites of popular teams, home teams, overs, and
   longshot parlays; the favourite-longshot bias (longshots overpriced) is
   the most replicated finding, though it is weak or reversed in baseball
   moneylines and stronger in football and basketball. Books shade their
   lines toward this flow. A retail-dominated venue such as Kalshi should
   show these biases *unshaded*, which is where a comparative edge would
   come from. Hypothesis, to be measured per league and market type.
4. **Prices move on information with a lag that depends on the venue.**
   Lineup announcements (MLB about three hours before first pitch),
   injury reports (NFL Wednesday to Friday, final inactives 90 minutes
   before), weather (wind at Wrigley, rain delays) and sharp money all move
   the sharp books within seconds to minutes. Whether Kalshi follows in
   seconds or in hours is the single most valuable thing the recorder will
   tell us: the lag is the edge.
5. **In-play markets are a latency race.** Whoever sees the play first
   wins; public feeds run 5 to 20 seconds behind. Retail in-play trading
   against faster participants loses by construction unless the venue is
   so slow that a 15-second-old truth still beats its price. Measure the
   lag before any in-play money.
6. **Edges decay.** Every documented inefficiency shrank once known.
   Kalshi sports volume is growing and attracting arbitrageurs; anything we
   find has a half-life. The system must keep measuring, keep re-fitting,
   and shrink size automatically when CLV fades.
7. **Fees and spread are the hurdle.** Kalshi's taker fee at 50 cents is
   1.75 cents a contract, spreads on thin markets are several cents, and
   a sportsbook's margin is 2 to 5 percent. An edge has to clear roughly
   three cents to be worth taking as a taker, about one cent as a maker.
   Most apparent gaps will not.

## 2. The strategy, in layers

Each layer is a separate, testable claim with its own gate. Lower layers
run first; higher layers only if the lower ones show CLV.

### Layer A: consensus gap (pre-game)

Buy the side Kalshi underprices relative to the sharp de-vigged consensus,
when the gap exceeds fee plus spread plus a fitted margin, the books agree
with each other (low dispersion), the quotes are fresh, and the game starts
soon enough that the reference is meaningful but not so soon that lineups
are unknown. Maker first with expiry at start, taker fallback when the gap
is wide. Measured by CLV against both the Kalshi close and the consensus
close, then by settled P&L. This is H1 in the design document.

### Layer B: bias correction on top of consensus

The consensus is nearly unbiased, but "nearly" is where the residual lives.
A calibration model learns, per league and market type, how the realised
outcome differs from the consensus as a function of price bucket,
favourite/underdog, home/away, public-team flag, day and time, and the
Kalshi-versus-consensus gap itself. If Kalshi is systematically wrong in a
direction (say, NCAAF home favourites overbought on Saturday afternoons),
this layer finds it and sizes Layer A accordingly. Judged by whether its
Brier score beats the plain consensus out of sample; if it does not, it is
switched off and Layer A runs on the raw consensus.

### Layer C: information-lag trading

When a sharp book moves (line movement above a threshold in a short
window), or when a scheduled information event occurs (lineup posted,
inactives released), compare Kalshi's reaction time. If Kalshi lags by
minutes, trade the direction of the sharp move before Kalshi catches up,
exit when it does or at start. This is the highest-value hypothesis if the
lag is real, and it needs the odds feed at a cadence of one to five minutes
near game time, which the free tier cannot provide.

### Layer D: internal consistency

Moneyline, spread, total and series markets on the same game imply each
other through a simple scoring model (Poisson-ish for baseball runs,
normal for football and basketball margins). Locked-in combinations after
fees are rare but riskless; single-leg mispricings relative to the others
are a second, hedgeable signal. H2 in the design.

### Layer E: in-play (later)

Win probability from game state (inning, outs, bases, score, batter and
pitcher quality; drive state and clock in football; score, time and
possession in basketball) against Kalshi's in-play price, with the lag
diagnostic as the first gate. Only after Layers A to C have a live record.

### Layer F: market making (later still)

Quote both sides around the Layer A/B fair value on thin markets, pull
quotes on any Layer C trigger, hold inventory within the risk engine's
correlated-exposure limit.

## 3. Connections that are necessary

The strategy is only as good as its data. This is the full list, with what
each connection buys and what it costs; the first four exist in the code.

| connection | what it is for | status |
| --- | --- | --- |
| Kalshi REST, public | markets, events, books, trades, settlements | done (`kalshi_bot.client`) |
| Kalshi REST, signed | balance, positions, orders, fills, shard transfers | done, unused by sports until phase 4 |
| ESPN scoreboard | schedule, start times, live state, scores, postponements | done (`feeds/scores.py`), unverified field names |
| The Odds API | sharp and soft book prices, h2h/spreads/totals, all five leagues | done (`feeds/odds.py`), needs a key; free tier too slow for Layer C |
| Kalshi WebSocket | real-time book and trade updates instead of polling several hundred markets | needed for Layers C, E, F; endpoint to verify (`wss://api.elections.kalshi.com/trade-api/ws/v2`) |
| MLB Stats API (`statsapi.mlb.com`) | probable pitchers, confirmed lineups, weather at the park, pitch-level live feed | needed for Layer C timing on MLB and Layer E; free, unofficial |
| Sharp book direct (Pinnacle) | the reference price at second-level cadence | Layer C at full strength; API access is for customers, so via the aggregator for now |
| Injury and inactive reports | NFL, NBA official reports; ESPN injuries endpoint | Layer C triggers; free |
| Weather (Open-Meteo) | wind and rain at outdoor parks; totals in MLB and NFL | Layer B feature and a Layer C trigger for rain delays; free |
| Team metadata | rosters, park factors, rest days, travel | Layer B features; static or nightly |
| A Linux VPS | the recorder and later the trader running unattended | the crypto compose stack, extended with `sports-recorder` |

Ordering: Kalshi REST, ESPN and the odds feed are enough for Layers A, B
and D and for the lag *measurement* that decides whether Layer C is worth
the faster feeds. Buy the faster feeds only when the measurement says so.

## 4. The self-learning pattern seeker

"Self-learning" here means a system that keeps measuring, re-fits on a
schedule, promotes changes only through a gate, and shrinks or stops on
its own when results fade. "Pattern seeker" means it searches
systematically for conditional edges in the recorded data. The discipline
around the search matters more than the search itself; unconstrained
pattern mining on betting data finds hundreds of false edges.

### 4.1 Feature store

One row per market per snapshot (and per decision), written by the
recorder and the trading loop into the same tables, so that research and
live see identical inputs:

- Kalshi: bid, ask, mid, spread, depth each side, last, volume, open
  interest, recent taker imbalance (from trades), minutes since last trade,
  change in mid over 5, 30, 120 minutes.
- Consensus: de-vigged probability by each of the three methods, sharp-only
  and soft-only versions, dispersion, number of books, age, change over
  the same windows (the line-movement features), the Kalshi gap.
- Context: league, market type, line, time to start, day of week, hour,
  home/away, favourite/underdog, price bucket, public-team flag (top
  markets by attendance or TV share), doubleheader, rest days, probable
  starters and their season stats, weather, postponement risk.
- In-play: state, period, clock, score, situation, seconds since the last
  state change and since the last Kalshi mid change.
- Labels, filled in later: Kalshi mid at start (the Kalshi close),
  consensus at start (the sharp close), result, settlement value, and for
  each hypothetical entry the realised CLV and net P&L.

### 4.2 Models

Small, interpretable, and always compared to the consensus baseline:

1. **Calibration model** (Layer B): logistic regression of the result on
   logit(consensus p) plus context features, L1-regularised, fit per
   league. Promoted only if out-of-sample Brier beats raw consensus with a
   paired bootstrap CI excluding zero.
2. **Lag model** (Layer C): for each sharp move, the distribution of
   Kalshi's catch-up time and fraction; a survival curve per league and
   time-to-start bucket. Read directly as the go/no-go for Layer C.
3. **Fill model**: probability a resting maker order fills, from spread,
   depth and time to start; feeds the backtester and sizing.
4. **Edge decay model**: CLV of each promoted rule over time, with a CUSUM
   drift detector. When the cumulative deviation crosses the threshold,
   size on that rule halves; twice, it is paused for re-validation.

Gradient boosting or larger models come only if the linear ones show a
residual worth chasing; on a few thousand games a season they usually
overfit.

### 4.3 The pattern search, with its guards

Weekly, the seeker scans the feature store for conditional edges of the
form "when condition C holds, buying side S at Kalshi's ask has mean CLV
greater than x". Conditions are drawn from a fixed grammar (one or two
features from the context list, thresholds on a coarse grid) so the search
space is enumerable and its size is known.

Guards, all mandatory:

- **Pre-registration by construction.** The grammar and the grid are fixed
  before the data is seen; every candidate the search evaluates is logged
  with its hash, including the ones that fail. Nothing found by hand
  outside the grammar can be promoted without going through the same
  procedure on fresh data.
- **Train, validate, test in time order.** Discovery on the oldest 60% of
  games, selection on the next 20%, the final test on the newest 20%.
  Never shuffled: sports data has season structure and edge decay.
- **False discovery control.** With hundreds of candidates, some will look
  good by chance. The Benjamini-Hochberg procedure at a 10% false
  discovery rate, on the validation window, decides which candidates go
  to the test window. Minimum sample per candidate: 150 signals, 40
  distinct game days.
- **Effect size, not just significance.** A candidate needs mean CLV of
  at least one cent after fees and a realised net that is not
  significantly negative. Small significant edges are not tradeable.
- **Paper period before money.** A promoted rule paper-trades on live
  quotes for at least three weeks or 100 signals; its live CLV must match
  its backtest within the CI.
- **Adversarial review.** Every promotion is written up (rule, sample,
  CLV, P&L, the confound check) and reviewed in chat before going live.
  The question asked each time is "what else could produce this pattern",
  with lineup timing, postponements and data errors as the usual suspects.
- **Decay watch and automatic demotion.** Per 4.2 item 4.

### 4.4 Information as input, not as signal

News, lineups, injuries and weather enter the system in two ways, neither
of them as a direct "buy on news" rule:

- As **triggers** for Layer C: the event timestamp is what we compare
  against the sharp move and Kalshi's reaction.
- As **features** for Layer B and as **stale-data guards** everywhere: a
  lineup change or a postponement flag cancels resting orders and blocks
  new entries until the consensus has updated.

Text sources (news, social) are deliberately excluded until a structured
lag is demonstrated; the books price public text within seconds, so its
only value to us is as a timestamp.

### 4.5 Sizing that learns

Quarter-Kelly on the calibrated edge, per the crypto bot's sizing module,
with three learned inputs: the calibration model's probability, the fill
model's fill probability (a maker order that does not fill has no edge),
and the decay model's health factor for the rule (1.0 healthy, 0.5 after
one drift alarm, 0 paused). Bankroll fraction at risk across correlated
positions is capped by the risk engine regardless.

## 5. What "expert level" looks like in numbers

Set expectations before the data comes in, so the verdict is read against
them rather than against hope.

- A well-run sharp bettor achieves 2 to 4 percent return on turnover at
  the close and 1 to 3 cents of CLV per dollar. Against a retail venue
  with a lag, the first months could be better; that is the honeymoon,
  not the steady state.
- At ten to twenty Layer A signals a day across five leagues, $10 a
  contract and a two-cent edge, expected profit is a few dollars a day
  before the size gate lifts. The point of the first season is a proven
  edge and a validated pipeline, not income.
- The bankroll needed for sizing to be meaningful is $500 to $1,000; the
  first six weeks are recording and paper only regardless.
- If, after the first full evaluation, no layer shows CLV with a CI
  excluding zero, the correct conclusion is that this venue is already
  efficient to the depth we can see, and the recorder keeps running while
  nothing trades. That outcome is written down here as acceptable.

## 6. Build order from here

1. Confirm the Kalshi grammar and the feeds with the phase-1 checklist.
2. Feature store and CLV report (phase 3 in the design), reading the
   recorder's tables; the `compare` report becomes a time series.
3. Lag measurement: for every sharp move above 2 cents, Kalshi's reaction
   curve. This decides whether the paid odds cadence and the WebSocket are
   worth building.
4. Layer A backtest with the fill model; Layer B calibration; the gate.
5. Pattern seeker on the accumulated store, weekly, with the guards.
6. Execution, risk engine and paper trading; then the decision.
