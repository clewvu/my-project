import pytest

from kalshi_bot.fees import breakeven_win_rate, fee_per_contract, order_fee


def test_fee_shape():
    assert fee_per_contract(0.5) == pytest.approx(0.0175)
    assert fee_per_contract(0.1) == pytest.approx(0.0063)
    assert fee_per_contract(0.5) > fee_per_contract(0.9) > fee_per_contract(0.99)
    assert fee_per_contract(0.5, rate=0.0) == 0.0


def test_order_fee_rounds_up_to_cent():
    assert order_fee(0.5, 1) == 0.02  # 1.75c -> 2c
    assert order_fee(0.5, 10) == 0.18  # 17.5c -> 18c
    assert order_fee(0.5, 4) == 0.07  # exactly 7c
    assert order_fee(0.9, 1) == 0.01


def test_breakeven():
    assert breakeven_win_rate(0.5) == pytest.approx(0.5175)
    assert breakeven_win_rate(0.9) == pytest.approx(0.9063)
