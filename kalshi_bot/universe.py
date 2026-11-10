"""The set of 15-minute series the bot follows, and where each one's spot comes from.

Two asset classes:

* **crypto** settles on the CF Benchmarks real-time index (one-minute average
  before close vs. the same average before open). Spot proxies: Coinbase
  WebSocket ticks (sub-second) and Pyth.
* **commodities** (metals, energy) settle on Pyth Network prices. Pyth is therefore
  the *exact* reference, not a proxy, and is the preferred spot for them. Pyth's
  Hermes API needs a key (``PYTH_API_KEY``) since 2026; without one the four
  precious metals fall back to free Swissquote spot quotes, rebased onto the
  Pyth level once per window (see ``commodities.py``). Copper, oil and natural
  gas have no free live feed and stay untraded until a key is set.

Correlation hypotheses the research tests are declared here too, so the
cross-asset analysis is pre-registered rather than fished for.

Override the defaults with a YAML file (``universe.yaml`` in the working
directory, or ``KALSHI_UNIVERSE`` env var); see ``universe.example.yaml``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PYTH = "pyth"
COINBASE_WS = "coinbase_ws"
COINBASE_REST = "coinbase"
SWISSQUOTE = "swissquote_rebased"  # Swissquote spot scaled onto the Pyth level per window


@dataclass(frozen=True)
class Asset:
    series: str  # Kalshi series ticker, e.g. KXBTC15M
    name: str  # short label, e.g. BTC
    asset_class: str  # crypto | metal | energy
    pyth_symbol: str | None = None  # e.g. Crypto.BTC/USD, Metal.XAU/USD
    coinbase_symbol: str | None = None  # e.g. BTC-USD
    enabled: bool = True
    proxy_symbol: str | None = None  # Swissquote pair, e.g. XAU/USD (keyless fallback)

    @property
    def spot_preference(self) -> list[tuple[str, str]]:
        """(source, symbol) pairs, best first, for attaching spot to this series."""
        prefs: list[tuple[str, str]] = []
        if self.asset_class == "crypto":
            if self.coinbase_symbol:
                prefs.append((COINBASE_WS, self.coinbase_symbol))
            if self.pyth_symbol:
                prefs.append((PYTH, self.pyth_symbol))
            if self.coinbase_symbol:
                prefs.append((COINBASE_REST, self.coinbase_symbol))
        else:
            if self.pyth_symbol:
                prefs.append((PYTH, self.pyth_symbol))
            if self.proxy_symbol:
                prefs.append((SWISSQUOTE, self.proxy_symbol))
            if self.coinbase_symbol:
                prefs.append((COINBASE_WS, self.coinbase_symbol))
                prefs.append((COINBASE_REST, self.coinbase_symbol))
        return prefs


@dataclass(frozen=True)
class Hypothesis:
    """A directional co-movement claim between two series, to be tested not assumed."""

    name: str
    leader: str  # series expected to move first / be informative
    follower: str  # series whose outcome we try to predict
    sign: int  # +1 same direction, -1 opposite


@dataclass
class Universe:
    assets: list[Asset]
    hypotheses: list[Hypothesis] = field(default_factory=list)

    @property
    def enabled(self) -> list[Asset]:
        return [a for a in self.assets if a.enabled]

    @property
    def series(self) -> list[str]:
        return [a.series for a in self.enabled]

    def by_series(self, series: str) -> Asset | None:
        return next((a for a in self.assets if a.series == series), None)

    def by_name(self, name: str) -> Asset | None:
        return next((a for a in self.assets if a.name == name), None)

    @property
    def coinbase_symbols(self) -> list[str]:
        return sorted({a.coinbase_symbol for a in self.enabled if a.coinbase_symbol})

    @property
    def pyth_symbols(self) -> list[str]:
        return sorted({a.pyth_symbol for a in self.enabled if a.pyth_symbol})

    @property
    def proxy_symbols(self) -> list[str]:
        return sorted({a.proxy_symbol for a in self.enabled if a.proxy_symbol})

    def spot_preference(self, series: str) -> list[tuple[str, str]]:
        a = self.by_series(series)
        return a.spot_preference if a else []


DEFAULT_ASSETS = [
    # crypto: Coinbase gives sub-second ticks; Pyth is a cross-check
    Asset("KXBTC15M", "BTC", "crypto", "Crypto.BTC/USD", "BTC-USD"),
    Asset("KXETH15M", "ETH", "crypto", "Crypto.ETH/USD", "ETH-USD"),
    Asset("KXSOL15M", "SOL", "crypto", "Crypto.SOL/USD", "SOL-USD"),
    Asset("KXDOGE15M", "DOGE", "crypto", "Crypto.DOGE/USD", "DOGE-USD"),
    Asset("KXXRP15M", "XRP", "crypto", "Crypto.XRP/USD", "XRP-USD"),
    # metals and energy: Pyth is the settlement source itself. The symbols are the
    # feeds named in each Kalshi series' settlement_sources (checked 2026-11-10).
    Asset("KXGOLD15M", "GOLD", "metal", "Metal.Index.1OZGOLD/USD", proxy_symbol="XAU/USD"),
    Asset("KXSILVER15M", "SILVER", "metal", "Metal.Index.SILVER/USD", proxy_symbol="XAG/USD"),
    Asset("KXPLATINUM15M", "PLATINUM", "metal", "Metal.XPT/USD", proxy_symbol="XPT/USD"),
    Asset("KXPALLADIUM15M", "PALLADIUM", "metal", "Metal.XPD/USD", proxy_symbol="XPD/USD"),
    # No free live feed: these are only priced (and traded) when PYTH_API_KEY is set.
    Asset("KXCOPPER15M", "COPPER", "metal", "Commodities.Index.CU/USD"),
    Asset("KXWTI15M", "WTI", "energy", "Commodities.Index.PYTHOIL/USD"),
    Asset("KXBRENT15M", "BRENT", "energy", "Commodities.Index.BRENT/USD"),
    Asset("KXNATGAS15M", "NATGAS", "energy", "Commodities.Index.NATGAS/USD"),
]

DEFAULT_HYPOTHESES = [
    Hypothesis("eth_leads_btc", "KXETH15M", "KXBTC15M", +1),
    Hypothesis("eth_leads_sol", "KXETH15M", "KXSOL15M", +1),
    Hypothesis("btc_leads_eth", "KXBTC15M", "KXETH15M", +1),
    Hypothesis("btc_leads_sol", "KXBTC15M", "KXSOL15M", +1),
    Hypothesis("gold_leads_silver", "KXGOLD15M", "KXSILVER15M", +1),
    Hypothesis("silver_leads_gold", "KXSILVER15M", "KXGOLD15M", +1),
    Hypothesis("brent_vs_gold", "KXBRENT15M", "KXGOLD15M", -1),
    Hypothesis("brent_vs_silver", "KXBRENT15M", "KXSILVER15M", -1),
    Hypothesis("wti_vs_gold", "KXWTI15M", "KXGOLD15M", -1),
]


def default_universe() -> Universe:
    return Universe(list(DEFAULT_ASSETS), list(DEFAULT_HYPOTHESES))


def load_universe(path: str | Path | None = None) -> Universe:
    """Defaults, overlaid with a YAML file if one is given or found."""
    candidate = path or os.getenv("KALSHI_UNIVERSE") or "universe.yaml"
    p = Path(candidate).expanduser()
    uni = default_universe()
    if not p.exists():
        return uni
    data = yaml.safe_load(p.read_text()) or {}
    if "assets" in data:
        assets: dict[str, Asset] = {a.series: a for a in uni.assets}
        for row in data["assets"] or []:
            series = row["series"]
            base = assets.get(series)
            merged = {
                "series": series,
                "name": row.get("name", base.name if base else series),
                "asset_class": row.get("asset_class", base.asset_class if base else "crypto"),
                "pyth_symbol": row.get("pyth_symbol", base.pyth_symbol if base else None),
                "coinbase_symbol": row.get(
                    "coinbase_symbol", base.coinbase_symbol if base else None
                ),
                "enabled": bool(row.get("enabled", base.enabled if base else True)),
                "proxy_symbol": row.get("proxy_symbol", base.proxy_symbol if base else None),
            }
            assets[series] = Asset(**merged)
        uni.assets = list(assets.values())
    if "hypotheses" in data:
        uni.hypotheses = [
            Hypothesis(h["name"], h["leader"], h["follower"], int(h.get("sign", 1)))
            for h in data["hypotheses"] or []
        ]
    return uni
