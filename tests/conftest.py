"""Shared fixtures: simulated source entities and a loaded BattMind entry."""
from __future__ import annotations

import datetime
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.battmind.const import CONF_ENTITY_PREFIX, DOMAIN, default_options

PREFIX = "bmtest"

BATTERY_LEVEL = "sensor.home_battery_level"
BATTERY_FLOW = "sensor.home_battery_power"
CONSUMPTION = "sensor.house_consumption"
PRODUCTION = "sensor.solar_production"
PRICE = "sensor.energi_data_service"

# Options as the config flow stores them (energidataservice + solar, no solcast/weather)
MOCK_OPTIONS: dict[str, Any] = {
    **default_options(),
    "language": "da",
    "powerwall_battery_level_entity_id": BATTERY_LEVEL,
    "powerwall_watt_flow_entity_id": BATTERY_FLOW,
    "power_consumption_entity_id": CONSUMPTION,
    "energidataservice_buy_entity_id": PRICE,
    "production_entity_id": PRODUCTION,
    "dashboard": False,
}


def price_rows(day: datetime.date, base: float) -> list[dict[str, Any]]:
    """24 hourly Energi Data Service rows: cheap at night, expensive at 17-20."""
    tz = dt_util.get_default_time_zone()
    rows = []
    for hour in range(24):
        price = base + (1.5 if 17 <= hour <= 20 else 0.6 if 7 <= hour <= 16 else 0.0) + hour / 100
        start = datetime.datetime.combine(day, datetime.time(hour), tzinfo=tz)
        rows.append({"hour": start.isoformat(), "price": round(price, 3)})
    return rows


def set_source_states(hass: HomeAssistant, *, battery_level: float = 50, solar_w: float = 0) -> None:
    """Create the entities a real installation gets from the inverter/battery/price integrations."""
    today = dt_util.now().date()
    tomorrow = today + datetime.timedelta(days=1)
    hass.states.async_set(BATTERY_LEVEL, str(battery_level), {"unit_of_measurement": "%", "device_class": "battery"})
    hass.states.async_set(BATTERY_FLOW, "0", {"unit_of_measurement": "W", "device_class": "power"})
    hass.states.async_set(CONSUMPTION, "650", {"unit_of_measurement": "W", "device_class": "power"})
    hass.states.async_set(PRODUCTION, str(solar_w), {"unit_of_measurement": "W", "device_class": "power"})
    hass.states.async_set(
        PRICE,
        "1.2",
        {
            "unit_of_measurement": "kr/kWh",
            "raw_today": price_rows(today, 1.0),
            "raw_tomorrow": price_rows(tomorrow, 0.8),
            "tomorrow_valid": True,
            "forecast": [
                *price_rows(tomorrow + datetime.timedelta(days=1), 0.9),
                *price_rows(tomorrow + datetime.timedelta(days=2), 1.1),
            ],
        },
    )


def form_defaults(result) -> dict[str, Any]:
    """The values a user submits by just pressing the button (defaults + suggested values)."""
    values = {}
    for key in result["data_schema"].schema:
        if (suggested := (key.description or {}).get("suggested_value")) not in (None, ""):
            values[str(key)] = suggested
        elif callable(key.default):
            values[str(key)] = key.default()
    return values


async def async_advance(hass: HomeAssistant, seconds: float) -> None:
    """Run the timers due within <seconds> (debounce, retry sleeps) without freezing the clock.

    The freezer fixture would turn datetime.now() into a FakeDatetime subclass, which the
    planner's strict type checks (in_between, ported unchanged from PyScript) reject.
    """
    async_fire_time_changed(hass, dt_util.utcnow() + datetime.timedelta(seconds=seconds))
    await hass.async_block_till_done()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow custom_components/battmind to be loaded in every test."""
    yield


@pytest.fixture
def mock_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"BattMind ({PREFIX})",
        data={CONF_ENTITY_PREFIX: PREFIX, "legacy_prefix": None, "initial_settings": {}},
        options=dict(MOCK_OPTIONS),
    )


async def async_unload_and_join(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Unload and wait for the worker thread (the HA test harness fails on lingering threads)."""
    coordinator = entry.runtime_data
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await hass.async_add_executor_job(coordinator._executor.shutdown, True)


@pytest.fixture
async def loaded_entry(hass: HomeAssistant, mock_entry: MockConfigEntry) -> AsyncGenerator[MockConfigEntry]:
    """BattMind set up against the simulated entities, startup sequence finished."""
    set_source_states(hass)
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    yield mock_entry
    if mock_entry.state is ConfigEntryState.LOADED:
        await async_unload_and_join(hass, mock_entry)
