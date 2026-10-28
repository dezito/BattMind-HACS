"""Selectors, schema building and validation of the config flow pages."""
from __future__ import annotations

from typing import Any
import re

import voluptuous as vol

from homeassistant.helpers import selector

from ..const import ENTITY_OPTION_KEYS, LANGUAGES, OPTION_PATHS, currency_symbol, default_options, normalize_language
from ..utils.helpers import get_dict_value_with_path
from ..utils.efficiency import parse_efficiency_curve


PREFIX_RE = re.compile(r"^[a-z][a-z0-9_]{0,49}$")
IMPORT_NONE = "none"
BACKUP_EXPORT = "export"
CRON_INTERVALS = ["1", "2", "3", "4", "5", "6", "10", "12", "15", "20", "30"]
PRICE_PROVIDERS = ["energidataservice", "stromligning"]
ENERGIDATASERVICE_URL = "https://github.com/MTrab/energidataservice"  # {eds_url} in the step descriptions
CONFIG_FLOW_STEPS = [
    "battery_entities",
    "battery_specs",
    "consumption",
    "price_provider",
    "prices_energidataservice",
    "prices_stromligning",
    "prices_stromligning_tariffs",
    "price_fees",
    "solar",
    "weather",
    "advanced",
    "database",
]
LEGACY_SETTING_PATHS = {
    "cheap_price_periods": "prices.cheap_price_periods",
    "cheap_price_period_rise_threshold": "prices.cheap_price_period_rise_threshold",
    "cheapest_price_rise_threshold": "prices.cheapest_price_rise_threshold",
}


def _entity(
    domain: str | list[str],
    *,
    device_class: str | None = None,
    integration: str | None = None,
    multiple: bool = False,
) -> selector.EntitySelector:
    """Entity picker limited to the relevant entities (domain + device class or integration)."""
    entity_filter: dict[str, Any] = {"domain": domain}
    if device_class:
        entity_filter["device_class"] = device_class
    if integration:
        entity_filter["integration"] = integration
    return selector.EntitySelector(selector.EntitySelectorConfig(filter=[entity_filter], multiple=multiple))


def _number(minimum: float, maximum: float, step: float, unit: str | None = None) -> selector.NumberSelector:
    config: dict[str, Any] = {"min": minimum, "max": maximum, "step": step, "mode": selector.NumberSelectorMode.BOX}
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(selector.NumberSelectorConfig(**config))


BOOLEAN = selector.BooleanSelector()
OBJECT = selector.ObjectSelector()
SELECTORS: dict[str, Any] = {
    "language": selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[selector.SelectOptionDict(value=code, label=label) for code, label in LANGUAGES.items()],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    ),
    # home battery
    "powerwall_battery_level_entity_id": _entity("sensor", device_class="battery"),
    "powerwall_watt_flow_entity_id": _entity("sensor", device_class="power"),
    "battery_temperature_entity_id": _entity("sensor", device_class="temperature"),
    "invert_powerwall_watt_flow_entity_id": BOOLEAN,
    "powerwall_battery_size": _number(0.1, 1000, 0.1, "kWh"),
    "powerwall_battery_level_min": _number(0, 100, 1, "%"),
    "powerwall_battery_level_max": _number(0, 100, 1, "%"),
    "battery_temperature_min": _number(-50, 100, 1, "°C"),
    "battery_temperature_max": _number(-50, 100, 1, "°C"),
    "powerwall_charging_power_limit": _number(100, 100000, 100, "W"),
    "powerwall_discharging_power": _number(100, 100000, 100, "W"),
    "powerwall_charge_discharge_loss": _number(0, 0.9, 0.01),
    "use_charge_discharge_loss_from_history": BOOLEAN,
    "battery_charging_efficiency_curve": OBJECT,
    "battery_discharging_efficiency_curve": OBJECT,
    "powerwall_wear_cost_per_kwh": _number(0, 10, 0.01, "{valuta_kwh}"),
    # consumption
    "power_consumption_entity_id": _entity("sensor", device_class="power"),
    "power_consumption_entity_id_include_powerwall_charging": BOOLEAN,
    "power_consumption_entity_id_include_powerwall_discharging": BOOLEAN,
    "ignore_consumption_from_entity_ids": _entity("sensor", device_class="power", multiple=True),
    # prices (only entities from the price integrations)
    "energidataservice_buy_entity_id": _entity("sensor", integration="energidataservice"),
    "energidataservice_sell_entity_id": _entity("sensor", integration="energidataservice"),
    "stromligning_buy_current_price": _entity("sensor", integration="stromligning"),
    "stromligning_buy_tomorrow_available": _entity("binary_sensor", integration="stromligning"),
    "stromligning_buy_forecasts": _entity("sensor", integration="stromligning"),
    "stromligning_sell_current_price": _entity("sensor", integration="stromligning"),
    "stromligning_sell_tomorrow_available": _entity("binary_sensor", integration="stromligning"),
    "stromligning_sell_forecasts": _entity("sensor", integration="stromligning"),
    "stromligning_sell_distribution": _entity("sensor", integration="stromligning"),
    "stromligning_sell_electricity_tax": _entity("sensor", integration="stromligning"),
    "stromligning_sell_nettariff": _entity("sensor", integration="stromligning"),
    "stromligning_sell_systemtariff": _entity("sensor", integration="stromligning"),
    "stromligning_sell_surcharge": _entity("sensor", integration="stromligning"),
    "refund": _number(0, 10, 0.001, "{valuta_kwh}"),
    "sell_fee": _number(0, 10, 0.001, "{valuta_kwh}"),
    # solar / weather
    "production_entity_id": _entity("sensor", device_class="power"),
    "inverter_discharging_power_limit": _number(100, 100000, 100, "W"),
    "production_price": _number(-1, 10, 0.01, "{valuta_kwh}"),
    "forecast_entity_id": _entity("sensor", integration="solcast_solar"),
    "hourly_service_entity_id": _entity("weather"),
    "daily_service_entity_id": _entity("weather"),
    # advanced / database
    "cron_interval": selector.SelectSelector(
        selector.SelectSelectorConfig(options=CRON_INTERVALS, mode=selector.SelectSelectorMode.DROPDOWN)
    ),
    "days_to_predict": _number(1, 7, 1, "d"),
    "dashboard": BOOLEAN,
    "enable_diagnostic_file_log": BOOLEAN,
    "testing_mode": BOOLEAN,
    "power_values_db_data_to_save": _number(1, 100, 1, "d"),
    "solar_available_db_data_to_save": _number(1, 100, 1),
    "kwh_avg_prices_db_data_to_save": _number(1, 100, 1, "d"),
    "charging_history_db_data_to_save": _number(1, 60, 1),  # months, see the field description
}
REQUIRED_KEYS = {
    "language",
    "powerwall_battery_level_entity_id",
    "powerwall_watt_flow_entity_id",
    "power_consumption_entity_id",
    "powerwall_battery_size",
    "powerwall_battery_level_min",
    "powerwall_battery_level_max",
    "battery_temperature_min",
    "battery_temperature_max",
    "powerwall_charging_power_limit",
    "powerwall_discharging_power",
    "powerwall_charge_discharge_loss",
    "powerwall_wear_cost_per_kwh",
    "energidataservice_buy_entity_id",
    "stromligning_buy_current_price",
    "refund",
    "sell_fee",
    "inverter_discharging_power_limit",
    "production_price",
    "days_to_predict",
    "cron_interval",
    "power_values_db_data_to_save",
    "solar_available_db_data_to_save",
    "kwh_avg_prices_db_data_to_save",
    "charging_history_db_data_to_save",
}
INT_KEYS = {
    "days_to_predict",
    "cron_interval",
    "power_values_db_data_to_save",
    "solar_available_db_data_to_save",
    "kwh_avg_prices_db_data_to_save",
    "charging_history_db_data_to_save",
}


def _form_value(key: str, value: Any) -> Any:
    if key == "cron_interval":
        return str(int(value)) if value not in (None, "") else "5"
    if key == "ignore_consumption_from_entity_ids":
        return list(value or [])
    return value


def _selector(key: str, currency: str | None) -> Any:
    """SELECTORS[key] with {valuta_kwh} in the unit replaced by the currency of Home Assistant."""
    field = SELECTORS[key]
    if isinstance(field, selector.NumberSelector) and "{valuta_kwh}" in str(field.config.get("unit_of_measurement")):
        unit = field.config["unit_of_measurement"].replace("{valuta_kwh}", f"{currency_symbol(currency) or 'kr'}/kWh")
        return selector.NumberSelector(selector.NumberSelectorConfig(**{**field.config, "unit_of_measurement": unit}))
    return field


def build_schema(keys: list[str], values: dict[str, Any], currency: str | None = None) -> vol.Schema:
    schema: dict[Any, Any] = {}
    for key in keys:
        value = _form_value(key, values.get(key))
        field = _selector(key, currency)
        if key in ENTITY_OPTION_KEYS:
            marker = vol.Required if key in REQUIRED_KEYS else vol.Optional
            schema[marker(key, description={"suggested_value": value or None})] = field
        elif key == "ignore_consumption_from_entity_ids":
            schema[vol.Optional(key, description={"suggested_value": value})] = field
        elif key in REQUIRED_KEYS:
            schema[vol.Required(key, default=value)] = field
        elif field is OBJECT:
            schema[vol.Optional(key, default=value or {})] = field
        else:
            schema[vol.Optional(key, default=bool(value))] = field
    return vol.Schema(schema)


def normalize_input(keys: list[str], user_input: dict[str, Any]) -> dict[str, Any]:
    """Convert form values to option values (cleared optional entities become '')."""
    result: dict[str, Any] = {}
    for key in keys:
        value = user_input.get(key)
        if key in ENTITY_OPTION_KEYS:
            result[key] = value or ""
        elif key == "ignore_consumption_from_entity_ids":
            result[key] = list(value or [])
        elif key in INT_KEYS:
            result[key] = int(float(value))
        elif SELECTORS[key] is BOOLEAN:
            result[key] = bool(value)
        elif isinstance(value, (int, float)):
            result[key] = float(value)
        else:
            result[key] = value
    return result


def validate_step(step: str, values: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if step == "battery_specs" and values["powerwall_battery_level_min"] >= values["powerwall_battery_level_max"]:
        errors["powerwall_battery_level_min"] = "min_above_max"
    if step == "battery_specs" and values["battery_temperature_min"] >= values["battery_temperature_max"]:
        errors["battery_temperature_min"] = "min_above_max"
    if step == "battery_specs":
        for key in ("battery_charging_efficiency_curve", "battery_discharging_efficiency_curve"):
            try:
                parse_efficiency_curve(values.get(key))
            except ValueError:
                errors[key] = "invalid_efficiency_curve"
    if step == "solar" and values.get("forecast_entity_id") and not values.get("production_entity_id"):
        errors["production_entity_id"] = "production_required"
    return errors


def options_from_legacy(legacy: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Flatten a PyScript <prefix>_config.yaml into config entry options + initial entity values."""
    options = default_options()
    for key, path in OPTION_PATHS.items():
        value = get_dict_value_with_path(legacy, path)
        if value is None:
            continue
        if key in ENTITY_OPTION_KEYS:
            options[key] = value or ""
        elif key == "ignore_consumption_from_entity_ids":
            options[key] = [e for e in value if e] if isinstance(value, list) else []
        elif key in INT_KEYS:
            options[key] = int(value)
        else:
            options[key] = value
    options["language"] = normalize_language(options.get("language"), "da")
    if str(options.get("cron_interval")) not in CRON_INTERVALS:
        options["cron_interval"] = 5
    settings = {}
    for key, path in LEGACY_SETTING_PATHS.items():
        value = get_dict_value_with_path(legacy, path)
        if isinstance(value, (int, float)):
            settings[key] = value
    return options, settings


def providers_from_options(options: dict[str, Any]) -> list[str]:
    providers = []
    if options.get("energidataservice_buy_entity_id"):
        providers.append("energidataservice")
    if options.get("stromligning_buy_current_price"):
        providers.append("stromligning")
    return providers or ["energidataservice"]

