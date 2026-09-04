import pytest

from kalshi_bot.config import DEMO_BASE_URL, PROD_BASE_URL, Settings


def test_defaults_are_safe(monkeypatch):
    for k in ("KALSHI_ENV", "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH", "KALSHI_DRY_RUN"):
        monkeypatch.delenv(k, raising=False)
    s = Settings.from_env(dotenv_path="/nonexistent/.env")
    assert s.env == "demo"
    assert s.base_url == DEMO_BASE_URL
    assert s.dry_run is True
    assert s.has_credentials is False


def test_prod_and_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("KALSHI_ENV", "PROD")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(tmp_path / "k.pem"))
    monkeypatch.setenv("KALSHI_DRY_RUN", "false")
    s = Settings.from_env(dotenv_path="/nonexistent/.env")
    assert s.is_prod and s.base_url == PROD_BASE_URL
    assert s.dry_run is False
    assert s.has_credentials


def test_bad_env_rejected(monkeypatch):
    monkeypatch.setenv("KALSHI_ENV", "staging")
    with pytest.raises(ValueError):
        Settings.from_env(dotenv_path="/nonexistent/.env")
