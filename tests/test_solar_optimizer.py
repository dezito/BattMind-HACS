"""optimize_solar_charging: sell the solar surplus expensive, charge cheap, still end full."""
from __future__ import annotations

from custom_components.battmind.engine_planner import _optimize_solar_blocking

HOURS = list(range(6, 24))


def run(start_level, solar, prices, delta=None, minimum=10.0, maximum=100.0):
    available = {h: pct for h, pct in solar.items() if pct > 0}
    return _optimize_solar_blocking(
        HOURS, delta or {}, available, prices, list(available), start_level, minimum, maximum
    )


def end_level(flow):
    return sum(flow[HOURS[-1]])


def test_sells_expensive_morning_and_charges_in_cheap_midday() -> None:
    solar = {h: 20.0 for h in range(8, 16)}
    prices = {8: 1.0, 9: 0.9, 10: 0.8, 11: 0.5, 12: 0.2, 13: 0.2, 14: 0.3, 15: 0.4}
    blocked, flow, charged, original = run(50.0, solar, prices)

    assert original[8] == original[9] == 20.0 and original[10] == 10.0
    assert blocked == {8, 9, 10, 11}
    assert charged[12] == charged[13] == 20.0 and charged[14] == 10.0
    assert end_level(flow) == 100.0


def test_blocks_expensive_hour_after_the_original_fill_point() -> None:
    # Full at 10 without blocking. Moving 8's surplus only pays if 11 (expensive) is blocked too
    solar = {h: 20.0 for h in range(8, 14)}
    prices = {8: 0.5, 9: 0.45, 10: 0.45, 11: 0.6, 12: 0.2, 13: 0.2}
    blocked, flow, charged, _ = run(50.0, solar, prices)

    # 50% needed: one 0.45 hour + the two 0.2 hours, nothing at 0.5 / 0.6
    assert {8, 11} <= blocked
    assert charged[8] == charged[11] == 0.0
    assert sum(charged[h] * prices[h] for h in prices) == 15.0
    assert end_level(flow) == 100.0


def test_nothing_blocked_when_the_sun_cannot_fill_the_battery() -> None:
    solar = {h: 5.0 for h in range(8, 16)}
    prices = {h: 1.0 - h / 20 for h in range(8, 16)}
    blocked, flow, charged, original = run(20.0, solar, prices)

    assert blocked == set()
    assert charged == original
    assert end_level(flow) == 60.0


def test_does_not_sell_energy_the_evening_needs() -> None:
    # The evening drains the battery to the minimum, so every solar kWh is needed
    solar = {h: 15.0 for h in range(8, 12)}
    prices = {8: 1.0, 9: 0.9, 10: 0.2, 11: 0.2}
    delta = {h: -20.0 for h in range(17, 23)}
    blocked, flow, _, _ = run(40.0, solar, prices, delta=delta)

    assert blocked == set()
    assert end_level(flow) == 10.0


def test_equal_prices_block_nothing() -> None:
    solar = {h: 20.0 for h in range(8, 16)}
    prices = {h: 0.5 for h in range(8, 16)}
    blocked, _, _, _ = run(50.0, solar, prices)

    assert blocked == set()
