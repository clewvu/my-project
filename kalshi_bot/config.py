"""Settings loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
PROD_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

BASE_URLS = {"demo": DEMO_BASE_URL, "prod": PROD_BASE_URL}


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    env: str
    api_key_id: str
    private_key_path: Path | None
    dry_run: bool
    min_request_interval: float
    log_level: str

    @property
    def base_url(self) -> str:
        return BASE_URLS[self.env]

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key_id) and self.private_key_path is not None

    @classmethod
    def from_env(cls, dotenv_path: str | Path | None = None) -> Settings:
        # A real environment variable always wins over the .env file.
        load_dotenv(dotenv_path=dotenv_path, override=False)

        env = os.getenv("KALSHI_ENV", "demo").strip().lower()
        if env not in BASE_URLS:
            raise ValueError(f"KALSHI_ENV must be one of {sorted(BASE_URLS)}, got {env!r}")

        key_path_raw = os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip()
        key_path = Path(key_path_raw).expanduser() if key_path_raw else None

        interval_raw = os.getenv("KALSHI_MIN_REQUEST_INTERVAL", "0.15").strip() or "0.15"
        interval = float(interval_raw)
        if interval < 0:
            raise ValueError("KALSHI_MIN_REQUEST_INTERVAL must be >= 0")

        return cls(
            env=env,
            api_key_id=os.getenv("KALSHI_API_KEY_ID", "").strip(),
            private_key_path=key_path,
            dry_run=_truthy(os.getenv("KALSHI_DRY_RUN"), default=True),
            min_request_interval=interval,
            log_level=os.getenv("KALSHI_LOG_LEVEL", "INFO").strip().upper() or "INFO",
        )
