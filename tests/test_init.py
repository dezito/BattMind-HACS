"""Setup, entities, services and unload of a BattMind entry."""
from __future__ import annotations

from unittest.mock import patch

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.battmind.const import DOMAIN, NUMBER_SETTINGS, SELECT_SETTINGS, SENSOR_KEYS, SWITCH_SETTINGS, TEXT_SETTINGS

from .conftest import CONSUMPTION, PREFIX, async_advance, async_unload_and_join, set_source_states


async def test_setup_creates_entities(hass: HomeAssistant, loaded_entry) -> None:
    assert loaded_entry.state is ConfigEntryState.LOADED
    coordinator = loaded_entry.runtime_data
    assert coordinator.started, coordinator.data.get("current_charging_rule")

    ent_reg = er.async_get(hass)
    expected = [
        *(f"sensor.{PREFIX}_{key}" for key in SENSOR_KEYS),
        *(f"switch.{PREFIX}_{key}" for key in SWITCH_SETTINGS),
        *(f"number.{PREFIX}_{key}" for key in NUMBER_SETTINGS),
        *(f"select.{PREFIX}_{key}" for key in SELECT_SETTINGS),
        *(f"text.{PREFIX}_{key}" for key in TEXT_SETTINGS),
    ]
    missing = [entity_id for entity_id in expected if ent_reg.async_get(entity_id) is None]
    assert not missing
    for entity_id in expected:
        assert hass.states.get(entity_id) is not None, entity_id


async def test_removed_entities_are_cleaned_up(hass: HomeAssistant, mock_entry) -> None:
    """Removed settings/buttons: their old registry entries are removed."""
    ent_reg = er.async_get(hass)
    mock_entry.add_to_hass(hass)
    old = [
        ent_reg.async_get_or_create(platform, DOMAIN, f"{mock_entry.entry_id}_{key}", config_entry=mock_entry)
        for platform, key in (("switch", "deactivate_script"), ("switch", "optimizer_shadow_plan"), ("button", "restart_script"))
    ]
    set_source_states(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert all(ent_reg.async_get(entry.entity_id) is None for entry in old)
    await async_unload_and_join(hass, mock_entry)


async def test_startup_publishes_plan(hass: HomeAssistant, loaded_entry) -> None:
    rule = hass.states.get(f"sensor.{PREFIX}_current_charging_rule")
    assert rule is not None
    assert "⛔" not in rule.state, rule.state
    assert loaded_entry.runtime_data.last_job.get("error") is None
    assert hass.states.get(f"sensor.{PREFIX}_overview").state not in ("unknown", "unavailable")


async def test_enforce_planning_service(hass: HomeAssistant, loaded_entry) -> None:
    await hass.services.async_call(DOMAIN, "enforce_planning", {"config_entry_id": loaded_entry.entry_id}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded_entry.runtime_data.last_job["job"] == "charge_if_needed"
    assert loaded_entry.runtime_data.last_job["error"] is None


async def test_debug_info_service(hass: HomeAssistant, loaded_entry) -> None:
    response = await hass.services.async_call(DOMAIN, "debug_info", {}, blocking=True, return_response=True)
    assert PREFIX in response["debug_info"]
    assert response["debug_info"][PREFIX]


async def test_switch_triggers_replanning(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    entity_id = f"switch.{PREFIX}_use_midnight_battery_level"  # off by default
    await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
    assert hass.states.get(entity_id).state == "on"
    assert coordinator.settings["use_midnight_battery_level"] is True
    assert coordinator._settings_debounce is not None  # 5 s debounce before the new plan
    coordinator.last_job.clear()

    await async_advance(hass, 6)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert coordinator._settings_debounce is None
    assert coordinator.last_job["job"] == "charge_if_needed"


async def test_unload(hass: HomeAssistant, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    await async_unload_and_join(hass, loaded_entry)
    assert loaded_entry.state is ConfigEntryState.NOT_LOADED
    assert not coordinator._unsubs
    assert hass.states.get(f"sensor.{PREFIX}_current_charging_rule").state == "unavailable"


async def test_settings_and_databases_survive_reload(hass: HomeAssistant, loaded_entry, hass_storage) -> None:
    entry_id = loaded_entry.entry_id
    await hass.services.async_call("switch", "turn_on", {"entity_id": f"switch.{PREFIX}_cheapest_hour_fill_up"}, blocking=True)
    await hass.services.async_call("number", "set_value", {"entity_id": f"number.{PREFIX}_cheap_price_periods", "value": 4}, blocking=True)
    # startup saves every database once
    for name in ("charging_history", "power_values", "kwh_avg_prices", "solar_available"):
        assert f"{DOMAIN}.{entry_id}.{name}" in hass_storage

    old_coordinator = loaded_entry.runtime_data
    assert await hass.config_entries.async_reload(entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.async_add_executor_job(old_coordinator._executor.shutdown, True)

    coordinator = loaded_entry.runtime_data
    assert coordinator is not old_coordinator and coordinator.started
    assert hass.states.get(f"switch.{PREFIX}_cheapest_hour_fill_up").state == "on"
    assert float(hass.states.get(f"number.{PREFIX}_cheap_price_periods").state) == 4
    assert coordinator.settings["cheap_price_periods"] == 4


async def test_missing_entity_waits_and_notifies(hass: HomeAssistant, mock_entry) -> None:
    """A configured entity that does not exist: retry after 2 minutes, then a persistent notification."""
    set_source_states(hass)
    hass.states.async_remove(CONSUMPTION)
    mock_entry.add_to_hass(hass)
    with patch.object(persistent_notification, "async_create", wraps=persistent_notification.async_create) as create:
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()
        assert not mock_entry.runtime_data.started  # waiting in the 2 minute retry

        await async_advance(hass, 121)
        await hass.async_block_till_done(wait_background_tasks=True)

    assert mock_entry.runtime_data.started  # starts anyway, like the PyScript version
    ids = [call.kwargs.get("notification_id") for call in create.call_args_list]
    assert f"{PREFIX}_validation_procedure_missing_entity_ids" in ids
    message = next(call.args[1] for call in create.call_args_list if call.kwargs.get("notification_id", "").endswith("missing_entity_ids"))
    assert CONSUMPTION in message
    await async_unload_and_join(hass, mock_entry)
