from kalshi_bot.cli import build_parser, cmd_check
from kalshi_bot.config import Settings


def test_parser_has_commands():
    p = build_parser()
    for cmd in [
        "check",
        "status",
        "markets",
        "orderbook",
        "candles",
        "cancel-all",
        "analyze",
        "whale",
        "fairvalue",
    ]:
        args = p.parse_args([cmd] + (["X"] if cmd in ("orderbook", "candles") else []))
        assert args.command == cmd


def test_check_reports_missing_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("KALSHI_ENV", "demo")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(tmp_path / "missing.pem"))
    settings = Settings.from_env(dotenv_path="/nonexistent/.env")
    assert cmd_check(settings, None) == 1
    assert "file not found" in capsys.readouterr().out


def test_env_override_and_record_default(monkeypatch):
    import kalshi_bot.cli as cli

    seen = {}

    def fake_check(settings, args):
        seen["env"] = settings.env
        return 0

    monkeypatch.setattr(cli, "cmd_check", fake_check)
    monkeypatch.delenv("KALSHI_ENV", raising=False)
    parser = cli.build_parser()
    assert parser.parse_args(["record", "--ticks", "1"]).default_env == "prod"
    assert parser.parse_args(["--env", "prod", "markets"]).env == "prod"
    assert parser.parse_args(["markets"]).env is None
    # main() applies --env before dispatch
    parser = cli.build_parser()
    cli.main(["--env-file", "/nonexistent", "--env", "prod", "check"])
    assert seen["env"] == "prod"


def test_markets_raw_prints_json(monkeypatch, capsys):
    import kalshi_bot.cli as cli
    from kalshi_bot.models import Market

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get_markets(self, **kw):
            return [
                Market.from_dict({"ticker": "T", "yes_bid_dollars": "0.21", "volume_fp": "12.0"})
            ]

    monkeypatch.setattr(cli, "_client", lambda settings, need_auth: FakeClient())
    settings = cli.Settings.from_env("/nonexistent")
    args = cli.build_parser().parse_args(["markets", "--raw", "--limit", "1"])
    assert cli.cmd_markets(settings, args) == 0
    out = capsys.readouterr().out
    assert '"yes_bid_dollars": "0.21"' in out and "ticker" in out
    args = cli.build_parser().parse_args(["markets"])
    cli.cmd_markets(settings, args)
    assert "21.0c" in capsys.readouterr().out


def test_setup_writes_env_from_messy_key_file(rsa_key, tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization

    import kalshi_bot.cli as cli

    pem = rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_file = tmp_path / "Ham Recipe.txt"
    key_file.write_bytes(b"key id 12345678-abcd-4ef0-9876-0123456789ab\n" + pem)
    env_out = tmp_path / ".env"
    args = cli.build_parser().parse_args(
        ["setup", "--key-file", str(key_file), "--live", "--env-out", str(env_out)]
    )
    assert cli.cmd_setup(None, args) == 0
    text = env_out.read_text()
    assert "KALSHI_ENV=prod" in text and "KALSHI_DRY_RUN=false" in text
    assert "KALSHI_API_KEY_ID=12345678-abcd-4ef0-9876-0123456789ab" in text
    # the written .env round-trips through Settings, spaces in the path included
    for var in ("KALSHI_ENV", "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH", "KALSHI_DRY_RUN"):
        monkeypatch.delenv(var, raising=False)
    settings = cli.Settings.from_env(env_out)
    assert settings.env == "prod" and settings.dry_run is False
    assert settings.private_key_path == key_file
    assert cli.cmd_check(settings, None) == 0
    # re-running backs up the previous file
    assert cli.cmd_setup(None, args) == 0 and (tmp_path / ".env.bak").exists()


def test_shard_plan():
    from kalshi_bot.cli import shard_plan
    from kalshi_bot.models import Balance

    bal = Balance(balance=49.72, breakdown={0: 49.72, 1: 0.0})
    assert shard_plan(bal, {"KXBTC15M": 1, "KXDOGE15M": 1}, needed=45.0) == (45.0, 0, 1)
    assert shard_plan(bal, {"KXBTC15M": 1}, needed=60.0) == (49.72, 0, 1)
    assert shard_plan(bal, {"KXBTC15M": 0}, needed=45.0) is None  # already funded
    assert shard_plan(bal, {"KXBTC15M": 1, "KXDOGE15M": 2}, needed=45.0) is None
    assert shard_plan(bal, {}, needed=45.0) is None
    assert shard_plan(Balance(balance=49.72), {"KXBTC15M": 1}, needed=45.0) is None
    partial = Balance(balance=50.0, breakdown={0: 10.0, 1: 40.0})
    assert shard_plan(partial, {"KXBTC15M": 1}, needed=45.0) == (5.0, 0, 1)


def test_market_shards_uses_first_open_market():
    from kalshi_bot.cli import market_shards
    from kalshi_bot.models import Market

    class C:
        def get_markets(self, *, series_ticker, status, max_pages):
            if series_ticker == "KXBTC15M":
                return [Market.from_dict({"ticker": "KXBTC15M-1", "exchange_index": 1})]
            return [Market.from_dict({"ticker": "KXDOGE15M-1"})]

    assert market_shards(C(), ("KXBTC15M", "KXDOGE15M")) == {"KXBTC15M": 1}
