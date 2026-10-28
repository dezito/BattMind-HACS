"""Setup, entities, services and unload of a BattMind entry."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.battmind import _async_run_after_home_assistant_started, _what_if_notification
from custom_components.battmind.const import DOMAIN, NUMBER_SETTINGS, SELECT_SETTINGS, SENSOR_KEYS, SWITCH_SETTINGS, TEXT_SETTINGS
from custom_components.battmind.engine import JobResult

from .conftest import CONSUMPTION, PREFIX, async_advance, async_unload_and_join, set_source_states


def test_what_if_new_service_fields_have_entity_names_and_descriptions() -> None:
    component = Path(__file__).parents[1] / "custom_components" / "battmind"
    fields = yaml.safe_load((component / "services.yaml").read_text(encoding="utf-8"))["what_if"]["fields"]
    new_fields = {
        "min_profit_per_kwh",
        "min_total_profit",
        "min_sell_kwh",
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
        "optimize_solar_charging",
    }

    for key in new_fields:
        assert fields[key]["name"]
        assert fields[key]["description"]

    for language in ("da", "en"):
        translations = json.loads(
            (component / "translations" / f"{language}.json").read_text(encoding="utf-8")
        )
        service_fields = translations["services"]["what_if"]["fields"]
        for key in new_fields:
            assert service_fields[key]["name"]
            assert service_fields[key]["description"]
        rule_fields = new_fields - {"min_profit_per_kwh", "min_total_profit", "min_sell_kwh"}
        for key in rule_fields:
            assert (
                service_fields[key]["name"]
                == translations["entity"]["switch"][key]["name"]
            )


def test_optimize_solar_charging_has_a_label_in_every_language() -> None:
    texts = Path(__file__).parents[1] / "custom_components" / "battmind" / "texts"
    for path in texts.glob("*.json"):
        translations = json.loads(path.read_text(encoding="utf-8"))
        assert translations["charging_type"]["optimize_solar_charging"]["description"]


async def test_post_setup_work_waits_until_home_assistant_started(hass: HomeAssistant, mock_entry) -> None:
    """Dashboard generation must not inspect other integrations too early."""
    action = AsyncMock()
    hass.set_state(CoreState.starting)

    await _async_run_after_home_assistant_started(hass, mock_entry, action, "battmind_test_startup")
    action.assert_not_awaited()

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    action.assert_awaited_once_with()

    action.reset_mock()
    await _async_run_after_home_assistant_started(hass, mock_entry, action, "battmind_test_reload")
    action.assert_awaited_once_with()


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


async def test_what_if_reads_statistics_and_raw_recorder_history(
    hass: HomeAssistant, loaded_entry
) -> None:
    hass.config.components.add("recorder")
    coordinator = loaded_entry.runtime_data
    coordinator.config["prices"]["entity_ids"].update(
        {
            "stromligning_buy_current_price": "sensor.stromligning_buy",
            "stromligning_sell_current_price": "sensor.stromligning_sell",
            "stromligning_sell_electricity_tax": "sensor.sell_tax",
            "stromligning_sell_nettariff": "sensor.sell_net_tariff",
            "stromligning_sell_surcharge": "sensor.sell_surcharge",
            "energidataservice_sell_entity_id": "sensor.eds_sell",
        }
    )
    recorder = AsyncMock()
    recorder.async_add_executor_job.side_effect = [{}, {}, {}]
    coordinator.async_run_job = AsyncMock(return_value=JobResult(value={}))

    with patch("homeassistant.components.recorder.get_instance", return_value=recorder):
        await coordinator.async_what_if({}, 1)

    assert recorder.async_add_executor_job.await_count == 3
    job_name, data, overrides = coordinator.async_run_job.await_args.args
    assert job_name == "what_if"
    assert "stats" in data
    assert "long_term_statistics_timestamps" in data
    assert data["price_providers"] == [
        {
            "name": "Strømligning",
            "buy_entity_id": "sensor.stromligning_buy",
            "sell_entity_id": "sensor.stromligning_sell",
            "sell_fee": 0.03,
            "sell_tariff_entity_ids": {
                "elafgift": "sensor.sell_tax",
                "tariffs": "sensor.sell_net_tariff",
                "surcharge": "sensor.sell_surcharge",
            },
        },
        {
            "name": "Energi Data Service",
            "buy_entity_id": "sensor.energi_data_service",
            "sell_entity_id": "sensor.eds_sell",
            "sell_fee": 0.03,
            "sell_includes_tariffs": True,
            "sell_tariff_history": [],
        },
    ]
    assert {
        "sensor.stromligning_buy",
        "sensor.stromligning_sell",
        "sensor.sell_tax",
        "sensor.sell_net_tariff",
        "sensor.sell_surcharge",
        "sensor.energi_data_service",
        "sensor.eds_sell",
    }.issubset(recorder.async_add_executor_job.call_args_list[0].args[4])
    # Recorder history indexes entity_ids (entity_ids[0] for one entity), so it must get a list.
    states_call, tariff_call = (call.args[0] for call in recorder.async_add_executor_job.call_args_list[1:])
    assert isinstance(states_call.args[3], list)
    assert states_call.args[3] == sorted(states_call.args[3])
    assert tariff_call.args[3] == ["sensor.eds_sell"]
    assert overrides == {}

    # A raw Energi Data Service price (tariffs disabled) has no tariffs attribute to read.
    hass.states.async_set("sensor.eds_sell", "0.5", {"unit_of_measurement": "kr/kWh"})
    recorder.async_add_executor_job.reset_mock()
    recorder.async_add_executor_job.side_effect = [{}, {}]
    with patch("homeassistant.components.recorder.get_instance", return_value=recorder):
        await coordinator.async_what_if({}, 1)
    assert recorder.async_add_executor_job.await_count == 2
    _, data, _ = coordinator.async_run_job.await_args.args
    assert data["price_providers"][1]["sell_includes_tariffs"] is False


async def test_what_if_service_returns_simulation_without_changing_options(
    hass: HomeAssistant, loaded_entry
) -> None:
    options = dict(loaded_entry.options)
    rules = {
        key: True
        for key in (
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
    }
    scenario_rules = {**rules, "most_profit": False}
    with patch.object(persistent_notification, "async_create", wraps=persistent_notification.async_create) as create:
        loaded_entry.runtime_data.async_what_if = AsyncMock(
            return_value=JobResult(
                value={
                    "period": {"days": 365, "hours": 8760},
                    "price_provider": "Energi Data Service",
                    "tariff_estimated_hours": 8760,
                    "tariff_unavailable_hours": 4,
                    "data_sources": {
                        "long_term_statistics_samples": 100,
                        "recorder_history_fallback_samples": 5,
                    },
                    "current_assumptions": {
                        "battery_size": 13.5,
                        "minimum_level": 10,
                        "maximum_level": 90,
                        "starting_level": 10,
                        "max_charging_power": 5000,
                        "max_discharging_power": 5000,
                        "round_trip_loss": 10,
                        "wear_cost_per_kwh": 0.1,
                        "usage_reserve": 20,
                        "min_profit_per_kwh": 0.0,
                        "min_total_profit": 0.0,
                        "min_sell_kwh": 0.2,
                        "rules": rules,
                        "control_mode": "optimizer",
                    },
                    "assumptions": {
                        "battery_size": 20,
                        "minimum_level": 10,
                        "maximum_level": 90,
                        "starting_level": 10,
                        "max_charging_power": 8000,
                        "max_discharging_power": 8000,
                        "round_trip_loss": 5,
                        "wear_cost_per_kwh": 0.1,
                        "usage_reserve": 20,
                        "min_profit_per_kwh": 0.0,
                        "min_total_profit": 0.0,
                        "min_sell_kwh": 0.2,
                        "rules": scenario_rules,
                    },
                    "current": {
                        "profit": 100,
                        "charged_solar_kwh": 12,
                        "with": {
                            "house_kwh": 800,
                            "house_cost": 400,
                            "battery_kwh": 50,
                            "battery_cost": 25,
                            "solar_kwh": 100,
                            "solar_income": -20,
                            "sold_kwh": 10,
                            "sold_income": -5,
                            "wear": 2,
                            "paid": 425,
                            "earned": -25,
                            "bill": 500,
                        },
                    },
                    "scenario": {
                        "profit": 125,
                        "charged_solar_kwh": 15,
                        "with": {
                            "house_kwh": 750,
                            "house_cost": 375,
                            "battery_kwh": 60,
                            "battery_cost": 30,
                            "solar_kwh": 90,
                            "solar_income": -18,
                            "sold_kwh": 15,
                            "sold_income": -8,
                            "wear": 3,
                            "paid": 405,
                            "earned": -26,
                            "bill": -633.03,
                        },
                    },
                    "difference": {"profit": 25, "bill": -1133.03},
                }
            )
        )
        response = await hass.services.async_call(
            DOMAIN,
            "what_if",
            {
                "config_entry_id": loaded_entry.entry_id,
                "battery_size": 20,
                "round_trip_loss": 5,
                "most_profit": False,
            },
            blocking=True,
            return_response=True,
        )

    assert create.call_count == 2
    progress_call, result_call = create.call_args_list
    notification_id = f"{PREFIX}_what_if"
    assert progress_call.kwargs["notification_id"] == notification_id
    assert progress_call.args[1] == loaded_entry.runtime_data.t(
        "ui.what_if.started", days=365
    )
    assert result_call.kwargs["notification_id"] == notification_id
    notification = result_call.args[1]
    coordinator = loaded_entry.runtime_data
    assert (
        f"| | **{coordinator.t('ui.what_if.current')}** "
        f"| **{coordinator.t('ui.what_if.scenario')}** |"
        in notification
    )
    assert "|---|---|---|" in notification
    assert coordinator.t("ui.what_if.data_sources", statistics=100, recorder=5) in notification
    assert coordinator.t(
        "ui.what_if.price_provider", provider="Energi Data Service"
    ) in notification
    assert coordinator.t("ui.what_if.tariff_estimated", hours=8760) in notification
    assert coordinator.t("ui.what_if.tariff_unavailable", hours=4) in notification
    assert coordinator.t(
        "ui.what_if.control_mode", mode=coordinator.t("ui.what_if.optimizer_mode")
    ) in notification
    assert f"| **{coordinator.t('ui.what_if.battery_size')}** | 13,5 kWh | 20,0 kWh |" in notification
    assert (
        f"| **{coordinator.t('charging_type.most_profit.description')}** | "
        f"{coordinator.t('ui.what_if.enabled')} | {coordinator.t('ui.what_if.disabled')} |"
        in notification
    )
    # Only rules that differ between current and scenario are listed.
    assert coordinator.t("charging_type.optimize_solar_charging.description") not in notification
    assert "charging_type.most_profit.description" not in notification
    assert (
        f"| | **{coordinator.t('ui.what_if.current')}** "
        f"| **{coordinator.t('ui.what_if.scenario')}** | |"
        in notification
    )
    assert f"**{coordinator.t('ui.what_if.round_trip_loss')}**" in notification
    assert f"**{coordinator.t('ui.what_if.minimum_level')}**" not in notification
    assert f"| **{coordinator.t('ui.what_if.house_kwh')}** | 800,0 | 750,0 | kWh |" in notification
    valuta = coordinator.t("ui.common.valuta")
    assert f"| **{coordinator.t('ui.what_if.paid')}** | 425,00 | 405,00 | {valuta} |" in notification
    assert f"| **{coordinator.t('ui.what_if.earned')}** | 25,00 | 26,00 | {valuta} |" in notification
    assert f"| **{coordinator.t('ui.what_if.bill')}** | 500,00 | -633,03 | {valuta} |" in notification
    result_table_end = notification.index(
        f"| **{coordinator.t('ui.what_if.bill')}** | 500,00 | -633,03 | {valuta} |"
    )
    profit_difference = f"**{coordinator.t('ui.what_if.profit_difference')}:** 25,00 {valuta}"
    bill_difference = f"**{coordinator.t('ui.what_if.bill_difference')}:** 1133,03 {valuta}"
    assert notification.index(profit_difference) > result_table_end
    assert notification.index(bill_difference) > result_table_end
    assert coordinator.t("ui.what_if.note") in notification

    simulation = response["simulations"][PREFIX]
    same_improvement = _what_if_notification(
        coordinator,
        {**simulation, "difference": {"profit": 25, "bill": -25}},
        {"battery_size": 20, "round_trip_loss": 5, "most_profit": False, "minimum_level": 10},
    )
    assert profit_difference in same_improvement
    # An override equal to the configured value is not a change.
    assert f"**{coordinator.t('ui.what_if.minimum_level')}**" not in same_improvement
    assert coordinator.t("ui.what_if.bill_difference") not in same_improvement
    assert simulation["assumptions"]["battery_size"] == 20
    assert simulation["assumptions"]["round_trip_loss"] == 5
    assert simulation["period"]["days"] == 365
    assert "plan" not in simulation
    loaded_entry.runtime_data.async_what_if.assert_awaited_once()
    assert loaded_entry.options == options


async def test_what_if_replaces_progress_notification_with_error(
    hass: HomeAssistant, loaded_entry
) -> None:
    coordinator = loaded_entry.runtime_data
    coordinator.async_what_if = AsyncMock(side_effect=HomeAssistantError("Recorder unavailable"))
    with (
        patch.object(persistent_notification, "async_create") as create,
        pytest.raises(ServiceValidationError, match="Recorder unavailable"),
    ):
        await hass.services.async_call(
            DOMAIN,
            "what_if",
            {"config_entry_id": loaded_entry.entry_id},
            blocking=True,
            return_response=True,
        )

    assert create.call_count == 2
    assert create.call_args_list[0].kwargs["notification_id"] == f"{PREFIX}_what_if"
    assert create.call_args_list[1].kwargs["notification_id"] == f"{PREFIX}_what_if"
    assert "Recorder unavailable" in create.call_args_list[1].args[1]


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
