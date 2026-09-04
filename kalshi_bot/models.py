"""Typed views over Kalshi API JSON.

All prices are integer cents (1..99). All sizes are integer contract counts.
The API also returns ``*_dollars`` string fields in newer versions; we ignore
those and read the cent fields, which remain present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _cents(value: Any) -> int | None:
    """Integer cents from a cents number, or from a dollars string such as "0.45"."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return round(float(value) * 100)
    return round(float(value))


def _count(value: Any) -> int:
    """Contract counts arrive as ints, floats, or numeric strings; treat missing as 0."""
    if value in (None, ""):
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _price(d: dict[str, Any], name: str) -> int | None:
    """Read ``name`` in cents, falling back to ``name_dollars`` (a dollars string)."""
    value = d.get(name)
    if value is None:
        value = d.get(f"{name}_dollars")
    return _cents(value)


@dataclass(frozen=True)
class Market:
    ticker: str
    event_ticker: str | None
    series_ticker: str | None
    title: str
    status: str
    yes_bid: int | None
    yes_ask: int | None
    no_bid: int | None
    no_ask: int | None
    last_price: int | None
    volume: int
    open_time: datetime | None
    close_time: datetime | None
    expiration_time: datetime | None
    result: str | None
    raw: dict[str, Any] = field(repr=False, compare=False, default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Market:
        return cls(
            ticker=d["ticker"],
            event_ticker=d.get("event_ticker"),
            series_ticker=d.get("series_ticker"),
            title=d.get("title") or "",
            status=d.get("status") or "",
            yes_bid=_price(d, "yes_bid"),
            yes_ask=_price(d, "yes_ask"),
            no_bid=_price(d, "no_bid"),
            no_ask=_price(d, "no_ask"),
            last_price=_price(d, "last_price"),
            volume=_count(d.get("volume", d.get("volume_fp"))),
            open_time=_parse_time(d.get("open_time")),
            close_time=_parse_time(d.get("close_time")),
            expiration_time=_parse_time(d.get("expiration_time")),
            result=d.get("result") or None,
            raw=d,
        )

    @property
    def yes_mid(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def spread(self) -> int | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    def seconds_to_close(self, now: datetime | None = None) -> float | None:
        if self.close_time is None:
            return None
        now = now or datetime.now(UTC)
        return (self.close_time - now).total_seconds()


@dataclass(frozen=True)
class Level:
    price: int  # cents
    count: int  # contracts resting at this price


@dataclass(frozen=True)
class Orderbook:
    """Resting bids on each side.

    ``yes`` are bids to buy YES at ``price``; ``no`` are bids to buy NO.
    A YES bid at p is equivalent to a NO ask at 100-p, and vice versa.
    Levels are sorted best-first (highest bid first).
    """

    ticker: str
    yes: list[Level]
    no: list[Level]

    @classmethod
    def from_dict(cls, ticker: str, d: dict[str, Any]) -> Orderbook:
        book = d.get("orderbook", d) or {}
        yes = book.get("yes")
        if yes is None:
            yes = book.get("yes_dollars", book.get("true"))
        no = book.get("no")
        if no is None:
            no = book.get("no_dollars", book.get("false"))
        return cls(ticker=ticker, yes=cls._levels(yes), no=cls._levels(no))

    @staticmethod
    def _levels(raw: Any) -> list[Level]:
        levels: list[Level] = []
        for item in raw or []:
            if isinstance(item, dict):
                price, count = item.get("price"), item.get("count")
            else:
                price, count = item[0], item[1]
            if price is None or count is None:
                continue
            levels.append(Level(price=_cents(price) or 0, count=int(count)))
        levels.sort(key=lambda lv: lv.price, reverse=True)
        return levels

    @property
    def best_yes_bid(self) -> int | None:
        return self.yes[0].price if self.yes else None

    @property
    def best_no_bid(self) -> int | None:
        return self.no[0].price if self.no else None

    @property
    def best_yes_ask(self) -> int | None:
        return 100 - self.best_no_bid if self.best_no_bid is not None else None

    @property
    def best_no_ask(self) -> int | None:
        return 100 - self.best_yes_bid if self.best_yes_bid is not None else None

    @property
    def yes_mid(self) -> float | None:
        if self.best_yes_bid is None or self.best_yes_ask is None:
            return None
        return (self.best_yes_bid + self.best_yes_ask) / 2

    def depth(self, side: str, max_levels: int | None = None) -> int:
        levels = self.yes if side == "yes" else self.no
        if max_levels is not None:
            levels = levels[:max_levels]
        return sum(lv.count for lv in levels)


@dataclass(frozen=True)
class Balance:
    balance: int  # cents available
    portfolio_value: int | None = None  # cents, if the API reports it

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Balance:
        return cls(
            balance=int(d.get("balance") or 0),
            portfolio_value=(
                int(d["portfolio_value"]) if d.get("portfolio_value") is not None else None
            ),
        )

    @property
    def dollars(self) -> float:
        return self.balance / 100


@dataclass(frozen=True)
class Position:
    ticker: str
    event_ticker: str | None
    position: int  # >0 long YES, <0 long NO
    total_cost: int  # cents
    realized_pnl: int  # cents
    fees_paid: int  # cents
    resting_order_count: int
    market_result: str | None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Position:
        return cls(
            ticker=d["ticker"],
            event_ticker=d.get("event_ticker"),
            position=int(d.get("position") or 0),
            total_cost=int(d.get("total_cost") or d.get("total_traded") or 0),
            realized_pnl=int(d.get("realized_pnl") or 0),
            fees_paid=int(d.get("fees_paid") or 0),
            resting_order_count=int(d.get("resting_order_count") or 0),
            market_result=d.get("market_result") or None,
        )

    @property
    def side(self) -> str | None:
        if self.position > 0:
            return "yes"
        if self.position < 0:
            return "no"
        return None


@dataclass(frozen=True)
class Order:
    order_id: str
    client_order_id: str | None
    ticker: str
    side: str  # yes | no
    action: str  # buy | sell
    type: str  # limit | market
    status: str  # resting | canceled | executed | pending
    yes_price: int | None
    no_price: int | None
    count: int
    remaining_count: int
    created_time: datetime | None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Order:
        return cls(
            order_id=d.get("order_id") or "",
            client_order_id=d.get("client_order_id"),
            ticker=d.get("ticker") or "",
            side=d.get("side") or "",
            action=d.get("action") or "",
            type=d.get("type") or "",
            status=d.get("status") or "",
            yes_price=_price(d, "yes_price"),
            no_price=_price(d, "no_price"),
            count=int(d.get("count") or d.get("initial_count") or 0),
            remaining_count=int(d.get("remaining_count") or 0),
            created_time=_parse_time(d.get("created_time")),
        )


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    ticker: str
    side: str
    action: str
    count: int
    price: int  # cents, on the fill's side
    is_taker: bool
    created_time: datetime | None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Fill:
        side = d.get("side") or ""
        price = d.get("price")
        if price is None:
            price = _price(d, "yes_price") if side == "yes" else _price(d, "no_price")
        return cls(
            fill_id=d.get("fill_id") or d.get("trade_id") or "",
            order_id=d.get("order_id") or "",
            ticker=d.get("ticker") or "",
            side=side,
            action=d.get("action") or "",
            count=int(d.get("count") or 0),
            price=_cents(price) or 0,
            is_taker=bool(d.get("is_taker", False)),
            created_time=_parse_time(d.get("created_time")),
        )


@dataclass(frozen=True)
class Candle:
    start_ts: int
    end_ts: int
    open: int | None
    high: int | None
    low: int | None
    close: int | None
    volume: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Candle:
        # Candlesticks nest price fields under "price" in the live API; older
        # responses put open/high/low/close at the top level.
        price = d.get("price") if isinstance(d.get("price"), dict) else d
        return cls(
            start_ts=int(d.get("start_ts") or 0),
            end_ts=int(d.get("end_period_ts") or d.get("end_ts") or 0),
            open=_cents(price.get("open")),
            high=_cents(price.get("high")),
            low=_cents(price.get("low")),
            close=_cents(price.get("close")),
            volume=int(d.get("volume") or 0),
        )
