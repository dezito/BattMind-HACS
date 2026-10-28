"""Power-dependent battery efficiency curves."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

EfficiencyCurve = tuple[tuple[float, float], ...]


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
