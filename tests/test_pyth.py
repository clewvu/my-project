import json

import httpx

from kalshi_bot.pyth import PythClient, PythStream, parse_feeds, parse_prices
from kalshi_bot.storage import MarketDataStore

FEEDS = [
    {
        "id": "0x765d2b",
        "attributes": {
            "symbol": "Metal.XAU/USD",
            "asset_type": "Metal",
            "description": "GOLD / US DOLLAR",
        },
    },
    {"id": "f2fb02", "attributes": {"symbol": "Metal.XAG/USD", "asset_type": "Metal"}},
    {"id": "junk"},
]
LATEST = {
    "parsed": [
        {"id": "765d2b", "price": {"price": "400012345", "expo": -5, "publish_time": 1700000000}},
        {"id": "f2fb02", "price": {"price": "4812345", "expo": -5, "publish_time": 1700000001}},
        {"id": "unknown", "price": {"price": "1", "expo": 0}},
    ]
}


def test_parse_feeds_normalises_ids():
    feeds = parse_feeds(FEEDS)
    assert [f.symbol for f in feeds] == ["Metal.XAU/USD", "Metal.XAG/USD"]
    assert feeds[0].id == "765d2b" and feeds[0].description == "GOLD / US DOLLAR"


def test_parse_prices_scales_by_expo():
    ticks = parse_prices(LATEST, {"765d2b": "Metal.XAU/USD", "f2fb02": "Metal.XAG/USD"}, 5.0)
    assert [(t.symbol, round(t.price, 5)) for t in ticks] == [
        ("Metal.XAU/USD", 4000.12345),
        ("Metal.XAG/USD", 48.12345),
    ]
    assert ticks[0].exchange_ts == 1700000000.0 and ticks[0].local_ts == 5.0


def _transport(stream_lines=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/price_feeds":
            q = request.url.params["query"]
            return httpx.Response(200, json=[f for f in FEEDS if q in json.dumps(f)])
        if request.url.path == "/v2/updates/price/latest":
            assert request.url.params.get_list("ids[]")
            return httpx.Response(200, json=LATEST)
        if request.url.path == "/v2/updates/price/stream":
            body = "".join(stream_lines or [])
            return httpx.Response(
                200, content=body.encode(), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_resolve_and_latest():
    client = PythClient(transport=_transport())
    feeds = client.resolve(["Metal.XAU/USD", "Metal.XAG/USD", "Metal.NOPE/USD"])
    assert set(feeds) == {"Metal.XAU/USD", "Metal.XAG/USD"}
    ticks = client.latest(feeds)
    assert {t.symbol for t in ticks} == {"Metal.XAU/USD", "Metal.XAG/USD"}
    assert client.latest({}) == []


def test_stream_consumes_sse_and_flushes():
    store = MarketDataStore()
    lines = [
        ": keepalive\n",
        "data: " + json.dumps(LATEST) + "\n\n",
        "data: not json\n\n",
        "data: "
        + json.dumps(
            {
                "parsed": [
                    {
                        "id": "765d2b",
                        "price": {"price": "400100000", "expo": -5, "publish_time": 1700000002},
                    }
                ]
            }
        )
        + "\n\n",
    ]
    ps = PythStream(
        store, ["Metal.XAU/USD", "Metal.XAG/USD"], min_interval=0, transport=_transport(lines)
    )
    ps.feeds = ps.client.resolve(ps.symbols)
    ps._consume()
    assert ps.messages == 3
    assert ps.flush() == 3 and ps.written == 3
    rows = store._conn.execute(
        "SELECT source, symbol, price FROM spot ORDER BY ts, symbol"
    ).fetchall()
    assert rows[0]["source"] == "pyth"
    assert {r["symbol"] for r in rows} == {"Metal.XAU/USD", "Metal.XAG/USD"}
    assert round(ps.last_tick("Metal.XAU/USD").price, 2) == 4001.0
    assert store.stats()["spot_by_source"] == {"pyth": 3}
