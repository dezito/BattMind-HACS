"""Power-dependent battery efficiency curves, configured or measured.

Measured: between two changes of the battery level the battery moved exactly the difference of
the two levels, so the AC energy of the battery power sensor in that time is compared with it
(charging: battery kWh / AC kWh, discharging: AC kWh / battery kWh). The measurements are added
up per AC power step; consecutive measurements in the same step share their boundaries, so the
rounding of the battery level cancels out. A wrong battery size scales the charging and
discharging efficiency in opposite directions; their product (the round trip) stays right.
"""
from __future__ import annotations

import bisect
from collections.abc import Mapping, Sequence
from typing import Any

EfficiencyCurve = tuple[tuple[float, float], ...]

EFFICIENCY_STEP_W = 500            # width of a measured AC power step
EFFICIENCY_IDLE_W = 50.0           # below this the battery counts as idle
EFFICIENCY_MIN_LEVEL_STEP = 1.0    # % battery level per measurement
EFFICIENCY_MIN_SECONDS = 120       # duration of a measurement
EFFICIENCY_MAX_IDLE = 0.1          # share of a measurement the battery may be idle
EFFICIENCY_MAX_OPPOSITE = 0.03     # energy the other way, share of the measured energy
EFFICIENCY_MIN_KWH = 2.0           # AC and battery kWh before a power step is used
EFFICIENCY_MIN_MEASUREMENTS = 20   # measurements before a power step is used
EFFICIENCY_MAX_KWH = 100.0         # beyond this AC kWh the older measurements of a step count half
EFFICIENCY_MIN_USED = 0.5          # measured efficiencies below this are not used


def efficiency_curve_mapping(curve: EfficiencyCurve) -> dict[str, float]:
    """A curve as the configured W -> percent mapping."""
    return {str(round(power)): round(efficiency * 100.0, 2) for power, efficiency in curve}


def _flow_energy(flow: list, keys: list[float], start: float, end: float, factor: float) -> tuple[float, float, float, float]:
    """(Wh positive, Wh negative, idle s, unknown s) of a step-held power sensor between start and end."""
    positive = negative = idle = unknown = 0.0
    index = bisect.bisect_right(keys, start) - 1
    value = flow[index][1] if index >= 0 else None
    time = start
    index += 1
    while time < end:
        next_time = min(keys[index], end) if index < len(keys) else end
        seconds = next_time - time
        if value is None:
            unknown += seconds
        else:
            watt = value * factor
            if abs(watt) < EFFICIENCY_IDLE_W:
                idle += seconds
            elif watt > 0:
                positive += watt * seconds / 3600.0
            else:
                negative -= watt * seconds / 3600.0
        time = next_time
        if index < len(keys):
            value = flow[index][1]
            index += 1
    return positive, negative, idle, unknown


def measure_efficiency(
    flow: list, levels: list, start: float, end: float, factor: float, positive_is_charging: bool, battery_size: float,
) -> tuple[list[tuple[str, float, float, float]], float]:
    """Measurements ("charging"/"discharging", AC kWh, battery kWh, seconds) between start and end.

    flow and levels are the sorted (epoch, value|None) rows of the battery power (factor converts it
    to W) and the battery level (%). Returns the measurements and the time measured until: the last
    change of the battery level a measurement ended at or was given up, to continue from next time.
    """
    changes: list[tuple[float, float]] = []
    previous = None
    for ts, level in levels:
        if ts > end:
            break
        if level is None:
            continue
        if previous is not None and level != previous and ts >= start:
            changes.append((ts, float(level)))
        previous = level
    keys = [row[0] for row in flow]
    measurements: list[tuple[str, float, float, float]] = []
    first = 0
    total = [0.0, 0.0, 0.0, 0.0]
    for index in range(1, len(changes)):
        piece = _flow_energy(flow, keys, changes[index - 1][0], changes[index][0], factor)
        total = [a + b for a, b in zip(total, piece)]
        (start_ts, start_level), (end_ts, end_level) = changes[first], changes[index]
        seconds = end_ts - start_ts
        level_step = end_level - start_level
        if abs(level_step) < EFFICIENCY_MIN_LEVEL_STEP or seconds < EFFICIENCY_MIN_SECONDS:
            continue
        positive, negative, idle, unknown = total
        charging_wh, discharging_wh = (positive, negative) if positive_is_charging else (negative, positive)
        charging = level_step > 0
        ac_wh, opposite_wh = (charging_wh, discharging_wh) if charging else (discharging_wh, charging_wh)
        battery_kwh = abs(level_step) / 100.0 * battery_size
        if (
            ac_wh > 0
            and unknown == 0
            and idle <= EFFICIENCY_MAX_IDLE * seconds
            and opposite_wh <= EFFICIENCY_MAX_OPPOSITE * ac_wh
            # a jump of the battery level (calibration) is not a measurement
            and 0.25 <= battery_kwh * 1000.0 / ac_wh <= 4.0
        ):
            measurements.append(("charging" if charging else "discharging", ac_wh / 1000.0, battery_kwh, seconds))
        first = index
        total = [0.0, 0.0, 0.0, 0.0]
    until = changes[first][0] if changes else start
    return measurements, until


def add_efficiency_measurement(steps: dict, ac_kwh: float, battery_kwh: float, seconds: float) -> None:
    """Add a measurement to its AC power step {"<W from>": {"ac", "battery", "n"}}."""
    power = ac_kwh * 3_600_000.0 / seconds
    key = str(int(power // EFFICIENCY_STEP_W) * EFFICIENCY_STEP_W)
    step = steps.setdefault(key, {"ac": 0.0, "battery": 0.0, "n": 0})
    step["ac"] = float(step.get("ac", 0.0)) + ac_kwh
    step["battery"] = float(step.get("battery", 0.0)) + battery_kwh
    step["n"] = int(step.get("n", 0)) + 1
    if step["ac"] > EFFICIENCY_MAX_KWH:
        step["ac"] /= 2
        step["battery"] /= 2
        step["n"] = (step["n"] + 1) // 2
    step["ac"] = round(step["ac"], 6)
    step["battery"] = round(step["battery"], 6)


def measured_efficiency_steps(steps: Any, charging: bool) -> list[dict]:
    """Every measured power step: middle power W, efficiency 0..1, AC kWh, measurements and whether it is used."""
    result = []
    for key, step in (steps if isinstance(steps, Mapping) else {}).items():
        try:
            power = float(key) + EFFICIENCY_STEP_W / 2
            ac = float(step["ac"])
            battery = float(step["battery"])
            count = int(step["n"])
        except (KeyError, TypeError, ValueError):
            continue
        if ac <= 0 or battery <= 0:
            continue
        efficiency = battery / ac if charging else ac / battery
        used = count >= EFFICIENCY_MIN_MEASUREMENTS and min(ac, battery) >= EFFICIENCY_MIN_KWH and efficiency >= EFFICIENCY_MIN_USED
        result.append({"power": power, "efficiency": efficiency, "kwh": ac, "n": count, "used": used})
    return sorted(result, key=lambda row: row["power"])


def learned_efficiency_curve(steps: Any, charging: bool) -> EfficiencyCurve:
    """The curve of the power steps with enough measurements (at most 100 %)."""
    return tuple((row["power"], min(row["efficiency"], 1.0)) for row in measured_efficiency_steps(steps, charging) if row["used"])


def parse_efficiency_curve(value: Any) -> EfficiencyCurve:
    """Return sorted (W, efficiency 0..1) points from a W -> percent mapping."""
    if value in (None, "", {}):
        return ()
    if not isinstance(value, Mapping):
        raise ValueError("the curve must be an object mapping watt to percent")
    points: dict[float, float] = {}
    for raw_power, raw_efficiency in value.items():
        try:
            power = float(raw_power)
            efficiency = float(raw_efficiency)
        except (TypeError, ValueError) as err:
            raise ValueError("power and efficiency must be numbers") from err
        if power <= 0:
            raise ValueError("power must be above 0 W")
        if not 0 < efficiency <= 100:
            raise ValueError("efficiency must be above 0 and at most 100 percent")
        points[power] = efficiency / 100.0
    return tuple(sorted(points.items()))


def efficiency_at_power(curve: Sequence[tuple[float, float]], power_w: float, fallback: float) -> float:
    """Interpolate a curve at absolute power; outside it, use the nearest endpoint."""
    if not curve or power_w <= 0:
        return fallback
    power = abs(power_w)
    if power <= curve[0][0]:
        return curve[0][1]
    if power >= curve[-1][0]:
        return curve[-1][1]
    for (left_power, left_efficiency), (right_power, right_efficiency) in zip(curve, curve[1:]):
        if power <= right_power:
            fraction = (power - left_power) / (right_power - left_power)
            return left_efficiency + fraction * (right_efficiency - left_efficiency)
    return curve[-1][1]
