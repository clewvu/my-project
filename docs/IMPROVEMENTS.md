# Improvements — robustness & profitability

Prioritized from a review of both desks and the shared infrastructure (2026-09-07).
Ranked by severity × likelihood × effort. File:line anchors are pointers to verify,
not gospel — confirm before editing.

Legend: **P0** stop-the-bleeding · **P1** important · **P2** worthwhile · **P3** later.

---

## Robustness

### P0 — The crypto loop crashes on any API error
`kalshi_bot/demo_loop.py` main loop (~484-499) calls `tick()` with **no exception
guard**. A single transient `KalshiAPIError`, network blip, or malformed response
takes the whole loop down — it stops trading *and* stops watching open positions,
silently, until someone notices the window. The sports loop already does the right
thing: `kalshi_sports/trader.py:622-642` wraps `tick()` in `except Exception`, logs,
and continues.
**Fix:** wrap the crypto tick in try/except; log to alerts; back off briefly; continue.
Halt only on repeated consecutive failures (e.g. 5 in a row), not the first.
*Being done in this pass.*

### P0 — No process supervision; a dead loop is invisible
Nothing restarts a loop that exits, and the dashboard doesn't show the recorders or
the learner at all. Tonight's `--clear-halt` crash is the case in point — it looked
"launched" but wasn't running.
**Fix (two parts):** (1) dashboard process-health rail so a dead process is obvious
*(being done in this pass)*; (2) a supervisor — `deploy/docker-compose.yml` has
`restart: unless-stopped`, but on the laptop use a tiny PowerShell wrapper that
relaunches on exit, or NSSM/Task Scheduler.

### P1 — State is saved *after* the order is placed (orphan-order window)
Both loops persist state after `create_order()` returns
(`demo_loop.py:674` / `trader.py:519` regions). A crash *between* placing the order
and writing state leaves a real position on the exchange that the loop doesn't know
about on restart.
**Fix:** write an "in-flight" marker (series, side, intended size) to state *before*
submitting, and clear it once the fill is recorded. On startup, resolve in-flight
markers against the exchange before doing anything else.

### P1 — Fund preflight can drain the wrong shard
The handoff says the preflight "funds shard 2 only from shard 0." The code
(`cli.py` `shard_plan()`, ~674-697) actually picks `max(others, key=balance)` — the
shard with the **most money**, whichever it is. If shard 3 (MLB) ever holds the most,
crypto funding would pull from it. It also has no cross-process lock, so two
simultaneous transfers can race.
**Fix:** make the source explicit (shard 0 only, configurable), and serialize
transfers with a lock file so the two desks can't move funds at the same instant.

### P1 — Rate limiting is per-process, not per-account
`client.py` throttles each client instance to `min_request_interval=0.15s`
independently. Two loops on one key ⇒ ~13 req/s combined, which can breach Kalshi's
account limit; 429 backoff is also independent (both back off, neither coordinates).
**Fix:** a shared token-bucket via a lock file / small local socket so the *account*
stays under one budget. Cheap insurance: raise each loop's interval to ~0.25s while
both run.

### P2 — Sports reconciles only at startup, and silently adopts positions
`trader.py:184-232` reconciles once at boot and **auto-inserts** any exchange
position it finds into its own DB. Crypto reconciles every ~120s and halts on a
persistent mismatch. The asymmetry means a stray/misattributed position could be
absorbed by sports without anyone noticing.
**Fix:** reconcile sports on a cadence too; on an unexpected position, alert instead
of silently adopting; assert series/league ownership before adopting.

### P2 — Settlement-after-crash can strand a position "open" forever
If a market settles while a loop is down, restart reconciliation (which filters to
*unsettled*) won't see it, so it lingers open in state/DB.
**Fix:** on startup, also fetch recently-settled positions and reconcile booked P&L.

### P3 — Key handling & single points of failure
One private key, plaintext on disk, no rotation, shared by both desks. Acceptable for
a solo laptop bot; revisit if it ever runs on a shared/remote host (encrypt at rest,
restrict file ACLs, plan rotation).

---

## Profitability

### P1 — Margin gate is an absolute dollar amount, not a rate
Crypto requires `edge ≥ $0.03` regardless of price (`strategy.py` ~641). That's a 5%
hurdle on a 60¢ contract but a 30% hurdle on a 10¢ contract — an inconsistent bar
that suppresses legitimate cheap-contract edges.
**Fix:** normalize to a percentage of price (e.g. edge/ask ≥ X%), or blend an
absolute floor with a rate.

### P1 — Slippage isn't modeled anywhere
Both desks compute edge assuming a fill at the quoted ask. Backtest uses a fixed
1-snapshot lag; live has no slippage estimate and no volatility adjustment. On a
2-4¢ edge, even 0.5-1.5¢ of slippage is a large fraction of the profit.
**Fix:** log realised fill vs. quoted ask per trade; feed a rolling slippage estimate
back into the required margin (margin += k × recent_slippage).

### P1 — Fee rounding not reflected in sizing (bias on marginal trades)
Edge uses the smooth `0.07·p·(1-p)` fee; the exchange rounds the *order* fee up to the
cent. On small orders that's a consistent 10-20% underestimate of fees — precisely on
the marginal trades near the margin threshold.
**Fix:** compute the *rounded per-order* fee in the edge check for the actual order
size, not the smooth per-contract value.

### P2 — No correlation between BTC and DOGE positions
Positions are sized independently, but BTC/DOGE are highly correlated. Two same-side
positions in one window is ~2× the intended risk in a shared move.
**Fix:** treat the two series as one exposure for sizing; scale down when both are
open on the same side.

### P2 — Confidence-scaled sizing sits idle for the first 20 results per tier
High-confidence signals stake only the base amount until a tier has 20 settled
results (`sizing.py:34`). Early edge is real but unstaked.
**Fix:** allow a partial confidence bump before 20 results using a shrinkage/CI-width
prior rather than a hard gate.

### P2 — Exits are biased to hold-to-settlement
`stop_value=0.10` forces holding a loser until the model values it ≤~3¢; there's no
early exit on a winner past its peak, and `min_hold=60s` locks you in during the first
(most volatile) minute.
**Fix:** add a peak/reversion exit for winners and a time-decay-aware exit near close;
revisit stop_value.

### P2 — Sports: Layer C (trade the sharp-book lag) is the actual edge, still off
The consensus-gap thesis depends on Kalshi lagging sharp books, but the loop doesn't
yet act on detected sharp moves, and it's taker-only (maker fallback unimplemented),
so it pays the spread it's trying to capture. Odds also come from the recorder's table
and can be stale with no alert on lag.
**Fix:** implement maker-first with taker fallback; add an odds-staleness alert;
prioritize the information-lag trigger — it's where the CLV comes from.

### P3 — Learner overfitting risk (small grid, in-sample calibration)
Tiny grids and calibration fit on the training split with weak regularization, on low
data volume. It won't break anything, but promoted params may be luck.
**Fix:** walk-forward validation (train→test→roll); widen CIs; keep the promotion gate
strict (it currently is — good).

---

## Suggested order of work
1. Crash-guard crypto loop **(P0, this pass)**
2. Dashboard health rail + funding ledger + signal table **(P0/visibility, this pass)**
3. Explicit shard-0 funding + transfer lock **(P1)**
4. In-flight order marker before submit **(P1)**
5. Slippage logging → margin feedback; fee-rounding in edge **(P1)**
6. Shared rate budget across desks **(P1)**
7. Correlation-aware sizing; exit improvements **(P2)**
8. Sports maker-first + odds-staleness alert **(P2)**
