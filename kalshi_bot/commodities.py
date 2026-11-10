"""Keyless spot prices for Kalshi's 15-minute precious-metal markets.

Kalshi settles its metal and energy series on Pyth Network feeds: the strike is
the close of the one-minute Pyth candle at the window's open, the settlement
value the close at the window's end. Pyth's Hermes API has needed a key since
2026, so without ``PYTH_API_KEY`` the trader had no spot for these series and
skipped them on every tick.

This module is the fallback:

* :class:`ProxyFeed` polls Swissquote's free public quotes for spot gold,
  silver, platinum and palladium every couple of seconds.
* :class:`Rebaser` corrects the level. A broker's spot differs from the Pyth
  feed by more than a 15-minute move (gold: 1-2% against the index), but at the
  open of each window the strike *is* the Pyth price, so
  ``ratio = strike / proxy_price_at_open`` maps the proxy onto the Pyth level
  for that window. The ratio is only valid for the window it was anchored on.

Volatility is estimated from the raw quotes (log returns do not care about the
level); only the price compared with the strike is rebased. Raw rows are stored
under source ``swissquote`` and rebased rows under ``swissquote_rebased``.

Copper, WTI, Brent and natural gas have no free real-time source; they need a
Pyth key.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import Any

import httpx

from .spot_ws import Tick

log = logging.getLogger(__name__)

RAW_SOURCE = "swissquote"
REBASED_SOURCE = "swissquote_rebased"
SWISSQUOTE_URL = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/{pair}"

ANCHOR_TOLERANCE_S = 15.0  # the raw quote used to anchor must be this close to the open
ANCHOR_MAX_LAG_S = 600.0  # a window may still be anchored this long after its open
RAW_KEEP_S = 900.0  # raw quotes buffered per symbol for late anchoring


def parse_quote(payload: Any) -> tuple[float, float | None] | None:
    """(mid, quote_ts) of the tightest quote in one instrument response, or None."""
    if not isinstance(payload, list):
        return None
    best: tuple[float, float, float | None] | None = None
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        try:
            ts = float(entry["ts"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            ts = None
        for prof in entry.get("spreadProfilePrices") or []:
            try:
                bid, ask = float(prof["bid"]), float(prof["ask"])
            except (KeyError, TypeError, ValueError):
                continue
            if bid <= 0 or ask < bid:
                continue
            if best is None or ask - bid < best[1] - best[0]:
                best = (bid, ask, ts)
    if best is None:
        return None
    return (best[0] + best[1]) / 2, best[2]


class Rebaser:
    """One proxy-to-Pyth ratio per symbol, anchored at the open of the current window."""

    def __init__(self, store: Any = None) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._raw: dict[str, deque[tuple[float, float]]] = {}
        # symbol -> (open_ts, close_ts, ratio)
        self._anchor: dict[str, tuple[float, float, float]] = {}
        self._load()

    def _load(self) -> None:
        if self.store is None or not hasattr(self.store, "get_meta"):
            return
        raw = self.store.get_meta("proxy_ratios")
        if not raw:
            return
        try:
            for symbol, (o, c, r) in json.loads(raw).items():
                if float(r) > 0:
                    self._anchor[symbol] = (float(o), float(c), float(r))
        except (TypeError, ValueError):
            log.warning("ignoring unreadable proxy_ratios in the store")

    def _persist(self) -> None:
        if self.store is None or not hasattr(self.store, "set_meta"):
            return
        try:
            self.store.set_meta({"proxy_ratios": json.dumps(self._anchor)})
        except Exception as exc:  # noqa: BLE001 - persistence only helps a restart
            log.warning("could not persist proxy ratios: %s", exc)

    def push(self, symbol: str, ts: float, price: float) -> None:
        """Buffer a raw quote so a window listed late can still be anchored at its open."""
        if price <= 0:
            return
        with self._lock:
            q = self._raw.setdefault(symbol, deque())
            if q and ts <= q[-1][0]:
                return
            q.append((ts, price))
            while q and q[0][0] < ts - RAW_KEEP_S:
                q.popleft()

    def _raw_at(self, symbol: str, ts: float) -> float | None:
        q = self._raw.get(symbol)
        if not q:
            return None
        best = min(q, key=lambda tp: abs(tp[0] - ts))
        return best[1] if abs(best[0] - ts) <= ANCHOR_TOLERANCE_S else None

    def observe(
        self, symbol: str, open_ts: float, close_ts: float, strike: float, now: float
    ) -> float | None:
        """Ratio for the window ``open_ts``..``close_ts``, anchoring it if possible.

        None when the window cannot be anchored: no raw quote was buffered within
        ``ANCHOR_TOLERANCE_S`` of its open (the bot started mid-window, or the feed
        was down), in which case the series is skipped until the next window.
        """
        with self._lock:
            held = self._anchor.get(symbol)
            if held is not None and held[0] == open_ts:
                return held[2]
            if strike <= 0 or not 0 <= now - open_ts <= ANCHOR_MAX_LAG_S:
                return None
            raw = self._raw_at(symbol, open_ts)
            if raw is None:
                return None
            ratio = strike / raw
            self._anchor[symbol] = (open_ts, close_ts, ratio)
            self._persist()
        log.info("%s: proxy ratio %.5f anchored at window open %.0f", symbol, ratio, open_ts)
        return ratio

    def current(self, symbol: str, now: float) -> float | None:
        """Ratio if ``now`` falls inside the window it was anchored on."""
        with self._lock:
            held = self._anchor.get(symbol)
        if held is None or not held[0] <= now < held[1]:
            return None
        return held[2]


class _Latest:
    """The slice of ``TickBuffer`` the trader reads: latest tick per symbol."""

    def __init__(self) -> None:
        self.latest: dict[str, Tick] = {}


class ProxyFeed:
    """Poll Swissquote on a background thread; expose latest raw ticks and the rebaser."""

    def __init__(
        self,
        store: Any,
        symbols: list[str],
        *,
        interval: float = 2.0,
        max_quote_age: float = 30.0,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.store = store
        self.symbols = list(symbols)
        self.interval = interval
        self.max_quote_age = max_quote_age
        self.buffer = _Latest()
        self.rebaser = Rebaser(store)
        self.written = 0
        self.last_error: str | None = None
        self._http = httpx.Client(
            timeout=timeout, transport=transport, headers={"User-Agent": "kalshi-bot/0.1"}
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll_once(self, now: float | None = None) -> int:
        rows: list[tuple[float, str, str, float, float | None]] = []
        for symbol in self.symbols:
            try:
                resp = self._http.get(SWISSQUOTE_URL.format(pair=symbol))
                resp.raise_for_status()
                quote = parse_quote(resp.json())
            except (httpx.HTTPError, ValueError) as exc:
                self.last_error = f"{symbol}: {exc}"
                log.debug("swissquote %s fetch failed: %s", symbol, exc)
                continue
            ts = time.time() if now is None else now
            if quote is None:
                continue
            mid, quote_ts = quote
            if quote_ts is not None and ts - quote_ts > self.max_quote_age:
                continue  # market closed or feed frozen: do not present an old quote as fresh
            self.buffer.latest[symbol] = Tick(symbol, mid, quote_ts, ts)
            self.rebaser.push(symbol, ts, mid)
            rows.append((ts, RAW_SOURCE, symbol, mid, quote_ts))
            ratio = self.rebaser.current(symbol, ts)
            if ratio is not None:
                rows.append((ts, REBASED_SOURCE, symbol, mid * ratio, quote_ts))
        if rows and self.store is not None:
            self.store.insert_spots(rows)
            self.written += len(rows)
        return len(rows)

    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            started = time.time()
            try:
                got = self.poll_once()
                failures = 0 if got else failures + 1
                if failures == 30:
                    log.warning(
                        "swissquote: no fresh quotes for %d polls (%s)",
                        failures,
                        self.last_error or "markets closed?",
                    )
            except Exception:  # noqa: BLE001 - keep polling
                log.exception("proxy feed poll crashed; continuing")
            self._stop.wait(max(0.0, self.interval - (time.time() - started)))

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="proxy-feed", daemon=True)
            self._thread.start()
            log.info("proxy feed started: swissquote %s", self.symbols)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self._http.close()
