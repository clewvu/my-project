"""Thin, well-behaved HTTP client for the Kalshi Trade API v2.

Responsibilities:
  * sign every request (see ``auth.Signer``)
  * space requests out and back off on 429 / 5xx
  * log every call with method, path, status and latency
  * refuse to place or cancel orders when ``dry_run`` is on
  * require an explicit ``allow_live`` flag before trading on production

Everything returns plain dataclasses from ``models``; nothing here knows about
strategy or risk.
"""

from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

import httpx

from .auth import Signer
from .config import DEMO_BASE_URL, Settings
from .models import TICK, Balance, Candle, Fill, Market, Order, Orderbook, Position, Trade

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
VALID_SIDES = {"yes", "no"}
VALID_ACTIONS = {"buy", "sell"}
VALID_ORDER_TYPES = {"limit", "market"}


class KalshiError(Exception):
    """Base error for client failures."""


class KalshiAPIError(KalshiError):
    def __init__(self, status: int, method: str, path: str, body: str) -> None:
        self.status = status
        self.method = method
        self.path = path
        self.body = body
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:300]}")


class LiveTradingBlocked(KalshiError):
    """Raised when an order would hit production without ``allow_live=True``."""


VALID_TIME_IN_FORCE = {"good_till_canceled", "immediate_or_cancel", "fill_or_kill"}


def book_side_and_price(side: str, action: str, price: float) -> tuple[str, float]:
    """Map an outcome-side order to the single YES book of the V2 endpoint.

    buy YES at p  -> bid at p         sell YES at p -> ask at p
    buy NO at q   -> ask at 1 - q     sell NO at q  -> bid at 1 - q
    """
    if side == "yes":
        return ("bid" if action == "buy" else "ask"), price
    yes_price = round(1.0 - price, 4)
    return ("ask" if action == "buy" else "bid"), yes_price


def validate_price(price: float) -> float:
    """Check a dollar price is on Kalshi's 0.001 grid strictly between 0 and 1."""
    if isinstance(price, bool) or not isinstance(price, int | float):
        raise ValueError("price must be a number of dollars, e.g. 0.45")
    ticks = round(price / TICK)
    if abs(ticks * TICK - price) > 1e-9:
        raise ValueError(f"price {price} is not a multiple of {TICK}")
    if not 1 <= ticks <= 999:
        raise ValueError("price must be between 0.001 and 0.999")
    return round(ticks * TICK, 4)


class DryRunOrder(dict):
    """What ``create_order`` returns in dry-run mode: the request that would have been sent."""


class KalshiClient:
    def __init__(
        self,
        base_url: str = DEMO_BASE_URL,
        signer: Signer | None = None,
        *,
        dry_run: bool = True,
        allow_live: bool = False,
        min_request_interval: float = 0.15,
        max_retries: int = 5,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.signer = signer
        self.dry_run = dry_run
        self.allow_live = allow_live
        self.min_request_interval = max(0.0, min_request_interval)
        self.max_retries = max(1, max_retries)
        self._path_prefix = urlsplit(self.base_url).path  # e.g. /trade-api/v2
        self._lock = threading.Lock()
        self._last_request_at = 0.0
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "kalshi-bot/0.1"},
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, *, allow_live: bool = False, **kwargs: Any
    ) -> KalshiClient:
        signer = None
        if settings.has_credentials:
            signer = Signer.from_pem_path(settings.api_key_id, settings.private_key_path)  # type: ignore[arg-type]
        return cls(
            settings.base_url,
            signer,
            dry_run=settings.dry_run,
            allow_live=allow_live,
            min_request_interval=settings.min_request_interval,
            **kwargs,
        )

    @property
    def is_prod(self) -> bool:
        return "demo" not in urlsplit(self.base_url).netloc

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> KalshiClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ transport

    def _throttle(self) -> None:
        with self._lock:
            wait = self._last_request_at + self.min_request_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        if not path.startswith("/"):
            path = "/" + path
        params = {k: v for k, v in (params or {}).items() if v is not None}
        signed_path = self._path_prefix + path

        for attempt in range(1, self.max_retries + 1):
            headers: dict[str, str] = {}
            if auth:
                if self.signer is None:
                    raise KalshiError(f"{method} {path} requires credentials; none configured")
                headers = self.signer.headers(method, signed_path)

            self._throttle()
            started = time.monotonic()
            try:
                resp = self._http.request(method, path, params=params, json=json, headers=headers)
            except httpx.TransportError as exc:
                elapsed = (time.monotonic() - started) * 1000
                log.warning(
                    "%s %s transport error after %.0fms (attempt %d/%d): %s",
                    method,
                    path,
                    elapsed,
                    attempt,
                    self.max_retries,
                    exc,
                )
                if attempt == self.max_retries:
                    raise KalshiError(f"{method} {path} failed: {exc}") from exc
                self._sleep_backoff(attempt)
                continue

            elapsed = (time.monotonic() - started) * 1000
            log.log(
                logging.DEBUG if resp.is_success else logging.WARNING,
                "%s %s -> %d (%.0fms)",
                method,
                path,
                resp.status_code,
                elapsed,
            )

            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                self._sleep_backoff(attempt, rate_limited=resp.status_code == 429)
                continue
            if not resp.is_success:
                raise KalshiAPIError(resp.status_code, method, path, resp.text)
            if not resp.content:
                return {}
            return resp.json()

        raise KalshiError(f"{method} {path}: retries exhausted")  # pragma: no cover

    @staticmethod
    def _sleep_backoff(attempt: int, rate_limited: bool = False) -> None:
        base = 0.5 if rate_limited else 0.25
        delay = min(8.0, base * (2 ** (attempt - 1))) + random.uniform(0, 0.25)
        time.sleep(delay)

    # ------------------------------------------------------------------ exchange

    def exchange_status(self) -> dict[str, Any]:
        return self._request("GET", "/exchange/status", auth=False)

    # ------------------------------------------------------------------ markets

    def get_markets(
        self,
        *,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = None,
        tickers: list[str] | None = None,
        min_close_ts: int | None = None,
        max_close_ts: int | None = None,
        limit: int = 100,
        max_pages: int = 10,
    ) -> list[Market]:
        """List markets, following pagination up to ``max_pages``."""
        markets: list[Market] = []
        cursor: str | None = None
        for _ in range(max_pages):
            data = self._request(
                "GET",
                "/markets",
                params={
                    "series_ticker": series_ticker,
                    "event_ticker": event_ticker,
                    "status": status,
                    "tickers": ",".join(tickers) if tickers else None,
                    "min_close_ts": min_close_ts,
                    "max_close_ts": max_close_ts,
                    "limit": limit,
                    "cursor": cursor,
                },
                auth=False,
            )
            markets.extend(Market.from_dict(m) for m in data.get("markets", []))
            cursor = data.get("cursor") or None
            if not cursor:
                break
        return markets

    def get_market(self, ticker: str) -> Market:
        data = self._request("GET", f"/markets/{ticker}", auth=False)
        return Market.from_dict(data.get("market", data))

    def get_orderbook(self, ticker: str, depth: int | None = None) -> Orderbook:
        data = self._request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}, auth=False
        )
        return Orderbook.from_dict(ticker, data)

    def get_candlesticks(
        self,
        series_ticker: str,
        ticker: str,
        *,
        start_ts: int,
        end_ts: int,
        period_interval: int = 1,
    ) -> list[Candle]:
        """OHLC candles for a market. ``period_interval`` is minutes: 1, 60 or 1440."""
        data = self._request(
            "GET",
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
            auth=False,
        )
        return [Candle.from_dict(c) for c in data.get("candlesticks", [])]

    def get_trades(
        self,
        ticker: str,
        *,
        limit: int = 1000,
        min_ts: int | None = None,
        max_ts: int | None = None,
        max_pages: int = 5,
    ) -> list[Trade]:
        """Public trade prints, following the cursor so bursts are not truncated."""
        trades: list[Trade] = []
        cursor: str | None = None
        for _ in range(max_pages):
            data = self._request(
                "GET",
                "/markets/trades",
                params={
                    "ticker": ticker,
                    "limit": limit,
                    "min_ts": min_ts,
                    "max_ts": max_ts,
                    "cursor": cursor,
                },
                auth=False,
            )
            page = data.get("trades", [])
            trades.extend(Trade.from_dict(t) for t in page)
            cursor = data.get("cursor") or None
            if not cursor or len(page) < limit:
                break
        else:
            log.warning("%s: trades still paginating after %d pages", ticker, max_pages)
        return trades

    # ------------------------------------------------------------------ portfolio

    def get_balance(self) -> Balance:
        return Balance.from_dict(self._request("GET", "/portfolio/balance"))

    def get_positions(
        self,
        *,
        ticker: str | None = None,
        event_ticker: str | None = None,
        settlement_status: str | None = None,
        limit: int = 100,
    ) -> list[Position]:
        data = self._request(
            "GET",
            "/portfolio/positions",
            params={
                "ticker": ticker,
                "event_ticker": event_ticker,
                "settlement_status": settlement_status,
                "limit": limit,
            },
        )
        return [
            Position.from_dict(p) for p in data.get("market_positions", data.get("positions", []))
        ]

    def get_orders(
        self, *, ticker: str | None = None, status: str | None = None, limit: int = 100
    ) -> list[Order]:
        data = self._request(
            "GET", "/portfolio/orders", params={"ticker": ticker, "status": status, "limit": limit}
        )
        return [Order.from_dict(o) for o in data.get("orders", [])]

    def get_fills(
        self,
        *,
        ticker: str | None = None,
        order_id: str | None = None,
        min_ts: int | None = None,
        limit: int = 100,
    ) -> list[Fill]:
        data = self._request(
            "GET",
            "/portfolio/fills",
            params={"ticker": ticker, "order_id": order_id, "min_ts": min_ts, "limit": limit},
        )
        return [Fill.from_dict(f) for f in data.get("fills", [])]

    # ------------------------------------------------------------------ orders

    def _guard_trading(self, what: str) -> None:
        if self.is_prod and not self.allow_live:
            raise LiveTradingBlocked(
                f"Refusing to {what} on production: client was created without allow_live=True"
            )

    def create_order(
        self,
        ticker: str,
        *,
        side: str,
        action: str,
        count: int,
        price: float | None = None,
        order_type: str = "limit",
        client_order_id: str | None = None,
        expiration_ts: int | None = None,
        time_in_force: str = "good_till_canceled",
    ) -> Order | DryRunOrder:
        """Place a limit order through the V2 endpoint (``POST /portfolio/events/orders``).

        The call keeps the outcome-side vocabulary (``side`` yes|no, ``action``
        buy|sell, ``price`` in dollars on that side, 0.001 grid) and translates
        it to the single YES-book shape the V2 endpoint wants: buying YES at p is
        a ``bid`` at p, buying NO at q is an ``ask`` at 1 - q, and so on. The
        legacy ``/portfolio/orders`` endpoint was retired by Kalshi in 2026 and
        answers 410. Market orders are not supported; use a limit at the ask.
        In dry-run mode nothing is sent and the would-be request is returned.
        """
        if side not in VALID_SIDES:
            raise ValueError(f"side must be one of {sorted(VALID_SIDES)}")
        if action not in VALID_ACTIONS:
            raise ValueError(f"action must be one of {sorted(VALID_ACTIONS)}")
        if order_type not in VALID_ORDER_TYPES:
            raise ValueError(f"order_type must be one of {sorted(VALID_ORDER_TYPES)}")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("count must be a positive integer number of contracts")
        if order_type != "limit":
            raise ValueError("only limit orders are supported by the V2 order endpoint")
        if price is None:
            raise ValueError("limit orders need a price")
        if time_in_force not in VALID_TIME_IN_FORCE:
            raise ValueError(f"time_in_force must be one of {sorted(VALID_TIME_IN_FORCE)}")
        price = validate_price(price)
        book_side, yes_price = book_side_and_price(side, action, price)

        body: dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": book_side,
            "count": str(count),
            "price": f"{yes_price:.4f}",
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
        }
        if expiration_ts is not None:
            body["expiration_time"] = int(expiration_ts)

        if self.dry_run:
            log.info("DRY RUN create_order %s", body)
            return DryRunOrder(body)

        self._guard_trading("place an order")
        log.info("create_order %s (%s %s @ %.4f)", body, action, side, price)
        data = self._request("POST", "/portfolio/events/orders", json=body)
        data = data.get("order", data)
        filled = float(data.get("fill_count") or 0)
        remaining = float(data.get("remaining_count") or 0)
        return Order.from_dict(
            {
                "order_id": data.get("order_id"),
                "client_order_id": data.get("client_order_id") or body["client_order_id"],
                "ticker": ticker,
                "side": side,
                "action": action,
                "type": "limit",
                "status": "executed" if remaining <= 0 and filled > 0 else "resting",
                f"{side}_price_dollars": f"{price:.4f}",
                "count_fp": f"{filled + remaining:.2f}",
                "remaining_count_fp": f"{remaining:.2f}",
                "fill_count_fp": f"{filled:.2f}",
                "created_time": (data["ts_ms"] / 1000) if data.get("ts_ms") else None,
            }
        )

    def cancel_order(self, order_id: str, *, ticker: str | None = None) -> Order | None:
        """Cancel one order (V2 endpoint). ``ticker`` lets the exchange route the request."""
        if self.dry_run:
            log.info("DRY RUN cancel_order %s", order_id)
            return None
        self._guard_trading("cancel an order")
        log.info("cancel_order %s", order_id)
        data = self._request(
            "DELETE",
            f"/portfolio/events/orders/{order_id}",
            params={"market_ticker": ticker},
        )
        if not data:
            return None
        data = data.get("order", data)
        return Order.from_dict(
            {
                **data,
                "order_id": data.get("order_id") or order_id,
                "status": data.get("status") or "canceled",
            }
        )

    def cancel_all_orders(self, *, ticker: str | None = None) -> list[str]:
        """Cancel every resting order (optionally only for one ticker). Returns order ids."""
        resting = self.get_orders(ticker=ticker, status="resting")
        cancelled: list[str] = []
        for order in resting:
            self.cancel_order(order.order_id, ticker=order.ticker or None)
            cancelled.append(order.order_id)
        return cancelled
