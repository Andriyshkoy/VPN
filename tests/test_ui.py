from decimal import Decimal

from bot.ui import estimate_monthly_cost, format_whole_money


def test_monthly_cost_estimate_hides_hourly_microcharge_without_overpromising():
    estimate = estimate_monthly_cost(Decimal("0.07"), 3_600)

    assert estimate == Decimal("50")
    assert format_whole_money(estimate) == "50"


def test_monthly_cost_estimate_tracks_runtime_tariff():
    assert estimate_monthly_cost(Decimal("1.00"), 7_200) == Decimal("360")
