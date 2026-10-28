from __future__ import annotations

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
    "battery_stopped",
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
