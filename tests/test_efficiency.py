from __future__ import annotations

import pytest

from custom_components.battmind.utils.efficiency import efficiency_at_power, parse_efficiency_curve


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
