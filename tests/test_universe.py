from kalshi_bot.universe import (
    COINBASE_REST,
    COINBASE_WS,
    PYTH,
    SWISSQUOTE,
    default_universe,
    load_universe,
)


def test_defaults_cover_crypto_and_metals():
    uni = default_universe()
    assert {"KXBTC15M", "KXETH15M", "KXSOL15M", "KXGOLD15M", "KXSILVER15M"} <= set(uni.series)
    assert {"KXPLATINUM15M", "KXPALLADIUM15M", "KXWTI15M", "KXCOPPER15M"} <= set(uni.series)
    assert "BTC-USD" in uni.coinbase_symbols and "Metal.Index.SILVER/USD" in uni.pyth_symbols
    assert uni.proxy_symbols == ["XAG/USD", "XAU/USD", "XPD/USD", "XPT/USD"]
    assert uni.by_name("GOLD").series == "KXGOLD15M"


def test_spot_preference_order_by_asset_class():
    uni = default_universe()
    assert [s for s, _ in uni.spot_preference("KXBTC15M")] == [COINBASE_WS, PYTH, COINBASE_REST]
    assert uni.spot_preference("KXGOLD15M") == [
        (PYTH, "Metal.Index.1OZGOLD/USD"),
        (SWISSQUOTE, "XAU/USD"),
    ]
    assert uni.spot_preference("KXWTI15M") == [(PYTH, "Commodities.Index.PYTHOIL/USD")]
    assert uni.spot_preference("NOPE") == []


def test_yaml_override_merges(tmp_path):
    cfg = tmp_path / "universe.yaml"
    cfg.write_text(
        """
assets:
  - series: KXWTI15M
    proxy_symbol: "XTI/USD"
    pyth_symbol: "Commodities.WTI1M"
  - series: KXXRP15M
    enabled: false
  - series: KXNEW15M
    name: NEW
    asset_class: metal
    pyth_symbol: "Metal.XPT/USD"
hypotheses:
  - {name: only_one, leader: KXWTI15M, follower: KXGOLD15M, sign: -1}
"""
    )
    uni = load_universe(cfg)
    wti = uni.by_series("KXWTI15M")
    assert wti.enabled and wti.pyth_symbol == "Commodities.WTI1M" and wti.name == "WTI"
    assert wti.proxy_symbol == "XTI/USD"
    assert uni.by_series("KXXRP15M").enabled is False
    assert uni.by_series("KXNEW15M").asset_class == "metal"
    assert "KXBTC15M" in uni.series  # untouched default survives
    assert [h.name for h in uni.hypotheses] == ["only_one"]
    assert uni.hypotheses[0].sign == -1


def test_missing_file_gives_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("KALSHI_UNIVERSE", str(tmp_path / "nope.yaml"))
    assert load_universe().series == default_universe().series
