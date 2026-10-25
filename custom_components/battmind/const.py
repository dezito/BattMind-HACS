"""Constants for the BattMind integration."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .helpers import get_dict_value_with_path, set_dict_value_with_path

DOMAIN = "battmind"
MANUFACTURER = "dezito"

CONF_ENTITY_PREFIX = "entity_prefix"
DEFAULT_ENTITY_PREFIX = "battmind"

PLATFORMS = ["sensor", "switch", "number", "select", "text", "button"]

LEGACY_DB_FILES = {
    "charging_history": "{prefix}_charging_history_db.yaml",
    "power_values": "{prefix}_power_values_db.yaml",
    "kwh_avg_prices": "{prefix}_kwh_avg_prices_db.yaml",
    "solar_available": "{prefix}_solar_production_available_db.yaml",
}

STORAGE_VERSION = 1

PRICE_ADDER_DAY_BETWEEN_DIVIDER = 30
CHARGING_HISTORY_COMBINE_AFTER = 24 * 3
HISTORY_BUFFER_HOURS = 50

ENTITY_UNAVAILABLE_STATES = (None, "unavailable", "unknown")
POWERWALL_ACTION_STATES = (
    "grid_charging",
    "discharge_allowed",
    "force_solar_only_charging",
    "force_discharge",
    "blocked_solar_charge",
)

# Languages with runtime texts in texts/<language>.json
LANGUAGES = {
    "da": "Dansk",
    "en": "English",
    "de": "Deutsch",
    "pl": "Polski",
    "uk": "Українська"
}


def normalize_language(value: str | None, fallback: str = "en") -> str:
    """'da-DK' (PyScript config) or 'da' -> 'da'."""
    language = str(value or "").split("-")[0].split("_")[0].lower()
    return language if language in LANGUAGES else fallback


# Symbol shown for hass.config.currency (ISO 4217); other currencies are shown with their code
CURRENCY_SYMBOLS = {
    "DKK": "kr",
    "SEK": "kr",
    "NOK": "kr",
    "ISK": "kr",
    "EUR": "€",
    "GBP": "£",
    "USD": "$",
    "PLN": "zł",
    "UAH": "₴",
    "CZK": "Kč",
    "HUF": "Ft",
}


def currency_symbol(currency: str | None) -> str | None:
    """'DKK' -> 'kr', 'CHF' -> 'CHF', None/'' -> None (the language's own valuta texts are used)."""
    if not currency:
        return None
    return CURRENCY_SYMBOLS.get(currency.upper(), currency.upper())

WEATHER_CONDITION_DICT = {
    "sunny": 100,
    "windy": 80,
    "windy-variant": 80,
    "partlycloudy": 60,
    "cloudy": 40,
    "rainy": 40,
    "pouring": 20,
    "lightning": 20,
    "lightning-rainy": 20,
    "snowy": 20,
    "snowy-rainy": 20,
    "clear-night": 0,
    "fog": 20,
    "hail": 0,
    "exceptional": 0,
}

CHARGING_TYPES = {
    "error": {
        "priority": 1,
        "emoji": "☠️",
        "group": "status"
    },
    "no_rule": {
        "priority": 2,
        "emoji": "⚠️",
        "group": "status"
    },
    "cheapest_hour_fill_planner": {
        "priority": 3,
        "emoji": "💸",
        "group": "charge"
    },
    "most_expensive_planner": {
        "priority": 3.1,
        "emoji": "🔥",
        "group": "charge"
    },
    "needed_before_max_level_planner": {
        "priority": 3.2,
        "emoji": "🩹",
        "group": "charge"
    },
    "negative_price_planner": {
        "priority": 3.3,
        "emoji": "🎁",
        "group": "charge"
    },
    "grid_charge_to_sell": {
        "priority": 3.4,
        "emoji": "🔁",
        "group": "charge"
    },
    "discharge_before_solar": {
        "priority": 4.3,
        "emoji": "🌄",
        "group": "discharge"
    },
    "only_discharge_on_profit": {
        "priority": 4.4,
        "emoji": "🚫",
        "group": "discharge"
    },
    "prioritize_discharge_hours_by_energy_cost": {
        "priority": 4.5,
        "emoji": "⏳",
        "group": "discharge"
    },
    "most_profit": {
        "priority": 4.6,
        "emoji": "🤑",
        "group": "discharge"
    },
    "sell_excess_kwh_available": {
        "priority": 4.7,
        "emoji": "📤",
        "group": "discharge"
    },
    "force_powerwall_max_hours_old_discharge": {
        "priority": 4.8,
        "emoji": "🕰️",
        "group": "discharge"
    },
    "charging": {
        "priority": 4,
        "emoji": "📈",
        "group": "charge"
    },
    "discharging": {
        "priority": 4.1,
        "emoji": "📉",
        "group": "discharge"
    },
    "force_discharge": {
        "priority": 5,
        "emoji": "🛒",
        "group": "discharge"
    },
    "solar": {
        "priority": 6,
        "emoji": "☀️",
        "group": "charge"
    },
    "solar_corrected": {
        "priority": 6.1,
        "emoji": "🌤️",
        "group": "charge"
    },
    "balance": {
        "priority": 7,
        "emoji": "⚖️",
        "group": "info"
    },
    "sunrise": {
        "priority": 96,
        "emoji": "🌅",
        "group": "info"
    },
    "sunset": {
        "priority": 97,
        "emoji": "🌇",
        "group": "info"
    },
    "profit": {
        "priority": 100,
        "emoji": "💰",
        "group": "info"
    },
    "average": {
        "priority": 999,
        "emoji": "⌛",
        "group": "info"
    },
    "bundle": {
        "priority": 1000,
        "emoji": "📦",
        "group": "info"
    },
    "blocked_solar_charge": {
        "priority": 6.2,
        "emoji": "🎛️",
        "group": "charge"
    },
}

# the sections of the emoji description, in order
EMOJI_DESCRIPTION_GROUPS = ("charge", "discharge", "status", "info")

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
        "powerwall_charge_discharge_loss": 0.1,
        "use_charge_discharge_loss_from_history": True,
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
    "invert_powerwall_watt_flow_entity_id": "home.invert_powerwall_watt_flow_entity_id",
    "power_consumption_entity_id": "home.entity_ids.power_consumption_entity_id",
    "power_consumption_entity_id_include_powerwall_charging": "home.power_consumption_entity_id_include_powerwall_charging",
    "power_consumption_entity_id_include_powerwall_discharging": "home.power_consumption_entity_id_include_powerwall_discharging",
    "ignore_consumption_from_entity_ids": "home.entity_ids.ignore_consumption_from_entity_ids",
    "powerwall_battery_size": "solar.powerwall_battery_size",
    "powerwall_battery_level_min": "solar.powerwall_battery_level_min",
    "powerwall_battery_level_max": "solar.powerwall_battery_level_max",
    "powerwall_charging_power_limit": "solar.powerwall_charging_power_limit",
    "powerwall_discharging_power": "solar.powerwall_discharging_power",
    "powerwall_charge_discharge_loss": "solar.powerwall_charge_discharge_loss",
    "use_charge_discharge_loss_from_history": "solar.use_charge_discharge_loss_from_history",
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
        "invert_powerwall_watt_flow_entity_id",
    ],
    "battery_specs": [
        "powerwall_battery_size",
        "powerwall_battery_level_min",
        "powerwall_battery_level_max",
        "powerwall_charging_power_limit",
        "powerwall_discharging_power",
        "powerwall_charge_discharge_loss",
        "use_charge_discharge_loss_from_history",
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
    # correct the price forecast with its measured bias (engine_analysis.py)
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

SIGNAL_UPDATE = "battmind_update_{entry_id}"

SERVICE_DEBUG_INFO = "debug_info"
SERVICE_EXPORT_DEBUG = "export_debug"
SERVICE_RECALC_HISTORY_TODAY = "recalc_charging_history_today"
SERVICE_ENFORCE_PLANNING = "enforce_planning"
SERVICE_EXPORT_DATA = "export_data"
SERVICE_IMPORT_DATA = "import_data"
