"""Market data recorder for Kalshi 15-minute crypto series.

Each tick, for every configured series:
  1. list open markets (public endpoint)
  2. for each: fetch the orderbook and store a snapshot
  3. fetch new public trades since the last one we stored
Every ``settle_interval`` seconds, markets whose close time has passed are
re-fetched to capture the settlement result. Optionally, spot prices are
recorded on every tick.

Any single failing call is logged and skipped; the loop keeps going.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass, field

from .client import KalshiClient, KalshiError
from .models import Market
from .spot import SpotFeed
from .storage import MarketDataStore

log = logging.getLogger(__name__)

DEFAULT_SERIES = ["KXBTC15M", "KXDOGE15M"]
DEAD_STATUSES = {"closed", "settled", "finalized", "determined"}


def is_live(market: Market, now: float) -> bool:
    """True if the market is still trading: not in a terminal status and not past close."""
    if market.status.lower() in DEAD_STATUSES:
        return False
    if market.close_time is not None and market.close_time.timestamp() <= now:
        return False
    return True


DEFAULT_SPOT_SYMBOLS = ["BTC-USD", "DOGE-USD"]


@dataclass
class TickResult:
    markets: int = 0
    snapshots: int = 0
    new_trades: int = 0
    settled: int = 0
    spot: int = 0
    errors: list[str] = field(default_factory=list)


class Recorder:
    def __init__(
        self,
        client: KalshiClient,
        store: MarketDataStore,
        *,
        series: list[str] | None = None,
        interval: float = 5.0,
        book_depth: int = 10,
        settle_interval: float = 60.0,
        spot: SpotFeed | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.series = list(series or DEFAULT_SERIES)
        self.interval = max(1.0, interval)
        self.book_depth = book_depth
        self.settle_interval = settle_interval
        self.spot = spot
        self._last_settle_check = 0.0
        self._stop = threading.Event()

    # ------------------------------------------------------------ one tick

    def tick(self, now: float | None = None) -> TickResult:
        now = time.time() if now is None else now
        result = TickResult()

        for series in self.series:
            try:
                markets = self.client.get_markets(series_ticker=series, status="open", max_pages=2)
            except KalshiError as exc:
                result.errors.append(f"{series}: list markets: {exc}")
                log.warning("%s: list markets failed: %s", series, exc)
                continue
            live = [m for m in markets if is_live(m, now)]
            if not live:
                log.debug("%s: no live markets (%d returned)", series, len(markets))
            for market in live:
                result.markets += 1
                self._record_market(market, now, result)

        if now - self._last_settle_check >= self.settle_interval:
            self._last_settle_check = now
            result.settled = self._check_settlements(now, result)

        if self.spot is not None:
            for symbol, price in self.spot.fetch().items():
                self.store.insert_spot(now, self.spot.source, symbol, price)
                result.spot += 1

        return result

    def _record_market(self, market: Market, now: float, result: TickResult) -> None:
        self.store.upsert_market(market, now)
        book = None
        try:
            book = self.client.get_orderbook(market.ticker, depth=self.book_depth)
        except KalshiError as exc:
            result.errors.append(f"{market.ticker}: orderbook: {exc}")
            log.warning("%s: orderbook failed: %s", market.ticker, exc)
        self.store.insert_snapshot(now, market, book)
        result.snapshots += 1

        try:
            since = self.store.last_trade_ts(market.ticker)
            trades = self.client.get_trades(
                market.ticker, limit=100, min_ts=int(since) if since else None
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
                # Not settled yet; refresh status so close_ts/status stay current.
                self.store.upsert_market(market, now)
        return settled

    # ------------------------------------------------------------ loop

    def stop(self) -> None:
        self._stop.set()

    def run(self, *, max_ticks: int | None = None, install_signals: bool = True) -> int:
        """Run until stopped. Returns the number of ticks completed."""
        if install_signals:
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: self.stop())
        log.info(
            "recording %s every %.0fs -> %s", ",".join(self.series), self.interval, self.store.path
        )
        ticks = 0
        consecutive_failures = 0
        while not self._stop.is_set():
            started = time.time()
            try:
                res = self.tick(started)
                consecutive_failures = 0 if not res.errors else consecutive_failures + 1
                log.info(
                    "tick markets=%d snapshots=%d trades+%d settled=%d spot=%d errors=%d",
                    res.markets,
                    res.snapshots,
                    res.new_trades,
                    res.settled,
                    res.spot,
                    len(res.errors),
                )
            except Exception:  # noqa: BLE001 - keep the loop alive
                consecutive_failures += 1
                log.exception("tick crashed")
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            # Back off when the exchange is unreachable, up to 60s.
            delay = (
                self.interval * min(12, 2 ** min(consecutive_failures, 4))
                if consecutive_failures
                else self.interval
            )
            elapsed = time.time() - started
            self._stop.wait(max(0.0, delay - elapsed))
        log.info("recorder stopped after %d ticks", ticks)
        return ticks
