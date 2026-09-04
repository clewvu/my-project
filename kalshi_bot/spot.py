"""External spot price feeds, used only for research data.

Kalshi's 15-minute crypto markets settle on a reference price. Recording an
independent spot series alongside the book lets us test whether the market's
implied probability lags the underlying.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

COINBASE_URL = "https://api.coinbase.com/v2/prices/{symbol}/spot"


class SpotFeed:
    """Fetches spot prices from Coinbase's public price endpoint (no auth)."""

    source = "coinbase"

    def __init__(
        self,
        symbols: list[str],
        *,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.symbols = list(symbols)
        self._http = httpx.Client(
            timeout=timeout, transport=transport, headers={"User-Agent": "kalshi-bot/0.1"}
        )

    def close(self) -> None:
        self._http.close()

    def fetch(self) -> dict[str, float]:
        """Return {symbol: price} for every symbol that could be fetched."""
        out: dict[str, float] = {}
        for symbol in self.symbols:
            try:
                resp = self._http.get(COINBASE_URL.format(symbol=symbol))
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                out[symbol] = float(data["data"]["amount"])
            except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
                log.warning("spot %s fetch failed: %s", symbol, exc)
        return out
