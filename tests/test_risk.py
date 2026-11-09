from kalshi_bot.risk import OrderIntent, RiskEngine, RiskLimits
from kalshi_bot.storage import MarketDataStore


def intent(**kw):
    base = {
        "ticker": "T1",
        "series": "S",
        "side": "yes",
        "count": 3,
        "price": 0.5,
        "secs_to_close": 600.0,
        "spot_age": 1.0,
        "book_age": 1.0,
    }
    base.update(kw)
    return OrderIntent(**base)


def engine(tmp_path, **lim):
    store = MarketDataStore(tmp_path / "r.sqlite")
    return RiskEngine(store, RiskLimits(**lim), "paper"), store


def test_basic_allow_and_sizing(tmp_path):
    eng, _ = engine(tmp_path, bankroll=100, max_bankroll_fraction=0.1, max_contracts_per_order=5)
    v = eng.check(intent(count=50), [], now=1e9)
    assert v.allowed and v.adjusted_count == 5  # capped by per-order limit
    v = eng.check(intent(count=50, price=0.9), [], now=1e9)
    assert v.allowed and v.adjusted_count == 5
    held = [{"ticker": "X", "count": 11, "price": 0.95}]  # 10.45 committed > 10 budget
    v = eng.check(intent(count=50, price=0.9), held, now=1e9)
    assert not v.allowed and "bankroll fraction" in v.reason
    held = [{"ticker": "X", "count": 10, "price": 0.95}]  # 0.50 left, nothing affordable
    assert "buys no contracts" in eng.check(intent(count=5, price=0.9), held, now=1e9).reason


def test_rejections(tmp_path):
    eng, _ = engine(tmp_path, kill_file=str(tmp_path / "KILL"))
    assert "no-entry" in eng.check(intent(secs_to_close=60), [], now=1e9).reason
    assert "spot stale" in eng.check(intent(spot_age=30), [], now=1e9).reason
    assert "book stale" in eng.check(intent(book_age=None), [], now=1e9).reason
    assert (
        "already have"
        in eng.check(intent(), [{"ticker": "T1", "count": 1, "price": 0.5}], now=1e9).reason
    )
    many = [{"ticker": f"X{i}", "count": 1, "price": 0.1} for i in range(3)]
    assert "max open" in eng.check(intent(), many, now=1e9).reason
    (tmp_path / "KILL").write_text("stop")
    assert "kill switch" in eng.check(intent(), [], now=1e9).reason


def test_daily_cap_halts_and_persists(tmp_path):
    eng, store = engine(tmp_path, daily_loss_cap=5.0)
    eng.record_settlement(-3.0, now=1e9)
    assert eng.check(intent(), [], now=1e9).allowed
    eng.record_settlement(-2.5, now=1e9 + 1)
    assert eng.state.halted
    v = eng.check(intent(), [], now=1e9 + 2)
    assert not v.allowed and "halted" in v.reason
    # survives a restart
    eng2 = RiskEngine(store, RiskLimits(daily_loss_cap=5.0), "paper")
    assert eng2.state.halted and eng2.state.day_pnl == -5.5
    eng2.reset()
    assert RiskEngine(store, RiskLimits(), "paper").state.halted is False
    # a new UTC day resets day_pnl but not an explicit halt
    eng2.halt("manual")
    eng2._roll_day(1e9 + 86400 * 2)
    assert eng2.state.day_pnl == 0.0 and eng2.state.halted


def test_loss_streak_cooloff(tmp_path):
    eng, _ = engine(tmp_path, consecutive_loss_limit=2, cooloff_seconds=100, daily_loss_cap=1e9)
    eng.record_settlement(-1, now=1e9)
    eng.record_settlement(+1, now=1e9)  # resets streak
    eng.record_settlement(-1, now=1e9)
    assert eng.check(intent(), [], now=1e9).allowed
    eng.record_settlement(-1, now=1e9)
    assert "cooling off" in eng.check(intent(), [], now=1e9 + 1).reason
    assert eng.check(intent(), [], now=1e9 + 101).allowed
