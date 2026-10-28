from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..utils.helpers import get_dict_value_with_path, set_dict_value_with_path

# Same nested layout as <prefix>_config.yaml in the PyScript version, so the ported
# algorithm reads CONFIG["solar"]["powerwall_battery_size"] etc. unchanged. Values that
# the dashboard changes at runtime are entities (see the *_SETTINGS dicts below).
DEFAULT_CONFIG: dict[str, Any] = {
    "cron_interval": 5,
    "database": {
        "power_values_db_data_to_save": 15,
        "solar_available_db_data_to_save": 10,
        "kwh_avg_prices_db_data_to_save": 15,
        "charging_history_db_data_to_save": 12,
    },
    "enable_diagnostic_file_log": False,
    # number of days (incl. today) the charging plan covers
    "days_to_predict": 3,
    # show the generated BattMind dashboard in the sidebar
    "dashboard": True,
    "forecast": {
        "entity_ids": {
            "hourly_service_entity_id": "",
            "daily_service_entity_id": "",
        }
    },
    "home": {
        "entity_ids": {
            "power_consumption_entity_id": "",
            "powerwall_watt_flow_entity_id": "",
            "powerwall_battery_level_entity_id": "",
            "battery_temperature_entity_id": "",
            "ignore_consumption_from_entity_ids": [],
        },
        "power_consumption_entity_id_include_powerwall_charging": False,
        "power_consumption_entity_id_include_powerwall_discharging": False,
        "invert_powerwall_watt_flow_entity_id": False,
    },
    "language": "da",
    "prices": {
        "entity_ids": {
            "energidataservice_buy_entity_id": "",
            "energidataservice_sell_entity_id": "",
            "stromligning_buy_current_price": "",
            "stromligning_buy_tomorrow_available": "",
            "stromligning_buy_forecasts": "",
            "stromligning_sell_current_price": "",
            "stromligning_sell_tomorrow_available": "",
            "stromligning_sell_forecasts": "",
            "stromligning_sell_distribution": "",
            "stromligning_sell_electricity_tax": "",
            "stromligning_sell_nettariff": "",
            "stromligning_sell_systemtariff": "",
            "stromligning_sell_surcharge": "",
        },
        "refund": 0.0,
        "sell_fee": 0.03,
    },
    "solar": {
        "entity_ids": {
            "production_entity_id": "",
            "forecast_entity_id": "",
        },
        # fixed sell price for solar surplus, -1 = use the hourly sell price
        "production_price": -1.0,
        "powerwall_battery_size": 10.0,
        "inverter_discharging_power_limit": 5000.0,
        "powerwall_charging_power_limit": 5000.0,
        "powerwall_discharging_power": 5000.0,
        "powerwall_battery_level_min": 5.0,
        "powerwall_battery_level_max": 100.0,
        "battery_temperature_min": 0.0,
        "battery_temperature_max": 50.0,
        "powerwall_charge_discharge_loss": 0.1,
        "use_charge_discharge_loss_from_history": True,
        "battery_charging_efficiency_curve": {},
        "battery_discharging_efficiency_curve": {},
        "powerwall_wear_cost_per_kwh": 0.1,
    },
    "testing_mode": False,
}

# Config flow option key -> path in DEFAULT_CONFIG
OPTION_PATHS: dict[str, str] = {
    # general
    "language": "language",
    # battery / home
    "powerwall_battery_level_entity_id": "home.entity_ids.powerwall_battery_level_entity_id",
    "powerwall_watt_flow_entity_id": "home.entity_ids.powerwall_watt_flow_entity_id",
    "battery_temperature_entity_id": "home.entity_ids.battery_temperature_entity_id",
    "invert_powerwall_watt_flow_entity_id": "home.invert_powerwall_watt_flow_entity_id",
    "power_consumption_entity_id": "home.entity_ids.power_consumption_entity_id",
    "power_consumption_entity_id_include_powerwall_charging": "home.power_consumption_entity_id_include_powerwall_charging",
    "power_consumption_entity_id_include_powerwall_discharging": "home.power_consumption_entity_id_include_powerwall_discharging",
    "ignore_consumption_from_entity_ids": "home.entity_ids.ignore_consumption_from_entity_ids",
    "powerwall_battery_size": "solar.powerwall_battery_size",
    "powerwall_battery_level_min": "solar.powerwall_battery_level_min",
    "powerwall_battery_level_max": "solar.powerwall_battery_level_max",
    "battery_temperature_min": "solar.battery_temperature_min",
    "battery_temperature_max": "solar.battery_temperature_max",
    "powerwall_charging_power_limit": "solar.powerwall_charging_power_limit",
    "powerwall_discharging_power": "solar.powerwall_discharging_power",
    "powerwall_charge_discharge_loss": "solar.powerwall_charge_discharge_loss",
    "use_charge_discharge_loss_from_history": "solar.use_charge_discharge_loss_from_history",
    "battery_charging_efficiency_curve": "solar.battery_charging_efficiency_curve",
    "battery_discharging_efficiency_curve": "solar.battery_discharging_efficiency_curve",
    "powerwall_wear_cost_per_kwh": "solar.powerwall_wear_cost_per_kwh",
    # prices
    "energidataservice_buy_entity_id": "prices.entity_ids.energidataservice_buy_entity_id",
    "energidataservice_sell_entity_id": "prices.entity_ids.energidataservice_sell_entity_id",
    "stromligning_buy_current_price": "prices.entity_ids.stromligning_buy_current_price",
    "stromligning_buy_tomorrow_available": "prices.entity_ids.stromligning_buy_tomorrow_available",
    "stromligning_buy_forecasts": "prices.entity_ids.stromligning_buy_forecasts",
    "stromligning_sell_current_price": "prices.entity_ids.stromligning_sell_current_price",
    "stromligning_sell_tomorrow_available": "prices.entity_ids.stromligning_sell_tomorrow_available",
    "stromligning_sell_forecasts": "prices.entity_ids.stromligning_sell_forecasts",
    "stromligning_sell_distribution": "prices.entity_ids.stromligning_sell_distribution",
    "stromligning_sell_electricity_tax": "prices.entity_ids.stromligning_sell_electricity_tax",
    "stromligning_sell_nettariff": "prices.entity_ids.stromligning_sell_nettariff",
    "stromligning_sell_systemtariff": "prices.entity_ids.stromligning_sell_systemtariff",
    "stromligning_sell_surcharge": "prices.entity_ids.stromligning_sell_surcharge",
    "refund": "prices.refund",
    "sell_fee": "prices.sell_fee",
    # solar / forecast
    "production_entity_id": "solar.entity_ids.production_entity_id",
    "forecast_entity_id": "solar.entity_ids.forecast_entity_id",
    "inverter_discharging_power_limit": "solar.inverter_discharging_power_limit",
    "production_price": "solar.production_price",
    "hourly_service_entity_id": "forecast.entity_ids.hourly_service_entity_id",
    "daily_service_entity_id": "forecast.entity_ids.daily_service_entity_id",
    # advanced
    "cron_interval": "cron_interval",
    "power_values_db_data_to_save": "database.power_values_db_data_to_save",
    "solar_available_db_data_to_save": "database.solar_available_db_data_to_save",
    "kwh_avg_prices_db_data_to_save": "database.kwh_avg_prices_db_data_to_save",
    "charging_history_db_data_to_save": "database.charging_history_db_data_to_save",
    "enable_diagnostic_file_log": "enable_diagnostic_file_log",
    "days_to_predict": "days_to_predict",
    "dashboard": "dashboard",
    "testing_mode": "testing_mode",
}

# Config/options flow pages: step id -> option keys shown on that page
FLOW_STEPS: dict[str, list[str]] = {
    "general": ["language"],
    "battery_entities": [
        "powerwall_battery_level_entity_id",
        "powerwall_watt_flow_entity_id",
        "battery_temperature_entity_id",
        "invert_powerwall_watt_flow_entity_id",
    ],
    "battery_specs": [
        "powerwall_battery_size",
        "powerwall_battery_level_min",
        "powerwall_battery_level_max",
        "battery_temperature_min",
        "battery_temperature_max",
        "powerwall_charging_power_limit",
        "powerwall_discharging_power",
        "powerwall_charge_discharge_loss",
        "use_charge_discharge_loss_from_history",
        "battery_charging_efficiency_curve",
        "battery_discharging_efficiency_curve",
        "powerwall_wear_cost_per_kwh",
    ],
    "consumption": [
        "power_consumption_entity_id",
        "power_consumption_entity_id_include_powerwall_charging",
        "power_consumption_entity_id_include_powerwall_discharging",
        "ignore_consumption_from_entity_ids",
    ],
    "prices_energidataservice": [
        "energidataservice_buy_entity_id",
        "energidataservice_sell_entity_id",
    ],
    "prices_stromligning": [
        "stromligning_buy_current_price",
        "stromligning_buy_tomorrow_available",
        "stromligning_buy_forecasts",
        "stromligning_sell_current_price",
        "stromligning_sell_tomorrow_available",
        "stromligning_sell_forecasts",
    ],
    "prices_stromligning_tariffs": [
        "stromligning_sell_distribution",
        "stromligning_sell_electricity_tax",
        "stromligning_sell_nettariff",
        "stromligning_sell_systemtariff",
        "stromligning_sell_surcharge",
    ],
    "price_fees": ["refund", "sell_fee"],
    "solar": ["production_entity_id", "inverter_discharging_power_limit", "production_price", "forecast_entity_id"],
    "weather": ["hourly_service_entity_id", "daily_service_entity_id"],
    "advanced": ["days_to_predict", "cron_interval", "dashboard", "enable_diagnostic_file_log", "testing_mode"],
    "database": [
        "power_values_db_data_to_save",
        "solar_available_db_data_to_save",
        "kwh_avg_prices_db_data_to_save",
        "charging_history_db_data_to_save",
    ],
}

# Price provider -> option keys belonging to it (cleared when the provider is deselected)
PRICE_PROVIDER_KEYS: dict[str, list[str]] = {
    "energidataservice": FLOW_STEPS["prices_energidataservice"],
    "stromligning": FLOW_STEPS["prices_stromligning"] + FLOW_STEPS["prices_stromligning_tariffs"],
}

# Options flow menu -> pages walked through in order
OPTIONS_MENU: dict[str, list[str]] = {
    "general": ["general"],
    "battery": ["battery_entities", "battery_specs"],
    "consumption": ["consumption"],
    "prices": ["price_provider", "prices_energidataservice", "prices_stromligning", "prices_stromligning_tariffs", "price_fees"],
    "solar": ["solar", "weather"],
    "advanced": ["advanced", "database"],
}

# Option keys holding a single entity id ("" when not configured)
ENTITY_OPTION_KEYS = {
    key for key, path in OPTION_PATHS.items() if ".entity_ids." in path
} - {"ignore_consumption_from_entity_ids"}


def default_options() -> dict[str, Any]:
    """Flat option dict with the default value for every config flow key."""
    return {
        key: deepcopy(get_dict_value_with_path(DEFAULT_CONFIG, path))
        for key, path in OPTION_PATHS.items()
    }


def build_config(options: dict[str, Any]) -> dict[str, Any]:
    """Build the nested CONFIG dict from flat config entry options."""
    config = deepcopy(DEFAULT_CONFIG)
    for key, path in OPTION_PATHS.items():
        if key in options and options[key] is not None:
            set_dict_value_with_path(config, path, deepcopy(options[key]))
    return config
