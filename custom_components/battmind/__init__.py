"""BattMind - home battery planner for Home Assistant."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    PLATFORMS,
    SERVICE_DEBUG_INFO,
    SERVICE_ENFORCE_PLANNING,
    SERVICE_RECALC_HISTORY_TODAY,
)
from .blueprint_install import async_install_blueprints
from .coordinator import BattMindCoordinator
from .dashboard import async_remove_dashboard_file, async_setup_dashboard

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

BattMindConfigEntry = ConfigEntry[BattMindCoordinator]

SERVICE_SCHEMA = vol.Schema({vol.Optional("config_entry_id"): cv.string})


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

    async def _recalc_history(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            await coordinator.async_recalc_charging_history_today()

    async def _enforce_planning(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            coordinator.async_request_charge(force=True)

    hass.services.async_register(
        DOMAIN, SERVICE_DEBUG_INFO, _debug_info, schema=SERVICE_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(DOMAIN, SERVICE_RECALC_HISTORY_TODAY, _recalc_history, schema=SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_ENFORCE_PLANNING, _enforce_planning, schema=SERVICE_SCHEMA)
    await async_install_blueprints(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BattMindConfigEntry) -> bool:
    coordinator = BattMindCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await coordinator.async_setup()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    if coordinator.config.get("dashboard"):
        await async_setup_dashboard(hass, entry, coordinator.config, coordinator.prefix)
    coordinator.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BattMindConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_unload()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: BattMindConfigEntry) -> None:
    from .storage import BattMindStorage

    await BattMindStorage(hass, entry.entry_id).async_remove()
    await async_remove_dashboard_file(hass, entry.entry_id)


async def _async_update_listener(hass: HomeAssistant, entry: BattMindConfigEntry) -> None:
    hass.config_entries.async_schedule_reload(entry.entry_id)
