# Research brief: Kalshi 15-minute crypto markets

Revised from the original "DOGE whale-follow" brief. Changes are marked **[changed]**
with the reason. This is the pre-registered plan the analysis follows; anything
not listed under "primary test" is exploratory and reported as such.

## 0. Ground truth already established

From the recorder running against production:

- A market is open for exactly its 15-minute window. The reference price is
  `floor_strike`, the average of the CF Benchmarks real-time index over the
  last minute before the window opened. It settles YES if the same average over
  the last minute before close is at least the strike.
- Prices use tenth-of-a-cent ticks below $0.10 and above $0.90, whole cents
  between. Counts are fractional.
- Books are deep and tight: roughly 50,000 contracts within ten levels on each
  side and a 1-cent spread on BTC mid-window.
- Activity: BTC prints roughly 20 trades per second in busy minutes; DOGE about
  a tenth of that.

**[changed] Series.** Run every test on both `KXBTC15M` and `KXDOGE15M`,
pooled with a series term, and report them separately too. DOGE alone will not
reach statistical significance for weeks; BTC gives ten times the sample from
the same recorder at no extra cost.

## 1. Recorder audit

Captured now (SQLite, schema v2):

| table | cadence | contents |
| --- | --- | --- |
| markets | on sight, on settlement | ticker, strike, open/close, status, result, raw JSON |
| snapshots | every 5 s per open market | best bid/ask, sizes, last, volume, open interest, top 10 levels each side, raw book |
| trades | every 5 s, cursor-paginated | trade id, exchange timestamp (microseconds), yes/no price, count, taker side, raw |
| spot | every 5 s | Coinbase BTC-USD and DOGE-USD spot via REST |

Timestamps: snapshot and spot rows carry the local clock; trades carry the
exchange clock. Clock skew is measurable from `updated_time` on market rows
against local time and should be reported in the audit.

Gaps, in priority order:

1. **[fixed]** Trade capture was truncated at 100 prints per 5-second poll,
   which BTC exceeds in busy minutes. Pagination now follows the cursor.
2. **Spot is 5-second REST**, not sub-second. Adequate for the whale test and a
   coarse fair-value backtest; inadequate for a lag-exploiting strategy. Add a
   WebSocket spot recorder (Coinbase, free) writing to the same `spot` table
   with a `source` of `coinbase_ws`. Start it early so history accumulates.
3. **Settlement price.** We record the result but not the settlement index
   value. Add `expiration_value` from the settled market row. It turns the
   binary result into a continuous target and lets us measure the basis
   between Coinbase spot and the CF Benchmarks index near the strike.
4. Book snapshots are polled, so intra-5-second book changes are unseen. Fine
   for now; a WebSocket orderbook feed is only worth it if fair-value trading
   proves out.

Storage verdict: SQLite in WAL mode is adequate. Expected volume is roughly
35,000 snapshots and under a million trades per day across both series, which
SQLite handles for months with the indexes already in place. No TimescaleDB.
Keep a daily file copy as backup.

## 2. Whale-follow hypothesis

**[changed] Definition.** A whale print is a trade whose notional
(count x price) exceeds a threshold. Report at $1,000 as specified, and also at
$250 and $5,000 to see whether the effect is monotonic in size. Also express
size relative to the rolling median print in that market, because $1,000 at 5
cents is 20,000 contracts while $1,000 at 95 cents is about 1,050, and those are
very different events.

**[changed] Aggressor.** Every Kalshi print has exactly one taker. `taker_side`
gives the aggressor's outcome side and `taker_book_side` whether they lifted
the ask or hit the bid. "Passive whale" therefore means the maker side of a
large print, which we cannot observe as an order but can infer as the
counterparty. Report both interpretations; expect the taker side to be the
informative one.

**[changed] Null baseline.** Random entries at the same times and sizes are
the wrong null. The market price at the moment of the print already encodes
most of what the whale knows, especially late in the window when spot has
decided the outcome. The primary comparison is against the implied
probability at the time of the print:

    excess = mean(win) - mean(implied probability of the whale's side)

with a bootstrap confidence interval **clustered by market**, because every
print in a window shares one settlement and treating prints as independent
produces confidence intervals that are far too narrow. Second null: does the
whale's side add anything after conditioning on sign(spot - strike) at that
moment? If not, whales are just bots trading spot, and we can trade spot
directly without paying to follow them.

**Copy P&L.** Taker: fill at the ask on the whale's side from the next
snapshot after the print, exact Kalshi fee with per-order round-up. Maker:
rest one tick inside the spread and count the fill only if `last_price` later
trades through that level before close; otherwise no trade. Report gross and
net for both.

**Splits (exploratory).** Time to expiry in buckets (over 10 min, 5 to 10, 2
to 5, 1 to 2, under 1), aggressor side, series, and threshold. Any split that
looks good is a hypothesis for the next data batch, not a result.

**Validation.** Time-ordered split: fit any thresholds on the first 70% of
windows, report on the last 30%. Walk-forward with weekly folds once there are
four or more weeks. Data-sufficiency gate: at least 200 qualifying prints in
the test set before a verdict is issued; below that the verdict is
"inconclusive" by construction.

**Viability threshold (pre-registered).** Viable only if the out-of-sample net
P&L per contract at the taker fill is at least +1 cent with a 95% clustered
confidence interval that excludes zero, on the pooled sample. Anything else
is not viable or inconclusive.

## 3. Fair-value alternative

Settlement is a one-minute average of an index, so fair value is the
probability that the average over the final minute is at or above the strike.
A first model:

    z = ln(S / K) / (sigma * sqrt(tau))
    p_up = Phi(z)

with S the current spot, K the strike, tau the time to the middle of the
settlement minute, and sigma the realised volatility over the last 30 to 60
minutes. Averaging over the last minute slightly reduces variance; the
adjustment is small and can be added later.

Trade rule: buy the side whose fair value exceeds the Kalshi ask by more than
the round-trip fee plus a margin; otherwise do nothing. Most windows will
produce no trade, which is the correct outcome.

**[changed] Drop perp funding and basis.** Funding is a multi-hour signal with
no information on a 15-minute horizon. It adds a dependency and no edge.

Effort: Coinbase WebSocket ingest, 1 to 2 days. Volatility estimator and fair
value, 1 day. Backtest against recorded snapshots, 2 to 3 days. Live loop on
demo, 2 days. The existing 5-second history is enough to test whether the
fair-value gap predicts settlement at all; it is not enough to size the
sub-second lag, which needs the WebSocket data.

The known risk: the Coinbase price and the CF Benchmarks index differ by a
basis that is small but not zero. Near the strike that basis is the whole
trade. The `expiration_value` field will let us measure it.

## 4. Risk framework

Implemented as a single `RiskEngine.check(order, state) -> Allow | Reject`
call sitting between the strategy and the client, with state persisted in
SQLite so a restart cannot reset a limit. All are hard cutoffs.

| control | rule | where |
| --- | --- | --- |
| daily loss cap | realised plus marked P&L since 00:00 UTC below -X: halt, set `halted` flag, require manual reset | before every order |
| per-market cap | max contracts per market, one position per window | before every order |
| bankroll at risk | sum of cost of open positions and resting orders at most Y% of balance | before every order |
| consecutive losses | N settled losses in a row: halt for M windows | on settlement |
| no-entry window | no new orders when seconds to close is under 120 | before every order |
| stale data | no orders if the newest spot or snapshot is older than 10 s; cancel resting orders if older than 30 s | every tick |
| kill switch | a file flag and a Telegram command that set the same flag; checked every tick; cancels all resting orders | every tick |
| decision log | one row per decision, filled or not, with strategy inputs, fair value, book state, and risk verdict as JSON | every decision |

**[added]** Reconciliation: on start and every few minutes, compare local
positions and resting orders with the exchange and halt on mismatch. Order
lifecycle: every resting order gets an expiry before the no-entry window and
is cancelled if unfilled. Sizing: fixed fraction of bankroll per window, capped
in contracts, never Kelly at full strength.

## 5. Operations

- Run on a small Linux VPS under systemd, or Docker; the current Windows
  laptop is fine for recording but not for a 24/7 trader.
- Telegram bot for alerts and the kill switch. Simpler than Slack for one
  operator and works from a phone.
- Heartbeat every five minutes; a dead-man alert if two are missed. P&L
  summary at each settlement and daily.
- Paper mode is the default. Live requires the explicit `allow_live` flag, the
  production environment, and a passing demo run.
- Dashboard: a minimal page reading the decision log and P&L from SQLite.
  Grafana only if the page stops being enough.

## 6. Tools

Agreed: no news or search tools. Additions limited to exchange WebSockets, the
existing SQLite store, Telegram, and the backtester with the clustered null.

## 7. Priorities

1. Keep recording. Add `expiration_value` capture and the WebSocket spot
   recorder now so the data exists when needed.
2. Whale test once the sufficiency gate is met, BTC and DOGE pooled.
3. Fair-value backtest on the same data, same nulls, same gate.
4. Only if one of them passes the pre-registered threshold: risk engine,
   demo run, then live at minimum size.
5. If neither passes: stop. The market is efficiently priced at the resolution
   we can observe. Look elsewhere rather than lower the bar.

## Bankroll reality check

Growing $200 to $1,000 needs a 5x return. At a net edge of 2 cents per
contract, 20 contracts per window, and one trade in every window on both
series, that is about $0.40 per traded window and, trading every window,
roughly $38 per day before variance. The edge, if it exists, will be sparse:
most windows will not qualify. A realistic path is weeks to months, and
only if a pre-registered test passes out of sample.

## 8. Cross-asset hypotheses (added 2026-11-09, pre-registered)

Cameron's observation: gold and silver, and BTC, ETH and SOL, usually settle
the same way; oil may move against the metals. The universe
(`kalshi_bot/universe.py`) declares these as directed hypotheses with a sign,
and `kalshi-bot crossasset` tests them.

**What co-movement does and does not prove.** If ETH and BTC finish the same
window up together, that is contemporaneous correlation. BTC's own spot and
BTC's own market price already carry that information. A cross-asset signal is
only useful if the leader tells us something about the follower's settlement
*that the follower's price does not yet reflect*: the follower's book lags the
leader's move, or the leader's market is better informed than the follower's.
Everything below is built to separate those two cases.

**Tests, in order.**

1. Outcome co-movement: pairwise agreement rate and phi across common
   windows, bootstrapped over windows. For each declared hypothesis, report
   whether the realised sign matches the claim ("Brent vs gold" is a claim
   of negative phi; the literature often finds the opposite, so the data
   decides).
2. Spot lead-lag: cross-correlation of 10-second log returns on a 5-second
   grid at lags from -60 s to +60 s. Peak at lag 0 means simultaneous and
   nothing to trade. Peak at a positive lag with higher correlation than at
   lag 0 means the leader leads.
3. Predictor test at T-300 s and T-60 s: logistic regression of the
   follower's outcome on logit(follower implied) plus one leader feature
   (leader's implied, leader's return since window open, leader's return over
   the last 60 s), sign-adjusted. Bootstrap CI on the coefficient over
   windows. "Informative" only if the CI excludes zero.
4. Divergence backtest: buy the follower side the leader's market favours
   when the two markets disagree by at least 10, 20 or 30 points, at the
   follower's ask, fee-inclusive, hold to settlement.

**Pre-registered bar.** A hypothesis is tradeable only if (3) is informative
at a horizon where the follower's book is still liquid, AND (4) shows net per
contract >= +1 cent with a 95% CI excluding zero, both in a time-ordered
held-out 30% of windows, with at least 200 windows in the held-out set.
Co-movement that passes (1) but fails (3) is "confirmed but already priced".

**Known traps.** Commodity series trade on a weekday schedule, so overlap
with 24/7 crypto is partial; pair statistics only use common windows. Pyth is
the settlement source for metals and oil, so for those series the spot series
is exact, while for crypto Coinbase remains a proxy for CF Benchmarks.
Multiple hypotheses are tested at once; with nine hypotheses and two horizons,
roughly one spurious "informative" result at the 95% level is expected by
chance, so a single pass means nothing until it repeats on the next batch.
