"""Recorder for Kalshi sports markets plus the odds and score feeds.

Each tick:
  1. every ``discover_interval``: list open markets for every configured series,
     classify them (league, teams, kind, line) and upsert markets and events
  2. for every live market whose cadence is due: orderbook snapshot and new trades
  3. every ``settle_interval``: re-fetch closed markets until they carry a result
  4. every ``odds_interval`` per league: sportsbook odds (if a feed is configured)
  5. every scores interval per league: ESPN scoreboard; fast while any game is in play

Cadence per market depends on time to the game (``cadence_for``): hourly-ish
far out, every 5 seconds near the start and in play. Sports markets number in
the hundreds when college football is included, so this matters for the rate
limit: at 0.15 s per request, 300 markets at 2 calls each is 90 s per sweep,
which is why far-out markets are sampled slowly.

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
from .catalog import GameMarket, classify
from .feeds.odds import OddsFeed
from .feeds.scores import ScoreFeed, today_eastern
from .storage import SportsDataStore

log = logging.getLogger(__name__)

DEAD_STATUSES = {"closed", "settled", "finalized", "determined"}


def is_live(market: Market, now: float) -> bool:
    if market.status.lower() in DEAD_STATUSES:
        return False
    if market.close_time is not None and market.close_time.timestamp() <= now:
        return False
    return True


def cadence_for(secs_to_start: float | None, *, fast: float, slow: float) -> float:
    """Seconds between snapshots given time to game start (negative = in play)."""
    if secs_to_start is None:
        return slow
    if secs_to_start <= 30 * 60:  # last half hour before start, and in play
        return fast
    if secs_to_start <= 3 * 3600:
        return 60.0
    if secs_to_start <= 24 * 3600:
        return 300.0
    return slow


@dataclass
class TickResult:
    discovered: int = 0
    markets: int = 0
    snapshots: int = 0
    new_trades: int = 0
    settled: int = 0
    odds: int = 0
    states: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class Tracked:
    market: Market
    gm: GameMarket
    start_ts: float | None
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
        slow_cadence: float = 900.0,
        discover_interval: float = 600.0,
        settle_interval: float = 300.0,
        book_depth: int = 10,
        odds: OddsFeed | None = None,
        odds_interval: float = 1800.0,
        scores: ScoreFeed | None = None,
        scores_interval_live: float = 20.0,
        scores_interval_idle: float = 300.0,
    ) -> None:
        self.client = client
        self.store = store
        self.series = list(series)
        self.leagues = [leagues.LEAGUES[k] for k in league_keys]
        self.interval = max(1.0, interval)
        self.fast_cadence = fast_cadence
        self.slow_cadence = slow_cadence
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
        for series in self.series:
            try:
                markets = self.client.get_markets(series_ticker=series, status="open", max_pages=5)
            except KalshiError as exc:
                result.errors.append(f"{series}: list markets: {exc}")
                log.warning("%s: list markets failed: %s", series, exc)
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
                    log.info("%s: no open markets (series may not exist; check `discover`)", series)
                    self._empty_series.add(series)
                continue
            self._empty_series.discard(series)
            self._load_events(series, now, result)
            for m in markets:
                if not is_live(m, now):
                    continue
                event = self.events.get(m.event_ticker or "")
                gm = classify(m, event)
                self.store.upsert_event(gm, now, event)
                self.store.upsert_market(m, gm, now)
                start = gm.start_ts or self.store.event_start(m.event_ticker)
                tr = self.tracked.get(m.ticker)
                if tr is None:
                    self.tracked[m.ticker] = Tracked(m, gm, start)
                    result.discovered += 1
                else:
                    tr.market, tr.gm, tr.start_ts = m, gm, start
        # forget markets that vanished from the open lists
        seen = {m for m in self.tracked if self.tracked[m].market is not None}
        for ticker in list(seen):
            if not is_live(self.tracked[ticker].market, now):
                del self.tracked[ticker]

    def _load_events(self, series: str, now: float, result: TickResult) -> None:
        try:
            events = self.client.get_events(series_ticker=series, status="open", max_pages=5)
        except KalshiError as exc:
            result.errors.append(f"{series}: list events: {exc}")
            log.debug("%s: list events failed: %s", series, exc)
            return
        for ev in events:
            ticker = ev.get("event_ticker") or ev.get("ticker")
            if ticker:
                self.events[str(ticker)] = ev

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
            if now < tr.next_snapshot:
                continue
            result.markets += 1
            self._record_market(tr, now, result)
            secs = (tr.start_ts - now) if tr.start_ts else None
            tr.next_snapshot = now + cadence_for(
                secs, fast=self.fast_cadence, slow=self.slow_cadence
            )

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
                    result.states += self._record_scores(lg, now, result)

        return result

    def _record_market(self, tr: Tracked, now: float, result: TickResult) -> None:
        market = tr.market
        try:
            fresh = self.client.get_market(market.ticker)
            market = tr.market = fresh
        except KalshiError as exc:
            result.errors.append(f"{market.ticker}: market: {exc}")
            log.warning("%s: market refresh failed: %s", market.ticker, exc)
        book = None
        try:
            book = self.client.get_orderbook(market.ticker, depth=self.book_depth)
        except KalshiError as exc:
            result.errors.append(f"{market.ticker}: orderbook: {exc}")
            log.warning("%s: orderbook failed: %s", market.ticker, exc)
        self.store.upsert_market(market, tr.gm, now)
        self.store.insert_snapshot(now, market, book, tr.start_ts)
        result.snapshots += 1
        try:
            since = self.store.last_trade_ts(market.ticker)
            trades = self.client.get_trades(market.ticker, min_ts=int(since) if since else None)
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

    def _record_scores(self, lg: leagues.League, now: float, result: TickResult) -> int:
        try:
            states = self.scores.fetch(lg, date=today_eastern(now))  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"scores {lg.key}: {exc}")
            log.warning("scores %s failed: %s", lg.key, exc)
            return 0
        self._in_play[lg.key] = any(s.state == "in" for s in states)
        # Only write a row when something changed, so idle days do not bloat the table.
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
        return self.store.insert_game_states(rows)

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
                    "tick tracked=%d polled=%d snapshots=%d trades+%d settled=%d odds+%d "
                    "states+%d errors=%d",
                    len(self.tracked),
                    res.markets,
                    res.snapshots,
                    res.new_trades,
                    res.settled,
                    res.odds,
                    res.states,
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
