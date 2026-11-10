"""Pyth Network prices via the public Hermes API.

Kalshi's 15-minute metal and energy markets settle on Pyth, so this is the
settlement reference itself for those series and a cross-check for crypto.

Endpoints (no key needed):

* ``GET /v2/price_feeds?query=XAU&asset_type=metal`` lists feeds with ids.
* ``GET /v2/updates/price/latest?ids[]=..&parsed=true`` latest prices.
* ``GET /v2/updates/price/stream?ids[]=..&parsed=true`` server-sent events.

Prices are integers scaled by ``10**expo``. ``publish_time`` is unix seconds.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .spot_ws import Tick, TickBuffer

log = logging.getLogger(__name__)

HERMES = "https://hermes.pyth.network"
SOURCE = "pyth"


@dataclass(frozen=True)
class Feed:
    id: str
    symbol: str  # e.g. Metal.XAU/USD
    asset_type: str
    description: str


def parse_feeds(raw: list[dict[str, Any]]) -> list[Feed]:
    out = []
    for item in raw or []:
        attrs = item.get("attributes", {}) or {}
        if not item.get("id") or not attrs.get("symbol"):
            continue
        out.append(
            Feed(
                id=str(item["id"]).removeprefix("0x").lower(),
                symbol=attrs["symbol"],
                asset_type=attrs.get("asset_type", ""),
                description=attrs.get("description", ""),
            )
        )
    return out


def parse_prices(
    payload: dict[str, Any], id_to_symbol: dict[str, str], local_ts: float | None = None
) -> list[Tick]:
    """Ticks from a ``/latest`` or stream message. Unknown ids are skipped."""
    local_ts = time.time() if local_ts is None else local_ts
    ticks: list[Tick] = []
    for item in payload.get("parsed", []) or []:
        fid = str(item.get("id", "")).removeprefix("0x").lower()
        symbol = id_to_symbol.get(fid)
        price = item.get("price") or {}
        if symbol is None or price.get("price") is None or price.get("expo") is None:
            continue
        try:
            value = int(price["price"]) * (10 ** int(price["expo"]))
        except (TypeError, ValueError):
            continue
        ticks.append(Tick(symbol, float(value), _float(price.get("publish_time")), local_ts))
    return ticks


def _float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class PythClient:
    """Feed discovery plus a REST poll; see ``PythStream`` for sub-second data."""

    def __init__(
        self,
        base_url: str = HERMES,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        api_key: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # Hermes price updates need a Pyth API key (Bearer token) since late 2026;
        # feed search is still public. Read PYTH_API_KEY from the environment/.env.
        self.api_key = (api_key if api_key is not None else os.getenv("PYTH_API_KEY", "")).strip()
        headers = {"User-Agent": "kalshi-bot/0.1"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers=headers,
        )

    def close(self) -> None:
        self._http.close()

    def search_feeds(self, query: str, asset_type: str | None = None) -> list[Feed]:
        params: dict[str, str] = {"query": query}
        if asset_type:
            params["asset_type"] = asset_type
        resp = self._http.get("/v2/price_feeds", params=params)
        resp.raise_for_status()
        return parse_feeds(resp.json())

    def resolve(self, symbols: list[str]) -> dict[str, Feed]:
        """Map Pyth symbols like ``Metal.XAU/USD`` to feeds, by exact symbol match."""
        found: dict[str, Feed] = {}
        for symbol in symbols:
            # Metal.Index.SILVER/USD -> SILVER, Crypto.BTC/USD -> BTC
            base = symbol.split("/", 1)[0].rsplit(".", 1)[-1]
            try:
                feeds = self.search_feeds(base)
            except httpx.HTTPError as exc:
                log.warning("pyth: feed lookup for %s failed: %s", symbol, exc)
                continue
            match = next((f for f in feeds if f.symbol == symbol), None)
            if match is None:
                log.warning(
                    "pyth: no feed with symbol %s (candidates: %s)",
                    symbol,
                    ", ".join(f.symbol for f in feeds[:6]),
                )
                continue
            found[symbol] = match
        return found

    def latest(self, feeds: dict[str, Feed]) -> list[Tick]:
        if not feeds:
            return []
        params = [("ids[]", f.id) for f in feeds.values()] + [("parsed", "true")]
        resp = self._http.get("/v2/updates/price/latest", params=params)
        resp.raise_for_status()
        return parse_prices(resp.json(), {f.id: s for s, f in feeds.items()})


class PythStream:
    """Background SSE consumer writing ticks to the store, mirroring ``SpotWebSocket``."""

    def __init__(
        self,
        store: Any,
        symbols: list[str],
        *,
        base_url: str = HERMES,
        min_interval: float = 0.2,
        silence_timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.store = store
        self.symbols = list(symbols)
        self.client = PythClient(base_url, transport=transport)
        self.buffer = TickBuffer(min_interval)
        self.silence_timeout = silence_timeout
        self.feeds: dict[str, Feed] = {}
        self.messages = 0
        self.written = 0
        self.reconnects = 0
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="pyth-sse", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self.flush()
        self.client.close()

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

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if not self.feeds:
                    self.feeds = self.client.resolve(self.symbols)
                    missing = set(self.symbols) - set(self.feeds)
                    if missing:
                        log.warning("pyth: unresolved symbols: %s", sorted(missing))
                    if not self.feeds:
                        raise RuntimeError("no Pyth feeds resolved")
                self._consume()
                backoff = 1.0
            except httpx.HTTPStatusError as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                if exc.response.status_code in (401, 403):
                    # No point retrying: the endpoint wants a key we do not have.
                    log.warning(
                        "pyth: Hermes rejected the request (%s). Price updates need a Pyth API "
                        "key; set PYTH_API_KEY in .env. Pyth stream disabled for this run.",
                        exc.response.status_code if not self.client.api_key else "key refused",
                    )
                    return
                log.warning("pyth stream error: %s (retry in %.0fs)", self.last_error, backoff)
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("pyth stream error: %s (retry in %.0fs)", self.last_error, backoff)
            if self._stop.is_set():
                break
            self.reconnects += 1
            self._stop.wait(backoff)
            backoff = min(60.0, backoff * 2)

    def _consume(self) -> None:
        params = [("ids[]", f.id) for f in self.feeds.values()] + [("parsed", "true")]
        id_to_symbol = {f.id: s for s, f in self.feeds.items()}
        with self.client._http.stream(
            "GET",
            "/v2/updates/price/stream",
            params=params,
            timeout=httpx.Timeout(10.0, read=self.silence_timeout),
        ) as r:
            r.raise_for_status()
            log.info("pyth stream connected: %s", sorted(self.feeds))
            for line in r.iter_lines():
                if self._stop.is_set():
                    return
                if not line.startswith("data:"):
                    continue
                self.messages += 1
                try:
                    payload = json.loads(line[5:].strip())
                except ValueError:
                    continue
                now = time.time()
                for tick in parse_prices(payload, id_to_symbol, now):
                    self.buffer.push(tick)
