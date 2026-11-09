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


def test_size_scale_only_shrinks(tmp_path):
    eng, _ = engine(
        tmp_path,
        daily_loss_cap=20.0,
        adapt_window=10,
        drift_window=100,
        consecutive_loss_limit=1000,
    )
    assert eng.size_scale() == 1.0
    for _ in range(6):
        eng.record_settlement(+1.0, now=1e9, expected_win_p=0.6)
    assert eng.size_scale() == 1.0
    for _ in range(7):
        eng.record_settlement(-1.0, now=1e9, expected_win_p=0.6)
    assert eng.size_scale() == 0.5  # last 10: 3 wins, 7 losses = -4, above -cap/4 = -5
    v = eng.check(intent(count=4), [], now=1e9)
    assert v.allowed and v.adjusted_count == 2 and "scaled" in v.reason
    for _ in range(4):
        eng.record_settlement(-1.5, now=1e9, expected_win_p=0.6)
    assert eng.size_scale() == 0.25
    v = eng.check(intent(count=2), [], now=1e9)
    assert not v.allowed and "scaled to zero" in v.reason


def test_drift_halts_when_wins_fall_short(tmp_path):
    eng, store = engine(
        tmp_path, daily_loss_cap=1e9, consecutive_loss_limit=1000, drift_window=30, drift_z=2.0
    )
    # model says 70% win each time; realised 11/30
    for i in range(30):
        pnl = +0.3 if i % 3 == 0 and i < 33 else -0.7
        eng.record_settlement(pnl, now=1e9 + i, expected_win_p=0.7)
    assert eng.state.halted and "drift" in eng.state.halt_reason
    # state survives restart
    eng2 = RiskEngine(store, RiskLimits(drift_window=30), "paper")
    assert eng2.state.halted and len(eng2.state.recent) == 30
    eng2.reset()
    assert not eng2.state.halted


def test_drift_quiet_when_results_match_model(tmp_path):
    eng, _ = engine(tmp_path, daily_loss_cap=1e9, consecutive_loss_limit=1000, drift_window=30)
    for i in range(30):
        eng.record_settlement(+0.4 if i % 10 < 6 else -0.6, now=1e9 + i, expected_win_p=0.6)
    assert not eng.state.halted
