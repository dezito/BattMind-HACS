"""Config flow for BattMind.

Everything from <prefix>_config.yaml is configured here, split into small pages with
help texts. The settings that the dashboard changes at runtime (planner switches,
thresholds, ...) are entities.
"""
from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .backup import BACKUP_DIR, async_export, async_import, async_load_backup, backup_dir, list_backups
from .const import (
    CONF_ENTITY_PREFIX,
    DEFAULT_ENTITY_PREFIX,
    DOMAIN,
    ENTITY_OPTION_KEYS,
    FLOW_STEPS,
    LANGUAGES,
    OPTION_PATHS,
    OPTIONS_MENU,
    PRICE_PROVIDER_KEYS,
    SENSOR_KEYS,
    currency_symbol,
    default_options,
    normalize_language,
)
from .helpers import get_dict_value_with_path
from .efficiency import parse_efficiency_curve
from .storage import find_legacy_sources, load_legacy_config

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


class BattMindFlowPages:
    """Pages shared by the config flow and the options flow.

    The subclass sets ``self._options`` (flat option dict), ``self._queue`` (remaining
    page ids) and implements ``_async_finish``.
    """

    _options: dict[str, Any]
    _queue: list[str]
    _providers: list[str]

    def _page_enabled(self, step: str) -> bool:
        if step == "prices_energidataservice":
            return "energidataservice" in self._providers
        if step == "prices_stromligning":
            return "stromligning" in self._providers
        if step == "prices_stromligning_tariffs":
            return "stromligning" in self._providers and bool(self._options.get("stromligning_sell_current_price"))
        if step == "weather":
            return bool(self._options.get("production_entity_id"))
        return True

    async def _async_next(self) -> ConfigFlowResult:
        while self._queue:
            step = self._queue.pop(0)
            if self._page_enabled(step):
                return await getattr(self, f"async_step_{step}")()
        return await self._async_finish()

    async def _async_finish(self) -> ConfigFlowResult:
        raise NotImplementedError

    def _placeholders(self) -> dict[str, str]:
        return {}

    async def _async_page(self, step: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        keys = FLOW_STEPS[step]
        errors: dict[str, str] = {}
        values_for_form = dict(self._options)
        if user_input is not None:
            values = normalize_input(keys, user_input)
            errors = validate_step(step, values)
            if not errors:
                self._options.update(values)
                return await self._async_next()
            values_for_form.update(values)
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id=step,
            data_schema=build_schema(keys, values_for_form, self.hass.config.currency),  # type: ignore[attr-defined]
            errors=errors,
            description_placeholders={**self._placeholders(), "eds_url": ENERGIDATASERVICE_URL},
        )

    async def async_step_price_provider(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            providers = [p for p in user_input.get("price_providers", []) if p in PRICE_PROVIDERS]
            if not providers:
                errors["base"] = "no_price_provider"
            else:
                self._providers = providers
                for provider, keys in PRICE_PROVIDER_KEYS.items():
                    if provider not in providers:
                        for key in keys:
                            self._options[key] = ""
                return await self._async_next()
        schema = vol.Schema(
            {
                vol.Required("price_providers", default=self._providers): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=PRICE_PROVIDERS,
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                        translation_key="price_providers",
                    )
                )
            }
        )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="price_provider", data_schema=schema, errors=errors, description_placeholders=self._placeholders()
        )

    async def async_step_general(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("general", user_input)

    async def async_step_battery_entities(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("battery_entities", user_input)

    async def async_step_battery_specs(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("battery_specs", user_input)

    async def async_step_consumption(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("consumption", user_input)

    async def async_step_prices_energidataservice(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("prices_energidataservice", user_input)

    async def async_step_prices_stromligning(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("prices_stromligning", user_input)

    async def async_step_prices_stromligning_tariffs(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("prices_stromligning_tariffs", user_input)

    async def async_step_price_fees(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("price_fees", user_input)

    async def async_step_solar(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("solar", user_input)

    async def async_step_weather(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("weather", user_input)

    async def async_step_advanced(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("advanced", user_input)

    async def async_step_database(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("database", user_input)


class BattMindConfigFlow(BattMindFlowPages, ConfigFlow, domain=DOMAIN):
    """Handle the BattMind config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._prefix = DEFAULT_ENTITY_PREFIX
        self._options: dict[str, Any] = default_options()
        self._initial_settings: dict[str, Any] = {}
        self._legacy_sources: dict[str, list[str]] = {}
        self._legacy_prefix: str | None = None
        self._backups: dict[str, str] = {}
        self._restore_backup: str | None = None
        self._queue: list[str] = []
        self._providers: list[str] = ["energidataservice"]

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return BattMindOptionsFlow()

    def _placeholders(self) -> dict[str, str]:
        return {"prefix": self._prefix}

    def _foreign_entities(self, prefix: str) -> list[str]:
        """Entity ids this prefix would create that already belong to something else (e.g. PyScript)."""
        ent_reg = er.async_get(self.hass)
        found = []
        for key in SENSOR_KEYS:
            entity_id = f"sensor.{prefix}_{key}"
            entry = ent_reg.async_get(entity_id)
            if (entry is not None and entry.platform != DOMAIN) or (entry is None and self.hass.states.get(entity_id)):
                found.append(entity_id)
        return found

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            prefix = user_input[CONF_ENTITY_PREFIX].strip().lower()
            used = {entry.data.get(CONF_ENTITY_PREFIX) for entry in self._async_current_entries()}
            if not PREFIX_RE.match(prefix):
                errors[CONF_ENTITY_PREFIX] = "invalid_prefix"
            elif prefix in used:
                errors[CONF_ENTITY_PREFIX] = "prefix_in_use"
            elif self._foreign_entities(prefix):
                errors[CONF_ENTITY_PREFIX] = "prefix_entities_exist"
            else:
                self._prefix = prefix
                self._options["language"] = user_input.get("language", self._options["language"])
                self._queue = list(CONFIG_FLOW_STEPS)
                self._backups = await self.hass.async_add_executor_job(list_backups, backup_dir(self.hass))
                if self._backups:
                    return await self.async_step_restore_backup()
                return await self._async_legacy_or_next()

        language = normalize_language(self.hass.config.language)
        schema = vol.Schema(
            {
                vol.Required(CONF_ENTITY_PREFIX, default=DEFAULT_ENTITY_PREFIX): selector.TextSelector(),
                vol.Required("language", default=language): SELECTORS["language"],
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def _async_legacy_or_next(self) -> ConfigFlowResult:
        self._legacy_sources = await self.hass.async_add_executor_job(find_legacy_sources, self.hass.config.config_dir)
        if self._legacy_sources:
            return await self.async_step_import_legacy()
        return await self._async_next()

    async def async_step_restore_backup(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask which exported or deleted BattMind entry (if any) should be restored."""
        if user_input is not None:
            name = user_input.get("backup", IMPORT_NONE)
            if name not in self._backups:
                return await self._async_legacy_or_next()
            backup = await async_load_backup(self.hass, name)
            self._restore_backup = name
            language = self._options["language"]
            self._options = {**default_options(), **(backup.get("options") or {}), "language": language}
            self._providers = providers_from_options(self._options)
            return await self._async_next()

        schema = vol.Schema(
            {
                vol.Required("backup", default=next(iter(self._backups))): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            *(selector.SelectOptionDict(value=name, label=label) for name, label in self._backups.items()),
                            selector.SelectOptionDict(value=IMPORT_NONE, label=IMPORT_NONE),
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                        translation_key="restore_backup",
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="restore_backup",
            data_schema=schema,
            description_placeholders={"prefix": self._prefix, "folder": BACKUP_DIR},
        )

    async def async_step_import_legacy(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask which PyScript installation (if any) should be imported."""
        if user_input is not None:
            source = user_input.get("import_source", IMPORT_NONE)
            if source in self._legacy_sources:
                self._legacy_prefix = source
                language = self._options["language"]
                legacy = await self.hass.async_add_executor_job(load_legacy_config, self.hass.config.config_dir, source)
                if legacy:
                    self._options, self._initial_settings = options_from_legacy(legacy)
                    self._options["language"] = language
                    self._providers = providers_from_options(self._options)
                # PyScript used the dashboard input_number, the yaml value was only its initial value
                if (state := self.hass.states.get(f"input_number.{source}_solar_sell_fixed_price")) is not None:
                    try:
                        self._options["production_price"] = float(state.state)
                    except ValueError:
                        pass
            return await self._async_next()

        sources = list(self._legacy_sources)
        default = self._prefix if self._prefix in sources else sources[0]
        schema = vol.Schema(
            {
                vol.Required("import_source", default=default): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[*sources, IMPORT_NONE],
                        mode=selector.SelectSelectorMode.LIST,
                        translation_key="import_source",
                    )
                )
            }
        )
        files = "\n".join(f"- **{source}**: {', '.join(names)}" for source, names in self._legacy_sources.items())
        return self.async_show_form(
            step_id="import_legacy",
            data_schema=schema,
            description_placeholders={"prefix": self._prefix, "files": files},
        )

    async def _async_finish(self) -> ConfigFlowResult:
        title = "BattMind" if self._prefix == DEFAULT_ENTITY_PREFIX else f"BattMind ({self._prefix})"
        return self.async_create_entry(
            title=title,
            data={
                CONF_ENTITY_PREFIX: self._prefix,
                "legacy_prefix": self._legacy_prefix,
                "initial_settings": self._initial_settings,
                "restore_backup": self._restore_backup,
            },
            options=self._options,
        )


class BattMindOptionsFlow(BattMindFlowPages, OptionsFlow):
    """Change the BattMind configuration (the entry is reloaded when saved)."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._queue: list[str] = []
        self._providers: list[str] = []

    def _placeholders(self) -> dict[str, str]:
        return {"prefix": self.config_entry.data.get(CONF_ENTITY_PREFIX, "")}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._options = {**default_options(), **self.config_entry.options}
        self._options["language"] = normalize_language(self._options.get("language"))
        self._providers = providers_from_options(self._options)
        return self.async_show_menu(step_id="init", menu_options=[*(f"menu_{group}" for group in OPTIONS_MENU), "backup"])

    async def async_step_backup(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Export the data now, or replace it with a backup (the entry is reloaded)."""
        if user_input is not None:
            choice = user_input.get("backup", BACKUP_EXPORT)
            if choice == BACKUP_EXPORT:
                name = await async_export(self.hass, self.config_entry)
                return self.async_abort(reason="backup_exported", description_placeholders={"file": f"{BACKUP_DIR}/{name}"})
            try:
                await async_import(self.hass, self.config_entry, choice, bool(user_input.get("restore_configuration")))
            except HomeAssistantError as err:
                return self.async_abort(reason="backup_failed", description_placeholders={"error": str(err)})
            return self.async_abort(reason="backup_imported", description_placeholders={"file": f"{BACKUP_DIR}/{choice}"})

        backups = await self.hass.async_add_executor_job(list_backups, backup_dir(self.hass))
        schema = vol.Schema(
            {
                vol.Required("backup", default=BACKUP_EXPORT): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=BACKUP_EXPORT, label=BACKUP_EXPORT),
                            *(selector.SelectOptionDict(value=name, label=label) for name, label in backups.items()),
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                        translation_key="backup_action",
                    )
                ),
                vol.Required("restore_configuration", default=False): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="backup", data_schema=schema, description_placeholders={**self._placeholders(), "folder": BACKUP_DIR}
        )

    async def _async_menu(self, group: str) -> ConfigFlowResult:
        self._queue = list(OPTIONS_MENU[group])
        return await self._async_next()

    async def async_step_menu_general(self, user_input=None) -> ConfigFlowResult:
        return await self._async_menu("general")

    async def async_step_menu_battery(self, user_input=None) -> ConfigFlowResult:
        return await self._async_menu("battery")

    async def async_step_menu_consumption(self, user_input=None) -> ConfigFlowResult:
        return await self._async_menu("consumption")

    async def async_step_menu_prices(self, user_input=None) -> ConfigFlowResult:
        return await self._async_menu("prices")

    async def async_step_menu_solar(self, user_input=None) -> ConfigFlowResult:
        return await self._async_menu("solar")

    async def async_step_menu_advanced(self, user_input=None) -> ConfigFlowResult:
        return await self._async_menu("advanced")

    async def _async_finish(self) -> ConfigFlowResult:
        return self.async_create_entry(data=self._options)
