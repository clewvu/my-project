"""Recorder for Kalshi sports markets plus the odds and score feeds.

Request budget. The probe on 2026-09-07 found well over a thousand open
markets (NFL and college football each list hundreds of spreads and totals),
and Kalshi answered a burst with 429. At the 0.15 s throttle, three requests
per market would take longer than any sane cadence. So:

  1. Discovery (every ``discover_interval``) lists every open market per series
     with ``limit=1000``: a handful of requests. The list carries bid, ask,
     last, volume and open interest, so a *light snapshot* is written for every
     live market straight from it, with no per-market request.
  2. Only markets within ``book_window_s`` of their start (default 24 h) get
     *book snapshots* (orderbook + trades) on a cadence that tightens toward
     the start: every 5 minutes inside a day, every minute inside three hours,
     every 5 seconds in the last half hour and in play.
  3. Settlements are re-fetched every ``settle_interval`` for closed markets.
  4. Odds and ESPN game state are polled per league; ESPN start times refine
     events whose start Kalshi gave only as a date.

Any single failing call is logged and skipped; the loop keeps going.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from kalshi_bot.client import KalshiClient, KalshiError
from kalshi_bot.models import Market

from . import leagues
from .catalog import GameMarket, classify, date_from_ts
from .feeds.odds import OddsFeed
from .feeds.scores import GameState, ScoreFeed, today_eastern
from .matching import GameRef, match_game
from .storage import SportsDataStore

log = logging.getLogger(__name__)

DEAD_STATUSES = {"closed", "settled", "finalized", "determined"}


def is_live(market: Market, now: float) -> bool:
    if market.status.lower() in DEAD_STATUSES:
        return False
    if market.close_time is not None and market.close_time.timestamp() <= now:
        return False
    return True


def cadence_for(
    secs_to_start: float | None, *, fast: float, window: float = 24 * 3600
) -> float | None:
    """Seconds between book snapshots given time to start; None = light snapshots only."""
    if secs_to_start is None or secs_to_start > window:
        return None
    if secs_to_start <= 30 * 60:  # last half hour and in play
        return fast
    if secs_to_start <= 3 * 3600:
        return 60.0
    return 300.0


@dataclass
class TickResult:
    discovered: int = 0
    light: int = 0
    markets: int = 0
    snapshots: int = 0
    new_trades: int = 0
    settled: int = 0
    odds: int = 0
    states: int = 0
    starts_refined: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class Tracked:
    market: Market
    gm: GameMarket
    start_ts: float | None
    start_exact: bool
    next_snapshot: float = 0.0


class SportsRecorder:
    def __init__(
        self,
        client: KalshiClient,
        store: SportsDataStore,
        *,
        series: list[str],
        league_keys: tuple[str, ...] | list[str] = leagues.DEFAULT_LEAGUES,
        interval: float = 5.0,
        fast_cadence: float = 5.0,
        book_window_s: float = 24 * 3600,
        discover_interval: float = 300.0,
        settle_interval: float = 300.0,
        book_depth: int = 10,
        odds: OddsFeed | None = None,
        odds_interval: float = 1800.0,
        scores: ScoreFeed | None = None,
        scores_interval_live: float = 20.0,
        scores_interval_idle: float = 300.0,
        first_backfill_pages: int = 20,
    ) -> None:
        self.client = client
        self.first_backfill_pages = first_backfill_pages
        self.store = store
        self.series = list(series)
        self.leagues = [leagues.LEAGUES[k] for k in league_keys]
        self.interval = max(1.0, interval)
        self.fast_cadence = fast_cadence
        self.book_window_s = book_window_s
        self.discover_interval = discover_interval
        self.settle_interval = settle_interval
        self.book_depth = book_depth
        self.odds = odds
        self.odds_interval = odds_interval
        self.scores = scores
        self.scores_interval_live = scores_interval_live
        self.scores_interval_idle = scores_interval_idle
        self.tracked: dict[str, Tracked] = {}
        self.events: dict[str, dict[str, Any]] = {}
        self._last_discover = 0.0
        self._last_settle = 0.0
        self._last_odds: dict[str, float] = {}
        self._last_scores: dict[str, float] = {}
        self._in_play: dict[str, bool] = {}
        self._empty_series: set[str] = set()
        self._stop = threading.Event()

    # ------------------------------------------------------------ discovery

    def discover(self, now: float, result: TickResult) -> None:
        seen: set[str] = set()
        for series in self.series:
            try:
                markets = self.client.get_markets(
                    series_ticker=series, status="open", limit=1000, max_pages=5
                )
            except KalshiError as exc:
                result.errors.append(f"{series}: list markets: {exc}")
                log.warning("%s: list markets failed: %s", series, exc)
                seen.update(
                    t for t, tr in self.tracked.items() if tr.market.series_ticker == series
                )
                continue
            lg = leagues.league_for_series(series)
            self.store.upsert_series(
                series,
                now,
                league=lg.key if lg else None,
                kind=lg.series_kind(series) if lg else None,
                title=None,
                category=None,
                open_markets=len(markets),
                raw=None,
            )
            if not markets:
                if series not in self._empty_series:
                    log.info("%s: no open markets", series)
                    self._empty_series.add(series)
                continue
            self._empty_series.discard(series)
            for m in markets:
                if not is_live(m, now):
                    continue
                seen.add(m.ticker)
                event = self.events.get(m.event_ticker or "")
                gm = classify(m, event)
                self.store.upsert_event(gm, now, event)
                self.store.upsert_market(m, gm, now)
                start, exact = gm.start_ts, gm.start_exact
                db_start, db_exact = self.store.event_start(m.event_ticker)
                if db_exact and db_start is not None:
                    start, exact = db_start, True
                tr = self.tracked.get(m.ticker)
                if tr is None:
                    self.tracked[m.ticker] = Tracked(m, gm, start, exact)
                    result.discovered += 1
                else:
                    tr.market, tr.gm, tr.start_ts, tr.start_exact = m, gm, start, exact
                # light snapshot from the list: no extra request
                self.store.insert_snapshot(now, m, None, start)
                result.light += 1
        for ticker in list(self.tracked):
            if ticker not in seen:
                del self.tracked[ticker]

    # ------------------------------------------------------------ one tick

    def tick(self, now: float | None = None) -> TickResult:
        now = time.time() if now is None else now
        result = TickResult()

        if now - self._last_discover >= self.discover_interval or not self.tracked:
            self._last_discover = now
            self.discover(now, result)

        for tr in list(self.tracked.values()):
            if not is_live(tr.market, now):
                self.tracked.pop(tr.market.ticker, None)
                continue
            secs = (tr.start_ts - now) if tr.start_ts is not None else None
            cadence = cadence_for(secs, fast=self.fast_cadence, window=self.book_window_s)
            if cadence is None or now < tr.next_snapshot:
                continue
            result.markets += 1
            self._record_market(tr, now, result)
            tr.next_snapshot = now + cadence

        if now - self._last_settle >= self.settle_interval:
            self._last_settle = now
            result.settled = self._check_settlements(now, result)

        if self.odds is not None:
            for lg in self.leagues:
                if now - self._last_odds.get(lg.key, 0.0) >= self.odds_interval:
                    self._last_odds[lg.key] = now
                    result.odds += self._record_odds(lg, result)

        if self.scores is not None:
            for lg in self.leagues:
                interval = (
                    self.scores_interval_live
                    if self._in_play.get(lg.key)
                    else self.scores_interval_idle
                )
                if now - self._last_scores.get(lg.key, 0.0) >= interval:
                    self._last_scores[lg.key] = now
                    states = self._record_scores(lg, now, result)
                    if states:
                        result.starts_refined += self._refine_starts(lg, states, now)

        return result

    def _record_market(self, tr: Tracked, now: float, result: TickResult) -> None:
        market = tr.market
        book = None
        try:
            book = self.client.get_orderbook(market.ticker, depth=self.book_depth)
        except KalshiError as exc:
            result.errors.append(f"{market.ticker}: orderbook: {exc}")
            log.warning("%s: orderbook failed: %s", market.ticker, exc)
        self.store.insert_snapshot(now, market, book, tr.start_ts)
        result.snapshots += 1
        try:
            since = self.store.last_trade_ts(market.ticker)
            # First contact with a market that has been trading for hours (an in-play game)
            # may need a deep backfill; after that each poll only asks for new prints.
            trades = self.client.get_trades(
                market.ticker,
                min_ts=int(since) if since else None,
                max_pages=self.first_backfill_pages if since is None else 5,
            )
            result.new_trades += self.store.insert_trades(market.ticker, trades)
        except KalshiError as exc:
            result.errors.append(f"{market.ticker}: trades: {exc}")
            log.warning("%s: trades failed: %s", market.ticker, exc)

    def _check_settlements(self, now: float, result: TickResult) -> int:
        settled = 0
        for ticker in self.store.pending_settlements(now):
            try:
                market = self.client.get_market(ticker)
            except KalshiError as exc:
                result.errors.append(f"{ticker}: settlement: {exc}")
                log.warning("%s: settlement fetch failed: %s", ticker, exc)
                continue
            if market.result:
                self.store.mark_settled(market, now)
                settled += 1
                log.info("%s settled %s", ticker, market.result)
            else:
                tr = self.tracked.get(ticker)
                gm = tr.gm if tr else classify(market, self.events.get(market.event_ticker or ""))
                self.store.upsert_market(market, gm, now)
        return settled

    def _record_odds(self, lg: leagues.League, result: TickResult) -> int:
        try:
            quotes = self.odds.fetch(lg)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - feed errors must not stop recording
            result.errors.append(f"odds {lg.key}: {exc}")
            log.warning("odds %s failed: %s", lg.key, exc)
            return 0
        return self.store.insert_odds([q.row() for q in quotes])

    def _record_scores(self, lg: leagues.League, now: float, result: TickResult) -> list[GameState]:
        try:
            states = self.scores.fetch(lg, date=today_eastern(now))  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"scores {lg.key}: {exc}")
            log.warning("scores %s failed: %s", lg.key, exc)
            return []
        self._in_play[lg.key] = any(s.state == "in" for s in states)
        rows = []
        for s in states:
            last = self.store.last_game_state(lg.key, s.game_id)
            if last is not None and (
                last["state"] == s.state
                and last["detail"] == s.detail
                and last["period"] == s.period
                and last["clock"] == s.clock
                and last["home_score"] == s.home_score
                and last["away_score"] == s.away_score
            ):
                continue
            rows.append(s.row())
        result.states += self.store.insert_game_states(rows)
        return states

    def _refine_starts(self, lg: leagues.League, states: list[GameState], now: float) -> int:
        """Give date-only Kalshi events an exact start from the score feed's schedule."""
        refs = [
            GameRef(lg.key, s.game_id, s.start_ts, s.home, s.away, s.home_abbr, s.away_abbr)
            for s in states
            if s.start_ts
        ]
        if not refs:
            return 0
        refined = 0
        for ev in self.store.events_needing_start(lg.key, date_from_ts(now)):
            ref = match_game(
                lg.key,
                ev["game_date"],
                ev["away_abbr"] or ev["away"],
                ev["home_abbr"] or ev["home"],
                refs,
                alt_away=ev["away"],
                alt_home=ev["home"],
            )
            if ref is None or ref.start_ts is None:
                continue
            self.store.set_event_start(ev["event_ticker"], ref.start_ts, "espn")
            for tr in self.tracked.values():
                if tr.market.event_ticker == ev["event_ticker"]:
                    tr.start_ts, tr.start_exact = ref.start_ts, True
            refined += 1
        return refined

    # ------------------------------------------------------------ loop

    def stop(self) -> None:
        self._stop.set()

    def run(self, *, max_ticks: int | None = None, install_signals: bool = True) -> int:
        if install_signals:
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: self.stop())
        log.info(
            "recording %d series for %s every %.0fs -> %s (odds=%s scores=%s)",
            len(self.series),
            ",".join(lg.key for lg in self.leagues),
            self.interval,
            self.store.path,
            "on" if self.odds else "off",
            "on" if self.scores else "off",
        )
        ticks = 0
        failures = 0
        while not self._stop.is_set():
            started = time.time()
            try:
                res = self.tick(started)
                failures = 0 if not res.errors else failures + 1
                log.info(
                    "tick tracked=%d light=%d books=%d trades+%d settled=%d odds+%d "
                    "states+%d starts+%d errors=%d",
                    len(self.tracked),
                    res.light,
                    res.snapshots,
                    res.new_trades,
                    res.settled,
                    res.odds,
                    res.states,
                    res.starts_refined,
                    len(res.errors),
                )
            except Exception:  # noqa: BLE001
                failures += 1
                log.exception("tick crashed")
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            delay = self.interval * min(12, 2 ** min(failures, 4)) if failures else self.interval
            self._stop.wait(max(0.0, delay - (time.time() - started)))
        log.info("recorder stopped after %d ticks", ticks)
        return ticks
