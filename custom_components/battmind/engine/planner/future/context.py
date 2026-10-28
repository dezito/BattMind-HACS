"""The values the future-charging rules share."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class FutureContext:
    """The inputs of ``future_charging`` and its running totals; handed to every rule."""

    amount_of_days: Any
    battery_expenses: Any
    battery_expenses_keys: Any
    calc_battery_loss_cost: Any
    charge_hours: Any
    charging_plan: Any
    config: Any
    current_hour: Any
    current_sell_price_timestamp: Any
    efficiency: Any
    forecast_type: Any
    get_battery_level: Any
    get_min_profit_per_kwh: Any
    grid_prices: Any
    grid_sell_prices: Any
    kwh_to_percentage: Any
    max_kwh_charging: Any
    max_kwh_discharging: Any
    name: Any
    now: Any
    percentage_to_kwh: Any
    sorted_by_cheapest_price: Any
    t: Any
    totals: Any  # [total_cost, total_kwh], updated in place by the rules
    valuta: Any
    valuta_kwh: Any

    def r(self, key: str, **fmt) -> str:
        """Planner explanation text (ui.reason.<key> of texts/<language>.json)."""
        return self.t(f"ui.reason.{key}", **fmt)
