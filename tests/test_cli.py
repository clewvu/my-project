from kalshi_bot.cli import build_parser, cmd_check
from kalshi_bot.config import Settings


def test_parser_has_commands():
    p = build_parser()
    for cmd in ["check", "status", "markets", "orderbook", "candles", "cancel-all"]:
        args = p.parse_args([cmd] + (["X"] if cmd in ("orderbook", "candles") else []))
        assert args.command == cmd


def test_check_reports_missing_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("KALSHI_ENV", "demo")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(tmp_path / "missing.pem"))
    settings = Settings.from_env(dotenv_path="/nonexistent/.env")
    assert cmd_check(settings, None) == 1
    assert "file not found" in capsys.readouterr().out
