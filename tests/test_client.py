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
    assert client.get_balance().balance == 12345
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
    assert client.get_balance().balance == 1
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
    result = client.create_order("KXBTC15M-X", side="yes", action="buy", count=2, price=45)
    assert isinstance(result, DryRunOrder)
    assert result["yes_price"] == 45 and "no_price" not in result
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
                    "order": {
                        "order_id": "o1",
                        "ticker": "T",
                        "status": "resting",
                        "side": "no",
                        "action": "buy",
                        "type": "limit",
                        "no_price": 60,
                        "count": 3,
                        "remaining_count": 3,
                    }
                },
            )
        ],
        dry_run=False,
    )
    order = client.create_order(
        "T", side="no", action="buy", count=3, price=60, client_order_id="cid"
    )
    assert order.order_id == "o1" and order.no_price == 60
    sent = json.loads(rec.requests[0].content)
    assert sent == {
        "ticker": "T",
        "client_order_id": "cid",
        "side": "no",
        "action": "buy",
        "count": 3,
        "type": "limit",
        "no_price": 60,
    }
    assert rec.requests[0].headers[HEADER_KEY] == "test-key-id"


def test_prod_orders_blocked_without_allow_live(signer):
    client, rec = make_client(signer, [], base_url=PROD_BASE_URL, dry_run=False)
    with pytest.raises(LiveTradingBlocked):
        client.create_order("T", side="yes", action="buy", count=1, price=50)
    assert rec.requests == []
    # public reads on prod are still fine
    client2, _ = make_client(signer, [(200, {"trading_active": True})], base_url=PROD_BASE_URL)
    assert client2.exchange_status()["trading_active"] is True


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(side="maybe", action="buy", count=1, price=50),
        dict(side="yes", action="hold", count=1, price=50),
        dict(side="yes", action="buy", count=0, price=50),
        dict(side="yes", action="buy", count=1, price=0),
        dict(side="yes", action="buy", count=1, price=100),
        dict(side="yes", action="buy", count=1, price=None),
        dict(side="yes", action="buy", count=1, price=50, order_type="market"),
        dict(side="yes", action="buy", count=1.5, price=50),
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
            (200, {"order": {"order_id": "a", "status": "canceled"}}),
            (200, {"order": {"order_id": "b", "status": "canceled"}}),
        ],
        dry_run=False,
    )
    assert client.cancel_all_orders() == ["a", "b"]
    assert [r.method for r in rec.requests] == ["GET", "DELETE", "DELETE"]
    assert rec.requests[1].url.path == "/trade-api/v2/portfolio/orders/a"


def test_positions_accepts_both_keys(signer):
    client, _ = make_client(signer, [(200, {"market_positions": [{"ticker": "T", "position": 2}]})])
    assert client.get_positions()[0].position == 2
