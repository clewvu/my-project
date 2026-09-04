"""Sub-second spot prices from Coinbase's public WebSocket ticker channel.

Runs in a background thread with its own asyncio loop so the polling recorder
stays synchronous. Reconnects with backoff, treats silence as a dead
connection, and buffers ticks for batched writes to the store.

Two Coinbase feeds are supported; both are public and need no key:

* ``advanced``  wss://advanced-trade-ws.coinbase.com   (default)
* ``exchange``  wss://ws-feed.exchange.coinbase.com

Each stored tick is a price change, rate-limited per symbol so BTC's firehose
does not bloat the database. ``last_tick`` exposes the freshest price and its
age for stale-data guards.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

FEEDS = {
    "advanced": "wss://advanced-trade-ws.coinbase.com",
    "exchange": "wss://ws-feed.exchange.coinbase.com",
}
SOURCE = "coinbase_ws"


@dataclass(frozen=True)
class Tick:
    symbol: str
    price: float
    exchange_ts: float | None
    local_ts: float


def subscribe_message(feed: str, symbols: list[str]) -> str:
    if feed == "advanced":
        return json.dumps({"type": "subscribe", "product_ids": symbols, "channel": "ticker"})
    return json.dumps({"type": "subscribe", "product_ids": symbols, "channels": ["ticker"]})


def _parse_time(value: Any) -> float | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def parse_ticks(raw: str | bytes, local_ts: float | None = None) -> list[Tick]:
    """Extract ticks from one message of either feed. Unknown messages yield nothing."""
    local_ts = time.time() if local_ts is None else local_ts
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(msg, dict):
        return []
    ticks: list[Tick] = []
    # Exchange feed: one flat ticker per message.
    if msg.get("type") == "ticker" and "product_id" in msg:
        price = msg.get("price")
        if price:
            ticks.append(
                Tick(msg["product_id"], float(price), _parse_time(msg.get("time")), local_ts)
            )
        return ticks
    # Advanced Trade feed: events[].tickers[] with a message-level timestamp.
    if msg.get("channel") == "ticker":
        ts = _parse_time(msg.get("timestamp"))
        for event in msg.get("events", []) or []:
            for t in event.get("tickers", []) or []:
                price = t.get("price")
                if price and t.get("product_id"):
                    ticks.append(Tick(t["product_id"], float(price), ts, local_ts))
    return ticks


class TickBuffer:
    """Keeps the latest tick per symbol and queues price changes for storage.

    A change is queued at most once per ``min_interval`` seconds per symbol,
    except that the latest price is always queued when the buffer is drained
    if it differs from what was last written.
    """

    def __init__(self, min_interval: float = 0.2) -> None:
        self.min_interval = min_interval
        self.latest: dict[str, Tick] = {}
        self._last_written: dict[str, tuple[float, float]] = {}  # symbol -> (price, ts)
        self._queue: list[Tick] = []
        self._lock = threading.Lock()

    def push(self, tick: Tick) -> None:
        with self._lock:
            self.latest[tick.symbol] = tick
            last = self._last_written.get(tick.symbol)
            if last is not None:
                last_price, last_ts = last
                if tick.price == last_price or tick.local_ts - last_ts < self.min_interval:
                    return
            self._last_written[tick.symbol] = (tick.price, tick.local_ts)
            self._queue.append(tick)

    def drain(self) -> list[Tick]:
        with self._lock:
            for symbol, tick in self.latest.items():
                last = self._last_written.get(symbol)
                if last is None or last[0] != tick.price:
                    self._last_written[symbol] = (tick.price, tick.local_ts)
                    self._queue.append(tick)
            out, self._queue = self._queue, []
        return out

    def age(self, symbol: str, now: float | None = None) -> float | None:
        tick = self.latest.get(symbol)
        if tick is None:
            return None
        return (time.time() if now is None else now) - tick.local_ts


class SpotWebSocket:
    """Background WebSocket client writing ticks to a MarketDataStore."""

    def __init__(
        self,
        store: Any,
        symbols: list[str],
        *,
        feed: str = "advanced",
        url: str | None = None,
        min_interval: float = 0.2,
        flush_interval: float = 1.0,
        silence_timeout: float = 30.0,
    ) -> None:
        if feed not in FEEDS:
            raise ValueError(f"feed must be one of {sorted(FEEDS)}")
        self.store = store
        self.symbols = list(symbols)
        self.feed = feed
        self.url = url or FEEDS[feed]
        self.buffer = TickBuffer(min_interval)
        self.flush_interval = flush_interval
        self.silence_timeout = silence_timeout
        self.messages = 0
        self.written = 0
        self.reconnects = 0
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run_thread, name="spot-ws", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self.flush()

    def flush(self) -> int:
        ticks = self.buffer.drain()
        if ticks:
            self.store.insert_spots(
                [(t.local_ts, SOURCE, t.symbol, t.price, t.exchange_ts) for t in ticks]
            )
            self.written += len(ticks)
        return len(ticks)

    def last_tick(self, symbol: str) -> Tick | None:
        return self.buffer.latest.get(symbol)

    def age(self, symbol: str) -> float | None:
        return self.buffer.age(symbol)

    # ------------------------------------------------------------ internals

    def _run_thread(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:  # noqa: BLE001
            log.exception("spot websocket thread crashed")

    async def _main(self) -> None:
        from websockets.asyncio.client import connect

        backoff = 1.0
        flusher = asyncio.create_task(self._flush_loop())
        try:
            while not self._stop.is_set():
                try:
                    async with connect(self.url, open_timeout=10, ping_interval=20) as ws:
                        await ws.send(subscribe_message(self.feed, self.symbols))
                        log.info("spot websocket connected: %s %s", self.feed, self.symbols)
                        backoff = 1.0
                        await self._consume(ws)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - reconnect on anything
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    log.warning(
                        "spot websocket error: %s (retry in %.0fs)", self.last_error, backoff
                    )
                if self._stop.is_set():
                    break
                self.reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)
        finally:
            flusher.cancel()

    async def _consume(self, ws: Any) -> None:
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self.silence_timeout)
            except TimeoutError:
                raise RuntimeError(f"no message for {self.silence_timeout:.0f}s") from None
            self.messages += 1
            now = time.time()
            for tick in parse_ticks(raw, now):
                self.buffer.push(tick)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval)
            try:
                self.flush()
            except Exception:  # noqa: BLE001
                log.exception("spot flush failed")
