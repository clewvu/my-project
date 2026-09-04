import json

import httpx
import pytest

from kalshi_bot.auth import HEADER_KEY, HEADER_SIGNATURE, HEADER_TIMESTAMP
from kalshi_bot.client import (
    DryRunOrder,
    KalshiAPIError,
    KalshiClient,
    LiveTradingBlocked,
)
from kalshi_bot.config import DEMO_BASE_URL, PROD_BASE_URL


class Recorder:
    """httpx mock transport that records requests and replays scripted responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if self.responses else (200, {})
        status, body = item
        return httpx.Response(status, json=body, request=request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def make_client(signer, responses, **kw):
    rec = Recorder(responses)
    kw.setdefault("base_url", DEMO_BASE_URL)
    kw.setdefault("min_request_interval", 0)
    client = KalshiClient(signer=signer, transport=rec.transport(), **kw)
    return client, rec


def test_public_endpoint_is_unsigned(signer):
    client, rec = make_client(signer, [(200, {"markets": [{"ticker": "A"}], "cursor": ""})])
    markets = client.get_markets(series_ticker="KXBTC15M", status="open")
    assert [m.ticker for m in markets] == ["A"]
    req = rec.requests[0]
    assert req.url.path == "/trade-api/v2/markets"
    assert req.url.params["series_ticker"] == "KXBTC15M"
    assert HEADER_KEY not in req.headers


def test_pagination_follows_cursor(signer):
    client, rec = make_client(
        signer,
        [
            (200, {"markets": [{"ticker": "A"}], "cursor": "c1"}),
            (200, {"markets": [{"ticker": "B"}], "cursor": ""}),
        ],
    )
    assert [m.ticker for m in client.get_markets()] == ["A", "B"]
    assert rec.requests[1].url.params["cursor"] == "c1"


def test_private_endpoint_signed_with_full_path(signer, rsa_key):
    client, rec = make_client(signer, [(200, {"balance": 12345})])
    assert client.get_balance().balance == 123.45
    req = rec.requests[0]
    assert req.headers[HEADER_KEY] == "test-key-id"
    ts = req.headers[HEADER_TIMESTAMP]
    # Recompute what should have been signed and verify with the public key.
    import base64

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    rsa_key.public_key().verify(
        base64.b64decode(req.headers[HEADER_SIGNATURE]),
        f"{ts}GET/trade-api/v2/portfolio/balance".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_private_endpoint_without_signer_errors():
    client = KalshiClient(
        DEMO_BASE_URL, None, transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    with pytest.raises(Exception, match="requires credentials"):
        client.get_balance()


def test_retries_on_429_then_succeeds(signer, monkeypatch):
    monkeypatch.setattr("kalshi_bot.client.time.sleep", lambda s: None)
    client, rec = make_client(signer, [(429, {"error": "rate"}), (200, {"balance": 1})])
    assert client.get_balance().balance == 0.01
    assert len(rec.requests) == 2


def test_gives_up_after_max_retries(signer, monkeypatch):
    monkeypatch.setattr("kalshi_bot.client.time.sleep", lambda s: None)
    client, rec = make_client(signer, [(503, {}), (503, {}), (503, {})], max_retries=3)
    with pytest.raises(KalshiAPIError) as exc:
        client.get_balance()
    assert exc.value.status == 503 and len(rec.requests) == 3


def test_4xx_is_not_retried(signer):
    client, rec = make_client(signer, [(400, {"error": {"message": "bad"}})])
    with pytest.raises(KalshiAPIError, match="HTTP 400"):
        client.get_market("NOPE")
    assert len(rec.requests) == 1


def test_dry_run_never_sends_orders(signer):
    client, rec = make_client(signer, [], dry_run=True)
    result = client.create_order("KXBTC15M-X", side="yes", action="buy", count=2, price=0.45)
    assert isinstance(result, DryRunOrder)
    assert result["side"] == "bid" and result["price"] == "0.4500" and result["count"] == "2"
    assert result["client_order_id"]
    assert client.cancel_order("abc") is None
    assert rec.requests == []


def test_live_order_body_and_response(signer):
    client, rec = make_client(
        signer,
        [
            (
                201,
                {
                    "order_id": "o1",
                    "client_order_id": "cid",
                    "fill_count": "3.00",
                    "remaining_count": "0.00",
                    "average_fill_price": "0.909000",
                    "average_fee_paid": "0.010000",
                    "ts_ms": 1700000000000,
                },
            )
        ],
        dry_run=False,
    )
    order = client.create_order(
        "T", side="no", action="buy", count=3, price=0.091, client_order_id="cid"
    )
    assert order.order_id == "o1" and order.no_price == 0.091 and order.side == "no"
    assert order.status == "executed" and order.count == 3 and order.remaining_count == 0
    sent = json.loads(rec.requests[0].content)
    # buying NO at 0.091 is an ask on the YES book at 0.909
    assert sent == {
        "ticker": "T",
        "client_order_id": "cid",
        "side": "ask",
        "count": "3",
        "price": "0.9090",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
    }
    assert rec.requests[0].url.path == "/trade-api/v2/portfolio/events/orders"
    assert rec.requests[0].headers[HEADER_KEY] == "test-key-id"


def test_resting_order_response(signer):
    client, rec = make_client(
        signer,
        [(201, {"order_id": "o2", "fill_count": "0.00", "remaining_count": "2.00"})],
        dry_run=False,
    )
    order = client.create_order("T", side="yes", action="buy", count=2, price=0.5, expiration_ts=99)
    assert order.status == "resting" and order.remaining_count == 2 and order.yes_price == 0.5
    sent = json.loads(rec.requests[0].content)
    assert sent["side"] == "bid" and sent["price"] == "0.5000" and sent["expiration_time"] == 99


@pytest.mark.parametrize(
    "side,action,price,book_side,yes_price",
    [
        ("yes", "buy", 0.55, "bid", 0.55),
        ("yes", "sell", 0.55, "ask", 0.55),
        ("no", "buy", 0.47, "ask", 0.53),
        ("no", "sell", 0.47, "bid", 0.53),
        ("no", "buy", 0.091, "ask", 0.909),
    ],
)
def test_book_side_mapping(side, action, price, book_side, yes_price):
    from kalshi_bot.client import book_side_and_price

    assert book_side_and_price(side, action, price) == (book_side, yes_price)


def test_prod_orders_blocked_without_allow_live(signer):
    client, rec = make_client(signer, [], base_url=PROD_BASE_URL, dry_run=False)
    with pytest.raises(LiveTradingBlocked):
        client.create_order("T", side="yes", action="buy", count=1, price=0.5)
    assert rec.requests == []
    # public reads on prod are still fine
    client2, _ = make_client(signer, [(200, {"trading_active": True})], base_url=PROD_BASE_URL)
    assert client2.exchange_status()["trading_active"] is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"side": "maybe", "action": "buy", "count": 1, "price": 0.5},
        {"side": "yes", "action": "hold", "count": 1, "price": 0.5},
        {"side": "yes", "action": "buy", "count": 0, "price": 0.5},
        {"side": "yes", "action": "buy", "count": 1, "price": 0.0},
        {"side": "yes", "action": "buy", "count": 1, "price": 1.0},
        {"side": "yes", "action": "buy", "count": 1, "price": 0.4505},  # off the 0.001 grid
        {"side": "yes", "action": "buy", "count": 1, "price": 45},  # cents, not dollars
        {"side": "yes", "action": "buy", "count": 1, "price": None},
        {"side": "yes", "action": "buy", "count": 1, "price": 0.5, "order_type": "market"},
        {"side": "yes", "action": "buy", "count": 1, "price": None, "order_type": "market"},
        {"side": "yes", "action": "buy", "count": 1, "price": 0.5, "time_in_force": "GTT"},
        {"side": "yes", "action": "buy", "count": 1.5, "price": 0.5},
        {"side": "yes", "action": "buy", "count": True, "price": 0.5},
    ],
)
def test_order_validation(signer, kwargs):
    client, rec = make_client(signer, [], dry_run=True)
    with pytest.raises(ValueError):
        client.create_order("T", **kwargs)
    assert rec.requests == []


def test_cancel_all(signer):
    client, rec = make_client(
        signer,
        [
            (
                200,
                {
                    "orders": [
                        {"order_id": "a", "status": "resting"},
                        {"order_id": "b", "status": "resting"},
                    ]
                },
            ),
            (200, {"order_id": "a", "client_order_id": "x", "reduced_by": "1.00", "ts_ms": 1}),
            (200, {"order_id": "b", "client_order_id": "y", "reduced_by": "1.00", "ts_ms": 2}),
        ],
        dry_run=False,
    )
    assert client.cancel_all_orders() == ["a", "b"]
    assert [r.method for r in rec.requests] == ["GET", "DELETE", "DELETE"]
    assert rec.requests[1].url.path == "/trade-api/v2/portfolio/events/orders/a"


def test_cancel_order_routes_by_ticker(signer):
    client, rec = make_client(
        signer, [(200, {"order_id": "a", "reduced_by": "2.00", "ts_ms": 1})], dry_run=False
    )
    order = client.cancel_order("a", ticker="KXBTC15M-X")
    assert order is not None and order.order_id == "a" and order.status == "canceled"
    assert rec.requests[0].url.path == "/trade-api/v2/portfolio/events/orders/a"
    assert rec.requests[0].url.params["market_ticker"] == "KXBTC15M-X"


def test_positions_accepts_both_keys(signer):
    client, _ = make_client(signer, [(200, {"market_positions": [{"ticker": "T", "position": 2}]})])
    assert client.get_positions()[0].position == 2


def test_validate_price_grid():
    from kalshi_bot.client import validate_price

    assert validate_price(0.091) == 0.091
    assert validate_price(0.1) == 0.1
    assert validate_price(0.999) == 0.999
    for bad in (0.0, 1.0, 0.0005, 0.4505, 45, -0.1):
        with pytest.raises(ValueError):
            validate_price(bad)


def test_get_trades_returns_trade_objects(signer):
    client, rec = make_client(
        signer,
        [
            (
                200,
                {
                    "trades": [
                        {
                            "trade_id": "t",
                            "ticker": "X",
                            "yes_price_dollars": "0.0910",
                            "count_fp": "2.5",
                        }
                    ]
                },
            )
        ],
    )
    trades = client.get_trades("X", min_ts=5)
    assert trades[0].yes_price == 0.091 and trades[0].count == 2.5
    assert rec.requests[0].url.params["min_ts"] == "5"


def test_get_trades_follows_cursor_on_full_pages(signer):
    full = [{"trade_id": str(i), "ticker": "X"} for i in range(2)]
    client, rec = make_client(
        signer,
        [
            (200, {"trades": full, "cursor": "c1"}),
            (200, {"trades": [{"trade_id": "9", "ticker": "X"}], "cursor": "c2"}),  # short page
        ],
    )
    trades = client.get_trades("X", limit=2)
    assert [t.trade_id for t in trades] == ["0", "1", "9"]
    assert len(rec.requests) == 2 and rec.requests[1].url.params["cursor"] == "c1"
