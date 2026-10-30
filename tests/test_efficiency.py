from __future__ import annotations

import math

import pytest

from custom_components.battmind.utils.efficiency import (
    EFFICIENCY_MIN_MEASUREMENTS,
    add_efficiency_measurement,
    efficiency_at_power,
    learned_efficiency_curve,
    measure_efficiency,
    measured_efficiency_steps,
    parse_efficiency_curve,
)


def test_parse_and_interpolate_efficiency_curve() -> None:
    curve = parse_efficiency_curve({"5000": 96, "1000": 80})

    assert curve == ((1000.0, 0.8), (5000.0, 0.96))
    assert efficiency_at_power(curve, 500, 0.9) == pytest.approx(0.8)
    assert efficiency_at_power(curve, 3000, 0.9) == pytest.approx(0.88)
    assert efficiency_at_power(curve, 6000, 0.9) == pytest.approx(0.96)
    assert efficiency_at_power((), 3000, 0.9) == pytest.approx(0.9)


@pytest.mark.parametrize(
    "curve",
    [
        [],
        {"zero": 90},
        {"0": 90},
        {"1000": 0},
        {"1000": 101},
    ],
)
def test_invalid_efficiency_curve(curve: object) -> None:
    with pytest.raises(ValueError):
        parse_efficiency_curve(curve)


def _battery(
    phases: list[tuple[float, float, float]], level: float = 20.0, size: float = 10.0, sign: float = 1.0, start: float = 1_000_000.0,
) -> tuple[list, list]:
    """Power and integer level rows of phases (seconds, AC W: + charging, efficiency); the level changes when it crosses a whole %."""
    flow, levels = [], [(start - 60, level)]
    stored = level * size * 10  # Wh
    time = start
    for seconds, watt, efficiency in phases:
        flow.append((time, sign * watt))
        battery_w = watt * efficiency if watt > 0 else watt / efficiency
        end = time + seconds
        while battery_w:
            percent = round(stored / (size * 10), 9)
            target = math.floor(percent) + 1 if battery_w > 0 else math.ceil(percent) - 1
            crossing = time + (target * size * 10 - stored) / battery_w * 3600
            if crossing > end:
                break
            stored, time = target * size * 10, crossing
            levels.append((time, float(target)))
        stored += battery_w * (end - time) / 3600
        time = end
    flow.append((time, 0.0))
    return flow, levels


def _measure(flow: list, levels: list, sign: float = 1.0) -> tuple[list, float]:
    return measure_efficiency(flow, levels, 0.0, levels[-1][0] + 3600, 1.0, sign > 0, 10.0)


@pytest.mark.parametrize("sign", [1.0, -1.0])
def test_efficiency_is_measured_between_changes_of_the_battery_level(sign: float) -> None:
    flow, levels = _battery([(3600, 3000, 0.9), (600, 0, 1.0), (3600, -2000, 0.95)], sign=sign)

    measurements, until = _measure(flow, levels, sign)

    charging = [m for m in measurements if m[0] == "charging"]
    discharging = [m for m in measurements if m[0] == "discharging"]
    assert len(charging) >= 25 and len(discharging) >= 19
    assert sum(m[2] for m in charging) / sum(m[1] for m in charging) == pytest.approx(0.9)
    assert sum(m[1] for m in discharging) / sum(m[2] for m in discharging) == pytest.approx(0.95)
    # the idle pause between them is no measurement, and the last change is where the next run continues
    assert until == levels[-1][0]


def test_efficiency_measurement_skips_unknown_power_and_the_opposite_direction() -> None:
    flow, levels = _battery([(3600, 3000, 0.9)])
    unknown = sorted([*flow, (1_001_500.0, None), (1_001_560.0, 3000.0)], key=lambda row: row[0])
    opposite = sorted([*flow, (1_001_500.0, -3000.0), (1_001_560.0, 3000.0)])

    assert len(_measure(unknown, levels)[0]) == len(_measure(flow, levels)[0]) - 1
    assert len(_measure(opposite, levels)[0]) == len(_measure(flow, levels)[0]) - 1


def test_efficiency_measurement_waits_for_the_next_change_of_the_level() -> None:
    flow, levels = _battery([(3600, 3000, 0.9)])
    half = len(levels) // 2

    first, until = measure_efficiency(flow, levels[:half], 0.0, levels[half - 1][0], 1.0, True, 10.0)
    rest, _ = measure_efficiency(flow, levels, until, levels[-1][0], 1.0, True, 10.0)

    assert until == levels[half - 1][0]
    assert len(first) + len(rest) == len(_measure(flow, levels)[0])


def test_learned_efficiency_curve_needs_enough_measurements() -> None:
    steps: dict = {}
    for _ in range(EFFICIENCY_MIN_MEASUREMENTS - 1):
        add_efficiency_measurement(steps, 0.111, 0.1, 133.0)  # 3004 W
    assert learned_efficiency_curve(steps, True) == ()
    assert measured_efficiency_steps(steps, True)[0]["used"] is False

    add_efficiency_measurement(steps, 0.111, 0.1, 133.0)
    assert list(steps) == ["3000"]
    assert learned_efficiency_curve(steps, True) == ((3250.0, pytest.approx(0.1 / 0.111)),)
    assert learned_efficiency_curve(steps, False) == ((3250.0, 1.0),)  # capped at 100 %


def test_efficiency_steps_count_older_measurements_half_beyond_the_limit() -> None:
    steps: dict = {}
    for _ in range(EFFICIENCY_MIN_MEASUREMENTS):
        add_efficiency_measurement(steps, 6.0, 5.4, 7200)
    assert steps["3000"]["ac"] <= 100.0
    assert steps["3000"]["battery"] / steps["3000"]["ac"] == pytest.approx(0.9)
    assert learned_efficiency_curve({"3000": {"ac": 10, "battery": 4, "n": 50}}, True) == ()  # below 50 %
    assert learned_efficiency_curve({"bad": {"ac": 10}}, True) == ()
