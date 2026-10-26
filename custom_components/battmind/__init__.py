"""BattMind - home battery planner for Home Assistant."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import logging
from typing import Any

import voluptuous as vol

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
from .backup import BACKUP_DIR, async_export, async_import
from .blueprint_install import async_install_blueprints
from .coordinator import BattMindCoordinator
from .debug_export import DEBUG_DIR, DebugDownloadView, async_export_debug, async_notify_debug, install_log_buffer
from .dashboard import async_remove_dashboard_file, async_setup_dashboard

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

BattMindConfigEntry = ConfigEntry[BattMindCoordinator]

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
            try:
                result = await coordinator.async_what_if(overrides, history_days)
            except HomeAssistantError as err:
                raise ServiceValidationError(str(err)) from err
            if result.error:
                raise ServiceValidationError(str(result.error))
            output[coordinator.prefix] = result.value
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
    from .storage import BattMindStorage

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
