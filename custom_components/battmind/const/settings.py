from __future__ import annotations

from typing import Any
from .base import CHARGING_HISTORY_COMBINE_AFTER

# --------------------------------------------------------------------------- #
# Settings entities (dashboard "Indstillinger")
# --------------------------------------------------------------------------- #
# The values are restored on restart and migrated once from input_*.<legacy prefix>_<key>.
SWITCH_SETTINGS: dict[str, dict[str, Any]] = {
    "solar_charging": {"default": True},
    "cheapest_hour_fill_planner": {"default": True},
    "cheapest_hour_fill_up": {"default": True},
    "most_expensive_planner": {"default": True},
    "needed_before_max_level_planner": {"default": True},
    "only_discharge_on_profit": {"default": True},
    "prioritize_discharge_hours_by_energy_cost": {"default": True},
    "sell_excess_kwh_available": {"default": True},
    "use_midnight_battery_level": {"default": False},
    "most_profit": {"default": True},
    "force_powerwall_max_hours_old_discharge": {"default": True},
    "optimize_solar_charging": {"default": True},
    "negative_price_planner": {"default": True},
    "grid_charge_to_sell": {"default": True},
    "discharge_before_solar": {"default": True},
    "optimizer_control": {"default": True},
    # use the configured power-dependent charging/discharging efficiency curves
    "enhance_battery_efficiency": {"default": True},
    # correct the price forecast with its measured bias (engine/analysis)
    "price_forecast_correction": {"default": True},
}

NUMBER_SETTINGS: dict[str, dict[str, Any]] = {
    "cheap_price_periods": {
        "min": 1,
        "max": 10,
        "step": 1,
        "mode": "slider",
        "unit": None,
        "default": 2,
    },
    "cheapest_price_rise_threshold": {
        "min": 0.0,
        "max": 1.0,
        "step": 0.01,
        "mode": "box",
        "unit": "{valuta_kwh}",
        "default": 0.5,
    },
    "cheap_price_period_rise_threshold": {
        "min": 0.0,
        "max": 1.0,
        "step": 0.01,
        "mode": "box",
        "unit": "{valuta_kwh}",
        "default": 0.1,
    },
    "min_profit_per_kwh": {
        "min": 0.0,
        "max": 10.0,
        "step": 0.01,
        "mode": "box",
        "unit": "{valuta_kwh}",
        "default": 0.0,
    },
    "min_total_profit": {
        "min": 0.0,
        "max": 100.0,
        "step": 0.01,
        "mode": "box",
        "unit": "{valuta}",
        "default": 0.0,
    },
    "min_sell_kwh": {
        "min": 0.2,
        "max": 1.0,
        "step": 0.1,
        "mode": "box",
        "unit": "kWh",
        "default": 0.2,
    },
    "usage_reserve": {
        "min": 0.0,
        "max": 100.0,
        "step": 1.0,
        "mode": "slider",
        "unit": "%",
        "default": 0.0,
    },
    "powerwall_max_hours_old": {
        "min": 1,
        "max": CHARGING_HISTORY_COMBINE_AFTER,
        "step": 1,
        "mode": "box",
        "unit": "h",
        "default": 48,
    },
}

FORECAST_TYPE_OPTIONS = [
    "Average",
    "EMA (Exponential Moving Average)",
    "Trend"
]
SELECT_SETTINGS: dict[str, dict[str, Any]] = {
    "forecast_type": {
        
        "options": FORECAST_TYPE_OPTIONS,
        "default": "EMA (Exponential Moving Average)",
    },
}

TEXT_SETTINGS: dict[str, dict[str, Any]] = {
    "exclude_sell_hours": {
        
        "pattern": r"^(unknown|unavailable|([0-9]|1[0-9]|2[0-3])(,([0-9]|1[0-9]|2[0-3]))*)?$",
        "default": "",
    },
}

# Output sensors (icons in icons.json)
SENSOR_KEYS = (
    "current_charging_rule",
    "powerwall_action",
    "overview",
    "charging_history",
    "emoji_description",
    "battery_level_needed",
    "kwh_charged_by_solar",
    "analysis",
)
