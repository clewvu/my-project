"""Typed views over Kalshi API JSON.

Units
-----
Prices are **dollars per contract** as floats with 0.001 resolution
(0.001 .. 0.999). Kalshi's 15-minute markets use a "tapered deci-cent" price
structure: tenth-of-a-cent steps below $0.10 and above $0.90, whole cents in
between, so integer cents would lose precision exactly where these markets
spend their final minutes. Contract counts are floats because the API reports
fractional counts (``count_fp``).

The API sends most numbers as fixed-point strings (``*_dollars``, ``*_fp``).
Older responses used integer cents and integer counts; both are accepted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

PRICE_DECIMALS = 4
TICK = 0.001


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def dollars(value: Any) -> float | None:
    """Price in dollars from a dollars string ("0.0910") or a legacy cents number (9)."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return round(float(value), PRICE_DECIMALS)
    return round(float(value) / 100, PRICE_DECIMALS)


def _price(d: dict[str, Any], name: str) -> float | None:
    """Read ``name_dollars`` (string) first, then legacy ``name`` (integer cents)."""
    value = d.get(f"{name}_dollars")
    if value is None or value == "":
        value = d.get(name)
    return dollars(value)


def _num(d: dict[str, Any], name: str, default: float | None = None) -> float | None:
    """Read a count-like number from ``name_fp`` (string) or ``name`` (number)."""
    for key in (f"{name}_fp", name):
        value = d.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return default


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _money(d: dict[str, Any], name: str) -> float | None:
    """Dollar amount from ``name_dollars`` (string) or legacy ``name`` (integer cents)."""
    return _price(d, name)


@dataclass(frozen=True)
class Market:
    ticker: str
    event_ticker: str | None
    series_ticker: str
    title: str
    status: str
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    yes_bid_size: float | None
    yes_ask_size: float | None
    last_price: float | None
    volume: float
    open_interest: float | None
    strike: float | None  # reference price the market settles against
    strike_type: str | None  # e.g. greater_or_equal
    expiration_value: float | None  # settlement index value, set once settled
    open_time: datetime | None
    close_time: datetime | None
    expiration_time: datetime | None
    result: str | None
    raw: dict[str, Any] = field(repr=False, compare=False, default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Market:
        ticker = d["ticker"]
        strike = d.get("floor_strike")
        if strike is None:
            strike = d.get("cap_strike")
        return cls(
            ticker=ticker,
            event_ticker=d.get("event_ticker"),
            series_ticker=d.get("series_ticker") or ticker.split("-", 1)[0],
            title=d.get("title") or "",
            status=d.get("status") or "",
            yes_bid=_price(d, "yes_bid"),
            yes_ask=_price(d, "yes_ask"),
            no_bid=_price(d, "no_bid"),
            no_ask=_price(d, "no_ask"),
            yes_bid_size=_num(d, "yes_bid_size"),
            yes_ask_size=_num(d, "yes_ask_size"),
            last_price=_price(d, "last_price"),
            volume=_num(d, "volume", 0.0) or 0.0,
            open_interest=_num(d, "open_interest"),
            strike=float(strike) if strike not in (None, "") else None,
            strike_type=d.get("strike_type") or None,
            expiration_value=_float_or_none(d.get("expiration_value")),
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
        return round((self.yes_bid + self.yes_ask) / 2, PRICE_DECIMALS)

    @property
    def spread(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return round(self.yes_ask - self.yes_bid, PRICE_DECIMALS)

    def seconds_to_close(self, now: datetime | None = None) -> float | None:
        if self.close_time is None:
            return None
        now = now or datetime.now(UTC)
        return (self.close_time - now).total_seconds()


@dataclass(frozen=True)
class Level:
    price: float  # dollars
    count: float  # contracts resting at this price


@dataclass(frozen=True)
class Orderbook:
    """Resting bids on each side.

    ``yes`` are bids to buy YES at ``price``; ``no`` are bids to buy NO.
    A YES bid at p is equivalent to a NO ask at 1-p, and vice versa.
    Levels are sorted best-first (highest bid first).
    """

    ticker: str
    yes: list[Level]
    no: list[Level]
    raw: dict[str, Any] = field(repr=False, compare=False, default_factory=dict)

    _BOOK_KEYS = ("orderbook_fp", "orderbook")
    _YES_KEYS = ("yes_dollars", "yes_fp", "yes", "true")
    _NO_KEYS = ("no_dollars", "no_fp", "no", "false")

    @classmethod
    def from_dict(cls, ticker: str, d: dict[str, Any]) -> Orderbook:
        book: Any = d
        for key in cls._BOOK_KEYS:
            if isinstance(d.get(key), dict):
                book = d[key]
                break
        book = book or {}
        return cls(
            ticker=ticker,
            yes=cls._levels(cls._first(book, cls._YES_KEYS)),
            no=cls._levels(cls._first(book, cls._NO_KEYS)),
            raw=d,
        )

    @staticmethod
    def _first(book: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            if book.get(key) is not None:
                return book[key]
        return None

    @staticmethod
    def _levels(raw: Any) -> list[Level]:
        levels: list[Level] = []
        for item in raw or []:
            if isinstance(item, dict):
                price = item.get("price_dollars", item.get("price"))
                count = item.get("count_fp", item.get("count"))
            else:
                price, count = item[0], item[1]
            p = dollars(price)
            if p is None or count in (None, ""):
                continue
            levels.append(Level(price=p, count=float(count)))
        levels.sort(key=lambda lv: lv.price, reverse=True)
        return levels

    @property
    def is_empty(self) -> bool:
        return not self.yes and not self.no

    @property
    def best_yes_bid(self) -> float | None:
        return self.yes[0].price if self.yes else None

    @property
    def best_no_bid(self) -> float | None:
        return self.no[0].price if self.no else None

    @property
    def best_yes_ask(self) -> float | None:
        return round(1 - self.best_no_bid, PRICE_DECIMALS) if self.no else None

    @property
    def best_no_ask(self) -> float | None:
        return round(1 - self.best_yes_bid, PRICE_DECIMALS) if self.yes else None

    @property
    def yes_mid(self) -> float | None:
        if self.best_yes_bid is None or self.best_yes_ask is None:
            return None
        return round((self.best_yes_bid + self.best_yes_ask) / 2, PRICE_DECIMALS)

    def depth(self, side: str, max_levels: int | None = None) -> float:
        levels = self.yes if side == "yes" else self.no
        if max_levels is not None:
            levels = levels[:max_levels]
        return sum(lv.count for lv in levels)


@dataclass(frozen=True)
class Balance:
    balance: float  # dollars available
    portfolio_value: float | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Balance:
        return cls(
            balance=_money(d, "balance") or 0.0,
            portfolio_value=_money(d, "portfolio_value"),
        )


@dataclass(frozen=True)
class Position:
    ticker: str
    event_ticker: str | None
    position: float  # >0 long YES, <0 long NO (contracts)
    total_cost: float  # dollars
    realized_pnl: float  # dollars
    fees_paid: float  # dollars
    resting_order_count: float
    market_result: str | None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Position:
        total_cost = _money(d, "total_traded")
        if total_cost is None:
            total_cost = _money(d, "total_cost")
        return cls(
            ticker=d["ticker"],
            event_ticker=d.get("event_ticker"),
            position=_num(d, "position", 0.0) or 0.0,
            total_cost=total_cost or 0.0,
            realized_pnl=_money(d, "realized_pnl") or 0.0,
            fees_paid=_money(d, "fees_paid") or 0.0,
            resting_order_count=_num(d, "resting_order_count", 0.0) or 0.0,
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
    yes_price: float | None
    no_price: float | None
    count: float
    remaining_count: float
    created_time: datetime | None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Order:
        count = _num(d, "count")
        if count is None:
            count = _num(d, "initial_count", 0.0)
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
            count=count or 0.0,
            remaining_count=_num(d, "remaining_count", 0.0) or 0.0,
            created_time=_parse_time(d.get("created_time")),
        )

    @property
    def price(self) -> float | None:
        return self.yes_price if self.side == "yes" else self.no_price


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    ticker: str
    side: str
    action: str
    count: float
    price: float  # dollars, on the fill's side
    is_taker: bool
    created_time: datetime | None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Fill:
        side = d.get("side") or ""
        price = _price(d, "price")
        if price is None:
            price = _price(d, "yes_price") if side == "yes" else _price(d, "no_price")
        return cls(
            fill_id=d.get("fill_id") or d.get("trade_id") or "",
            order_id=d.get("order_id") or "",
            ticker=d.get("ticker") or "",
            side=side,
            action=d.get("action") or "",
            count=_num(d, "count", 0.0) or 0.0,
            price=price or 0.0,
            is_taker=bool(d.get("is_taker", False)),
            created_time=_parse_time(d.get("created_time")),
        )


@dataclass(frozen=True)
class Trade:
    """A public trade print."""

    trade_id: str
    ticker: str
    yes_price: float | None
    no_price: float | None
    count: float
    taker_side: str | None
    created_time: datetime | None
    raw: dict[str, Any] = field(repr=False, compare=False, default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Trade:
        return cls(
            trade_id=str(d.get("trade_id") or d.get("id") or ""),
            ticker=d.get("ticker") or "",
            yes_price=_price(d, "yes_price"),
            no_price=_price(d, "no_price"),
            count=_num(d, "count", 0.0) or 0.0,
            taker_side=d.get("taker_side") or d.get("taker_outcome_side") or None,
            created_time=_parse_time(d.get("created_time")),
            raw=d,
        )


@dataclass(frozen=True)
class Candle:
    start_ts: int
    end_ts: int
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Candle:
        # Candlesticks nest price fields under "price" in the live API; older
        # responses put open/high/low/close at the top level.
        price = d.get("price") if isinstance(d.get("price"), dict) else d
        return cls(
            start_ts=int(d.get("start_ts") or 0),
            end_ts=int(d.get("end_period_ts") or d.get("end_ts") or 0),
            open=_price(price, "open"),
            high=_price(price, "high"),
            low=_price(price, "low"),
            close=_price(price, "close"),
            volume=_num(d, "volume", 0.0) or 0.0,
        )
