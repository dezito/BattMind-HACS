"""BattMind - home battery planner for Home Assistant."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    PLATFORMS,
    SERVICE_DEBUG_INFO,
    SERVICE_ENFORCE_PLANNING,
    SERVICE_EXPORT_DEBUG,
    SERVICE_EXPORT_DATA,
    SERVICE_IMPORT_DATA,
    SERVICE_RECALC_HISTORY_TODAY,
    SERVICE_WHAT_IF,
)
from .management.backup import BACKUP_DIR, async_export, async_import
from .management.blueprint_install import async_install_blueprints
from .coordinator import BattMindCoordinator
from .management.debug_export import DEBUG_DIR, DebugDownloadView, async_export_debug, async_notify_debug, install_log_buffer
from .management.dashboard_build import async_remove_dashboard_file, async_setup_dashboard

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

BattMindConfigEntry = ConfigEntry[BattMindCoordinator]

_WHAT_IF_SETTINGS = (
    ("battery_size", "battery_size", "kWh"),
    ("minimum_level", "minimum_level", "%"),
    ("maximum_level", "maximum_level", "%"),
    ("starting_level", "starting_level", "%"),
    ("max_charging_power", "max_charging_power", "W"),
    ("max_discharging_power", "max_discharging_power", "W"),
    ("round_trip_loss", "round_trip_loss", "%"),
    ("wear_cost_per_kwh", "wear_cost_per_kwh", "{valuta}/kWh"),
    ("usage_reserve", "usage_reserve", "%"),
    ("min_profit_per_kwh", "min_profit_per_kwh", "{valuta}/kWh"),
    ("min_total_profit", "min_total_profit", "{valuta}"),
    ("min_sell_kwh", "min_sell_kwh", "kWh"),
)

_WHAT_IF_RULES = (
    "cheapest_hour_fill_planner",
    "most_expensive_planner",
    "needed_before_max_level_planner",
    "negative_price_planner",
    "grid_charge_to_sell",
    "most_profit",
    "sell_excess_kwh_available",
    "force_powerwall_max_hours_old_discharge",
    "discharge_before_solar",
    "only_discharge_on_profit",
    "prioritize_discharge_hours_by_energy_cost",
    "optimize_solar_charging",
)


def _format_what_if_number(value: float, decimals: int | None = None) -> str:
    formatted = f"{value:.{decimals}f}" if decimals is not None else f"{value:g}"
    return formatted.replace(".", ",")


_WHAT_IF_METRICS = (
    ("house_kwh", "house_kwh", "kWh", False),
    ("house_cost", "house_cost", "{valuta}", False),
    ("battery_kwh", "battery_kwh", "kWh", False),
    ("battery_cost", "battery_cost", "{valuta}", False),
    ("solar_kwh", "solar_kwh", "kWh", False),
    ("solar_income", "solar_income", "{valuta}", True),
    ("charged_solar_kwh", "charged_solar_kwh", "kWh", False),
    ("sold_kwh", "sold_kwh", "kWh", False),
    ("sold_income", "sold_income", "{valuta}", True),
    ("wear", "wear", "{valuta}", False),
    ("paid", "paid", "{valuta}", False),
    ("earned", "earned", "{valuta}", True),
    ("bill", "bill", "{valuta}", False),
)


def _what_if_notification(coordinator: BattMindCoordinator, simulation: dict, overrides: dict) -> str:
    current = simulation["current_assumptions"]
    scenario = simulation["assumptions"]
    setting_rows = []
    for key, label_key, unit in _WHAT_IF_SETTINGS:
        if key not in overrides or current[key] == scenario[key]:
            continue
        translated_unit = f"{coordinator.t('ui.common.valuta')}/kWh" if unit == "{valuta}/kWh" else unit
        decimals = 1 if unit == "kWh" else 2 if unit == "{valuta}/kWh" else None
        setting_rows.append(
            "| **{label}** | {current} {unit} | {scenario} {unit} |".format(
                label=coordinator.t(f"ui.what_if.{label_key}"),
                current=_format_what_if_number(current[key], decimals),
                scenario=_format_what_if_number(scenario[key], decimals),
                unit=translated_unit,
            )
        )
    for key in _WHAT_IF_RULES:
        current_enabled = simulation["current_assumptions"]["rules"][key]
        scenario_enabled = simulation["assumptions"]["rules"][key]
        if current_enabled == scenario_enabled:
            continue
        setting_rows.append(
            "| **{label}** | {current} | {scenario} |".format(
                label=coordinator.t(
                    "ui.what_if.use_midnight_battery_level"
                    if key == "use_midnight_battery_level"
                    else f"charging_type.{key}.description"
                ),
                current=coordinator.t("ui.what_if.enabled" if current_enabled else "ui.what_if.disabled"),
                scenario=coordinator.t("ui.what_if.enabled" if scenario_enabled else "ui.what_if.disabled"),
            )
        )

    currency = coordinator.t("ui.common.valuta")
    current_summary = simulation["current"]["with"]
    scenario_summary = simulation["scenario"]["with"]
    metric_rows = []
    for key, label_key, unit, income in _WHAT_IF_METRICS:
        current_value = current_summary[key] if key in current_summary else simulation["current"][key]
        scenario_value = scenario_summary[key] if key in scenario_summary else simulation["scenario"][key]
        if income:
            current_value = -current_value
            scenario_value = -scenario_value
        translated_unit = currency if unit == "{valuta}" else unit
        metric_rows.append(
            "| **{label}** | {current} | {scenario} | {unit} |".format(
                label=coordinator.t(f"ui.what_if.{label_key}"),
                current=_format_what_if_number(current_value, 1 if unit == "kWh" else 2),
                scenario=_format_what_if_number(scenario_value, 1 if unit == "kWh" else 2),
                unit=translated_unit,
            )
        )

    period = coordinator.t(
        "ui.what_if.period",
        days=simulation["period"]["days"],
        hours=simulation["period"]["hours"],
    )
    data_sources = coordinator.t(
        "ui.what_if.data_sources",
        statistics=simulation["data_sources"]["long_term_statistics_samples"],
        recorder=simulation["data_sources"]["recorder_history_fallback_samples"],
    )
    price_provider = simulation.get("price_provider")
    price_provider_hours = simulation.get("price_provider_hours") or {}
    if len(price_provider_hours) > 1:
        price_provider = ", ".join(
            coordinator.t("ui.what_if.price_provider_hours", provider=name, hours=hours)
            for name, hours in price_provider_hours.items()
        )
    tariff_estimated_hours = simulation.get("tariff_estimated_hours", 0)
    tariff_unavailable_hours = simulation.get("tariff_unavailable_hours", 0)
    control_mode = coordinator.t(
        "ui.what_if.control_mode",
        mode=coordinator.t(
            "ui.what_if.optimizer_mode"
            if simulation["current_assumptions"]["control_mode"] == "optimizer"
            else "ui.what_if.rules_mode"
        ),
    )
    heading = (
        f"| | **{coordinator.t('ui.what_if.current')}** "
        f"| **{coordinator.t('ui.what_if.scenario')}** |"
    )
    result_heading = (
        f"| | **{coordinator.t('ui.what_if.current')}** "
        f"| **{coordinator.t('ui.what_if.scenario')}** "
        "| |"
    )
    lines = [period, data_sources]
    if price_provider:
        lines.append(coordinator.t("ui.what_if.price_provider", provider=price_provider))
    if tariff_estimated_hours:
        lines.append(coordinator.t("ui.what_if.tariff_estimated", hours=tariff_estimated_hours))
    if tariff_unavailable_hours:
        lines.append(coordinator.t("ui.what_if.tariff_unavailable", hours=tariff_unavailable_hours))
    lines.extend((control_mode, ""))
    if setting_rows:
        lines.extend((f"**{coordinator.t('ui.what_if.changed_settings')}**", heading, "|---|---|---|", *setting_rows, ""))
    lines.extend(
        (
            f"**{coordinator.t('ui.what_if.results')}**",
            result_heading,
            "|---|---|---|---|",
            *metric_rows,
        )
    )
    profit_difference = round(simulation["difference"]["profit"], 2)
    bill_improvement = round(-simulation["difference"]["bill"], 2)
    lines.extend(
        (
            "",
            "**{label}:** {profit} {currency}".format(
                label=coordinator.t("ui.what_if.profit_difference"),
                profit=_format_what_if_number(profit_difference, 2),
                currency=currency,
            ),
        )
    )
    if bill_improvement != profit_difference:
        lines.append(
            "**{label}:** {bill} {currency}".format(
                label=coordinator.t("ui.what_if.bill_difference"),
                bill=_format_what_if_number(bill_improvement, 2),
                currency=currency,
            )
        )
    lines.extend(("", coordinator.t("ui.what_if.note")))
    return "\n".join(lines)


SERVICE_SCHEMA = vol.Schema({vol.Optional("config_entry_id"): cv.string})
IMPORT_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("file"): cv.string,
        vol.Optional("restore_configuration", default=False): cv.boolean,
    }
)
WHAT_IF_SCHEMA = vol.Schema(
    {
        vol.Optional("config_entry_id"): cv.string,
        vol.Optional("history_days", default=365): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
        vol.Optional("battery_size"): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=1000)),
        vol.Optional("minimum_level"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Optional("maximum_level"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Optional("starting_level"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Optional("max_charging_power"): vol.All(vol.Coerce(float), vol.Range(min=100, max=100000)),
        vol.Optional("max_discharging_power"): vol.All(vol.Coerce(float), vol.Range(min=100, max=100000)),
        vol.Optional("round_trip_loss"): vol.All(vol.Coerce(float), vol.Range(min=0, max=90)),
        vol.Optional("wear_cost_per_kwh"): vol.All(vol.Coerce(float), vol.Range(min=0, max=10)),
        vol.Optional("usage_reserve"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Optional("min_profit_per_kwh"): vol.All(vol.Coerce(float), vol.Range(min=0, max=10)),
        vol.Optional("min_total_profit"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Optional("min_sell_kwh"): vol.All(vol.Coerce(float), vol.Range(min=0.2, max=1.0)),
        **{vol.Optional(key): cv.boolean for key in _WHAT_IF_RULES if key != "use_midnight_battery_level"},
    }
)


def _coordinators(hass: HomeAssistant, call: ServiceCall) -> list[BattMindCoordinator]:
    entry_id = call.data.get("config_entry_id")
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if entry_id:
        entries = [entry for entry in entries if entry.entry_id == entry_id]
        if not entries:
            raise ServiceValidationError(f"BattMind config entry {entry_id} is not loaded")
    return [entry.runtime_data for entry in entries]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the BattMind services."""

    async def _debug_info(call: ServiceCall) -> ServiceResponse:
        output = {}
        for coordinator in _coordinators(hass, call):
            output[coordinator.prefix] = await coordinator.async_debug_info()
        return {"debug_info": output} if call.return_response else None

    async def _export_debug(call: ServiceCall) -> ServiceResponse:
        files = {}
        for coordinator in _coordinators(hass, call):
            name = await async_export_debug(hass, coordinator)
            await async_notify_debug(hass, coordinator, name)
            files[coordinator.prefix] = f"{DEBUG_DIR}/{name}"
        return {"files": files} if call.return_response else None

    async def _recalc_history(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            await coordinator.async_recalc_charging_history_today()

    async def _enforce_planning(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            coordinator.async_request_charge(force=True)

    async def _what_if(call: ServiceCall) -> ServiceResponse:
        history_days = call.data["history_days"]
        overrides = {key: value for key, value in call.data.items() if key not in ("config_entry_id", "history_days")}
        output = {}
        for coordinator in _coordinators(hass, call):
            notification_id = f"{coordinator.prefix}_what_if"
            title = coordinator.t("ui.what_if.title", prefix=coordinator.prefix)
            persistent_notification.async_create(
                hass,
                coordinator.t("ui.what_if.started", days=history_days),
                title=title,
                notification_id=notification_id,
            )
            try:
                result = await coordinator.async_what_if(overrides, history_days)
            except HomeAssistantError as err:
                persistent_notification.async_create(
                    hass,
                    coordinator.t("ui.what_if.failed", error=err),
                    title=title,
                    notification_id=notification_id,
                )
                raise ServiceValidationError(str(err)) from err
            except Exception as err:
                _LOGGER.exception("Unexpected error while running the BattMind what-if simulation")
                persistent_notification.async_create(
                    hass,
                    coordinator.t("ui.what_if.failed", error=err),
                    title=title,
                    notification_id=notification_id,
                )
                raise
            if result.error:
                persistent_notification.async_create(
                    hass,
                    coordinator.t("ui.what_if.failed", error=result.error),
                    title=title,
                    notification_id=notification_id,
                )
                raise ServiceValidationError(str(result.error))
            output[coordinator.prefix] = result.value
            persistent_notification.async_create(
                hass,
                _what_if_notification(coordinator, result.value, overrides),
                title=title,
                notification_id=notification_id,
            )
        return {"simulations": output}

    async def _export_data(call: ServiceCall) -> ServiceResponse:
        files = {}
        for coordinator in _coordinators(hass, call):
            files[coordinator.prefix] = f"{BACKUP_DIR}/{await async_export(hass, coordinator.entry)}"
        return {"files": files} if call.return_response else None

    async def _import_data(call: ServiceCall) -> None:
        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(f"{call.data['config_entry_id']} is not a BattMind config entry")
        try:
            await async_import(hass, entry, call.data["file"].split("/")[-1], call.data["restore_configuration"])
        except HomeAssistantError as err:
            raise ServiceValidationError(str(err)) from err

    hass.services.async_register(
        DOMAIN, SERVICE_EXPORT_DATA, _export_data, schema=SERVICE_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(DOMAIN, SERVICE_IMPORT_DATA, _import_data, schema=IMPORT_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_DEBUG_INFO, _debug_info, schema=SERVICE_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, SERVICE_EXPORT_DEBUG, _export_debug, schema=SERVICE_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    install_log_buffer()
    if getattr(hass, "http", None) is not None:
        hass.http.register_view(DebugDownloadView())
    hass.services.async_register(DOMAIN, SERVICE_RECALC_HISTORY_TODAY, _recalc_history, schema=SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_ENFORCE_PLANNING, _enforce_planning, schema=SERVICE_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_WHAT_IF,
        _what_if,
        schema=WHAT_IF_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    await async_install_blueprints(hass)
    return True


async def _async_run_after_home_assistant_started(
    hass: HomeAssistant,
    entry: ConfigEntry,
    action: Callable[[], Coroutine[Any, Any, None]],
    name: str,
) -> None:
    """Run post-setup work after every integration has had its startup turn."""
    if hass.state is CoreState.running:
        await action()
        return

    async def _async_run() -> None:
        # Let all other homeassistant_started listeners run before inspecting
        # their entities. This keeps generated dashboards independent of setup order.
        await asyncio.sleep(0)
        await action()

    @callback
    def _started(_event: Event) -> None:
        entry.async_create_task(hass, _async_run(), name=name)

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _started))


async def async_setup_entry(hass: HomeAssistant, entry: BattMindConfigEntry) -> bool:
    coordinator = BattMindCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await coordinator.async_setup()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    if coordinator.config.get("dashboard"):
        await _async_run_after_home_assistant_started(
            hass,
            entry,
            lambda: async_setup_dashboard(hass, entry, coordinator.config, coordinator.prefix),
            f"battmind_{coordinator.prefix}_dashboard_startup",
        )
    coordinator.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BattMindConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_unload()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: BattMindConfigEntry) -> None:
    from .management.storage import BattMindStorage

    # the data can be imported again (config flow of a new entry, or import_data)
    try:
        await async_export(hass, entry, "deleted")
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Could not back up BattMind %s, its databases are kept in .storage", entry.title)
        return
    await BattMindStorage(hass, entry.entry_id).async_remove()
    await async_remove_dashboard_file(hass, entry.entry_id)


async def _async_update_listener(hass: HomeAssistant, entry: BattMindConfigEntry) -> None:
    hass.config_entries.async_schedule_reload(entry.entry_id)
