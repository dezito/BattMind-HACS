"""The bundled battery control blueprint runs the right actions on the right triggers."""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
import shutil
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.battmind.blueprint_install import SOURCE
from custom_components.battmind.const import DOMAIN

ACTION_SENSOR = "sensor.battmind_powerwall_action"
ACTIONS = ["battery_stopped", "grid_charging", "force_discharge", "discharge_allowed", "force_solar_only_charging", "blocked_solar_charge"]


@pytest.fixture(autouse=True)
def config_dir(hass: HomeAssistant, tmp_path: Path) -> None:
    hass.config.config_dir = str(tmp_path)
    target = tmp_path / "blueprints" / "automation" / DOMAIN
    target.mkdir(parents=True)
    shutil.copy(SOURCE / "battery_control.yaml", target / "battery_control.yaml")


@pytest.fixture
def ran(hass: HomeAssistant) -> list[str]:
    events: list[str] = []

    @callback
    def _record(event: Event) -> None:
        events.append(event.data["action"])

    hass.bus.async_listen("battmind_test", _record)
    return events


async def _setup(hass: HomeAssistant, freezer: FrozenDateTimeFactory, default_repeat: bool = False, **inputs: Any) -> None:
    if not default_repeat:
        # the tests choose the repeat themselves (the blueprint repeats every 5 minutes by default)
        inputs.setdefault("repeat_interval", {"hours": 0, "minutes": 0, "seconds": 0})
    freezer.move_to(datetime(2026, 10, 23, 10, 13, 30, tzinfo=dt_util.get_default_time_zone()))
    hass.states.async_set(ACTION_SENSOR, "discharge_allowed")
    actions = {
        name: [{"event": "battmind_test", "event_data": {"action": name}}] for name in ACTIONS
    }
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "use_blueprint": {
                    "path": f"{DOMAIN}/battery_control.yaml",
                    "input": {"battmind_action": ACTION_SENSOR, **actions, **inputs},
                }
            }
        },
    )
    await hass.async_block_till_done()


async def _settle() -> None:
    """Let the automation run until it waits. async_block_till_done would wait for the repeat loop."""
    for _ in range(20):
        await asyncio.sleep(0)


async def _wait(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: int, step: int = 1) -> None:
    """Let the time pass in small steps, so every delay finishes on time."""
    for _ in range(seconds // step):
        freezer.tick(step)
        async_fire_time_changed(hass)
        await _settle()


async def _stop(hass: HomeAssistant) -> None:
    """End a running repeat loop, so it does not linger after the test."""
    await hass.services.async_call("automation", "turn_off", {"entity_id": "all"}, blocking=True)
    await hass.async_block_till_done()


async def test_action_change_runs_action(hass: HomeAssistant, freezer: FrozenDateTimeFactory, ran: list[str]) -> None:
    await _setup(hass, freezer)
    hass.states.async_set(ACTION_SENSOR, "grid_charging")
    await _settle()
    assert ran == ["grid_charging"]


async def test_temperature_stop_runs_stop_action(hass: HomeAssistant, freezer: FrozenDateTimeFactory, ran: list[str]) -> None:
    await _setup(hass, freezer)
    hass.states.async_set(ACTION_SENSOR, "battery_stopped")
    await _settle()
    assert ran == ["battery_stopped"]


async def test_repeat_off(hass: HomeAssistant, freezer: FrozenDateTimeFactory, ran: list[str]) -> None:
    await _setup(hass, freezer)
    hass.states.async_set(ACTION_SENSOR, "grid_charging")
    await _settle()
    await _wait(hass, freezer, 3600, step=60)
    assert ran == ["grid_charging"]


async def test_repeat_every_5_minutes_by_default(hass: HomeAssistant, freezer: FrozenDateTimeFactory, ran: list[str]) -> None:
    """A lost command or an inverter that changed its mode is put right within 5 minutes."""
    await _setup(hass, freezer, default_repeat=True)
    hass.states.async_set(ACTION_SENSOR, "grid_charging")
    await _settle()
    await _wait(hass, freezer, 900, step=30)
    assert ran == ["grid_charging"] * 4

    await _stop(hass)


async def test_runs_on_reload_by_default(hass: HomeAssistant, freezer: FrozenDateTimeFactory, ran: list[str]) -> None:
    await _setup(hass, freezer, default_repeat=True)
    hass.bus.async_fire("automation_reloaded")
    await _settle()
    assert ran == ["discharge_allowed"]

    await _stop(hass)


async def test_repeat_interval_seconds(hass: HomeAssistant, freezer: FrozenDateTimeFactory, ran: list[str]) -> None:
    await _setup(hass, freezer, repeat_interval={"hours": 0, "minutes": 0, "seconds": 10})
    hass.states.async_set(ACTION_SENSOR, "force_discharge")
    await _settle()
    await _wait(hass, freezer, 35)
    # at once and after 10, 20 and 30 seconds
    assert ran == ["force_discharge"] * 4
    await _stop(hass)


async def test_repeat_interval_minutes_and_hours(hass: HomeAssistant, freezer: FrozenDateTimeFactory, ran: list[str]) -> None:
    await _setup(hass, freezer, repeat_interval={"hours": 1, "minutes": 30, "seconds": 0})
    hass.states.async_set(ACTION_SENSOR, "force_discharge")
    await _settle()
    await _wait(hass, freezer, 3 * 3600 + 60, step=60)
    # at once and after 1:30 and 3:00
    assert ran == ["force_discharge"] * 3
    await _stop(hass)


async def test_new_action_restarts_the_repeat(hass: HomeAssistant, freezer: FrozenDateTimeFactory, ran: list[str]) -> None:
    await _setup(hass, freezer, repeat_interval={"seconds": 10})
    hass.states.async_set(ACTION_SENSOR, "force_discharge")
    await _settle()
    await _wait(hass, freezer, 5)
    hass.states.async_set(ACTION_SENSOR, "grid_charging")
    await _settle()
    await _wait(hass, freezer, 12)
    assert ran == ["force_discharge", "grid_charging", "grid_charging"]
    await _stop(hass)


async def test_repeat_stops_when_sensor_unavailable(hass: HomeAssistant, freezer: FrozenDateTimeFactory, ran: list[str]) -> None:
    await _setup(hass, freezer, repeat_interval={"seconds": 10})
    hass.states.async_set(ACTION_SENSOR, "force_discharge")
    await _settle()
    hass.states.async_set(ACTION_SENSOR, "unavailable")
    await _settle()
    await _wait(hass, freezer, 60)
    assert ran == ["force_discharge"]


async def test_repeat_starts_on_reload(hass: HomeAssistant, freezer: FrozenDateTimeFactory, ran: list[str]) -> None:
    await _setup(hass, freezer, repeat_interval={"seconds": 10})
    hass.bus.async_fire("automation_reloaded")
    await _settle()
    await _wait(hass, freezer, 15)
    assert ran == ["discharge_allowed"] * 2
    await _stop(hass)


async def test_no_run_on_reload_without_repeat(hass: HomeAssistant, freezer: FrozenDateTimeFactory, ran: list[str]) -> None:
    await _setup(hass, freezer)
    hass.bus.async_fire("automation_reloaded")
    await _settle()
    assert ran == []


async def test_custom_trigger(hass: HomeAssistant, freezer: FrozenDateTimeFactory, ran: list[str]) -> None:
    await _setup(hass, freezer, extra_triggers=[{"trigger": "event", "event_type": "modbus_tick"}])
    hass.bus.async_fire("modbus_tick")
    await _settle()
    assert ran == ["discharge_allowed"]


EV_POWER = "sensor.ev_charger_power"
EV_ALLOWED = "switch.cjp_tesla_powerwall_discharge_above_needed"
EV_MIN_LEVEL = "number.cjp_tesla_ev_charge_after_powerwall_battery_level"
HOME_BATTERY = "sensor.powerwall_battery_level"
EV_INPUTS = {"ev_chargers": [EV_POWER], "ev_charging": [{"event": "battmind_test", "event_data": {"action": "ev_charging"}}]}


async def _car_charging(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    hass.states.async_set(EV_POWER, "7000")
    await _wait(hass, freezer, 31)


async def test_car_charging_blocks_discharge(hass: HomeAssistant, freezer: FrozenDateTimeFactory, ran: list[str]) -> None:
    hass.states.async_set(EV_POWER, "0")
    await _setup(hass, freezer, **EV_INPUTS)
    await _car_charging(hass, freezer)
    assert ran == ["ev_charging"]


async def test_discharge_into_car_allowed_by_entity(hass: HomeAssistant, freezer: FrozenDateTimeFactory, ran: list[str]) -> None:
    hass.states.async_set(EV_POWER, "0")
    hass.states.async_set(EV_ALLOWED, "on")
    await _setup(hass, freezer, **EV_INPUTS, ev_discharge_allowed=EV_ALLOWED)
    await _car_charging(hass, freezer)
    assert ran == ["discharge_allowed"]
    # turned off while the car charges: back to the car action
    hass.states.async_set(EV_ALLOWED, "off")
    await _settle()
    assert ran == ["discharge_allowed", "ev_charging"]


async def test_discharge_into_car_only_above_level(hass: HomeAssistant, freezer: FrozenDateTimeFactory, ran: list[str]) -> None:
    hass.states.async_set(EV_POWER, "0")
    hass.states.async_set(EV_ALLOWED, "on")
    hass.states.async_set(EV_MIN_LEVEL, "40")
    hass.states.async_set(HOME_BATTERY, "35")
    await _setup(hass, freezer, **EV_INPUTS, ev_discharge_allowed=EV_ALLOWED, ev_discharge_min_level=EV_MIN_LEVEL, home_battery_level=HOME_BATTERY)
    await _car_charging(hass, freezer)
    assert ran == ["ev_charging"]
    hass.states.async_set(HOME_BATTERY, "60")
    await _settle()
    assert ran == ["ev_charging", "discharge_allowed"]
    # changes while the car is not charging do not run anything
    hass.states.async_set(EV_POWER, "0")
    await _wait(hass, freezer, 31)
    hass.states.async_set(HOME_BATTERY, "20")
    await _settle()
    assert ran == ["ev_charging", "discharge_allowed", "discharge_allowed"]
