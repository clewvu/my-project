"""Settings for the sports package, read from the environment or .env.

Kalshi credentials and host selection stay in ``kalshi_bot.config.Settings``;
this adds the external feeds.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from .leagues import DEFAULT_LEAGUES


@dataclass(frozen=True)
class SportsSettings:
    odds_api_key: str
    odds_regions: tuple[str, ...]
    odds_interval: float  # seconds between odds polls per league
    scores_interval_live: float
    scores_interval_idle: float
    leagues: tuple[str, ...]

    @property
    def has_odds(self) -> bool:
        return bool(self.odds_api_key)

    @classmethod
    def from_env(cls, dotenv_path: str | None = None) -> SportsSettings:
        load_dotenv(dotenv_path=dotenv_path, override=False)
        regions = os.getenv("ODDS_API_REGIONS", "us,eu").strip() or "us,eu"
        leagues = os.getenv("SPORTS_LEAGUES", ",".join(DEFAULT_LEAGUES)).strip()
        return cls(
            odds_api_key=os.getenv("ODDS_API_KEY", "").strip(),
            odds_regions=tuple(r.strip() for r in regions.split(",") if r.strip()),
            odds_interval=float(os.getenv("ODDS_INTERVAL", "3600") or 3600),
            scores_interval_live=float(os.getenv("SCORES_INTERVAL_LIVE", "20") or 20),
            scores_interval_idle=float(os.getenv("SCORES_INTERVAL_IDLE", "300") or 300),
            leagues=tuple(k.strip().lower() for k in leagues.split(",") if k.strip()),
        )
