"""Config flow and options flow of BattMind."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.battmind.const import CONF_ENTITY_PREFIX, DOMAIN

from .conftest import BATTERY_FLOW, BATTERY_LEVEL, CONSUMPTION, PRICE, PRODUCTION, form_defaults

FIXTURES = Path(__file__).parent / "fixtures"

BATTERY_SPECS = {
    "powerwall_battery_size": 13.5,
    "powerwall_battery_level_min": 10,
    "powerwall_battery_level_max": 95,
    "powerwall_charging_power_limit": 5000,
    "powerwall_discharging_power": 5000,
    "powerwall_charge_discharge_loss": 0.1,
    "use_charge_discharge_loss_from_history": True,
    "powerwall_wear_cost_per_kwh": 0.1,
    "battery_temperature_min": 0,
    "battery_temperature_max": 50,
}


async def _start(hass: HomeAssistant, prefix: str = "bmflow"):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ENTITY_PREFIX: prefix, "language": "da"})


async def _configure(hass: HomeAssistant, result, step_id: str, user_input: dict):
    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == step_id, result
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def _to_price_provider(hass: HomeAssistant):
    result = await _start(hass)
    result = await _configure(
        hass, result, "battery_entities",
        {"powerwall_battery_level_entity_id": BATTERY_LEVEL, "powerwall_watt_flow_entity_id": BATTERY_FLOW, "invert_powerwall_watt_flow_entity_id": True},
    )
    result = await _configure(hass, result, "battery_specs", BATTERY_SPECS)
    return await _configure(hass, result, "consumption", {"power_consumption_entity_id": CONSUMPTION})


async def test_full_flow_creates_entry(hass: HomeAssistant) -> None:
    result = await _to_price_provider(hass)
    result = await _configure(hass, result, "price_provider", {"price_providers": ["energidataservice"]})
    result = await _configure(hass, result, "prices_energidataservice", {"energidataservice_buy_entity_id": PRICE})
    result = await _configure(hass, result, "price_fees", {"refund": 0.0, "sell_fee": 0.03})
    result = await _configure(
        hass, result, "solar",
        {"production_entity_id": PRODUCTION, "inverter_discharging_power_limit": 6000, "production_price": -1},
    )
    result = await _configure(hass, result, "weather", {})
    result = await _configure(hass, result, "advanced", {"days_to_predict": 3, "cron_interval": "5", "dashboard": False})
    result = await _configure(
        hass, result, "database",
        {
            "power_values_db_data_to_save": 15,
            "solar_available_db_data_to_save": 10,
            "kwh_avg_prices_db_data_to_save": 15,
            "charging_history_db_data_to_save": 12,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "BattMind (bmflow)"
    assert result["data"][CONF_ENTITY_PREFIX] == "bmflow"
    options = result["options"]
    assert options["powerwall_battery_level_entity_id"] == BATTERY_LEVEL
    assert options["invert_powerwall_watt_flow_entity_id"] is True
    assert options["powerwall_battery_size"] == 13.5
    assert options["cron_interval"] == 5  # the dropdown value is stored as int
    assert options["stromligning_buy_current_price"] == ""  # provider not chosen
    assert options["hourly_service_entity_id"] == ""


async def test_stromligning_pages(hass: HomeAssistant) -> None:
    result = await _to_price_provider(hass)
    result = await _configure(hass, result, "price_provider", {"price_providers": ["stromligning"]})
    result = await _configure(
        hass, result, "prices_stromligning",
        {"stromligning_buy_current_price": "sensor.stromligning_current_price_vat", "stromligning_sell_current_price": "sensor.stromligning_sell"},
    )
    # the tariff page is only shown with a sell price
    result = await _configure(hass, result, "prices_stromligning_tariffs", {})
    assert result["step_id"] == "price_fees"


@pytest.mark.parametrize(
    ("provider", "step", "sell"),
    [
        ("energidataservice", "prices_energidataservice", {"energidataservice_sell_entity_id": "sensor.eds_sell"}),
        ("stromligning", "prices_stromligning", {"stromligning_sell_current_price": "sensor.stromligning_sell"}),
    ],
)
async def test_a_sell_entity_alone_is_rejected(hass: HomeAssistant, provider: str, step: str, sell: dict) -> None:
    """The planner, what-if and dashboard need the buy entity of every chosen provider."""
    result = await _to_price_provider(hass)
    result = await _configure(hass, result, "price_provider", {"price_providers": [provider]})
    with pytest.raises(InvalidData):
        await _configure(hass, result, step, sell)


async def test_weather_page_skipped_without_solar(hass: HomeAssistant) -> None:
    result = await _to_price_provider(hass)
    result = await _configure(hass, result, "price_provider", {"price_providers": ["energidataservice"]})
    result = await _configure(hass, result, "prices_energidataservice", {"energidataservice_buy_entity_id": PRICE})
    result = await _configure(hass, result, "price_fees", {"refund": 0.0, "sell_fee": 0.03})
    result = await _configure(hass, result, "solar", {"inverter_discharging_power_limit": 6000, "production_price": -1})
    assert result["step_id"] == "advanced"


async def test_solcast_needs_production(hass: HomeAssistant) -> None:
    result = await _to_price_provider(hass)
    result = await _configure(hass, result, "price_provider", {"price_providers": ["energidataservice"]})
    result = await _configure(hass, result, "prices_energidataservice", {"energidataservice_buy_entity_id": PRICE})
    result = await _configure(hass, result, "price_fees", {"refund": 0.0, "sell_fee": 0.03})
    result = await _configure(
        hass, result, "solar",
        {"forecast_entity_id": "sensor.solcast_pv_forecast_forecast_today", "inverter_discharging_power_limit": 6000, "production_price": -1},
    )
    assert result["errors"] == {"production_entity_id": "production_required"}


async def test_invalid_prefix(hass: HomeAssistant) -> None:
    result = await _start(hass, prefix="1-bad prefix")
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ENTITY_PREFIX: "invalid_prefix"}


async def test_prefix_in_use(hass: HomeAssistant, mock_entry) -> None:
    mock_entry.add_to_hass(hass)
    result = await _start(hass, prefix=mock_entry.data[CONF_ENTITY_PREFIX])
    assert result["errors"] == {CONF_ENTITY_PREFIX: "prefix_in_use"}


async def test_prefix_taken_by_pyscript(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.bmflow_overview", "PyScript")
    result = await _start(hass)
    assert result["errors"] == {CONF_ENTITY_PREFIX: "prefix_entities_exist"}


async def test_battery_min_above_max(hass: HomeAssistant) -> None:
    result = await _start(hass)
    result = await _configure(hass, result, "battery_entities", {"powerwall_battery_level_entity_id": BATTERY_LEVEL, "powerwall_watt_flow_entity_id": BATTERY_FLOW})
    result = await _configure(hass, result, "battery_specs", {**BATTERY_SPECS, "powerwall_battery_level_min": 90, "powerwall_battery_level_max": 80})
    assert result["step_id"] == "battery_specs"
    assert result["errors"] == {"powerwall_battery_level_min": "min_above_max"}

    result = await _configure(
        hass,
        result,
        "battery_specs",
        {**BATTERY_SPECS, "battery_temperature_min": 50, "battery_temperature_max": 50},
    )
    assert result["step_id"] == "battery_specs"
    assert result["errors"] == {"battery_temperature_min": "min_above_max"}

    result = await _configure(
        hass,
        result,
        "battery_specs",
        {**BATTERY_SPECS, "battery_charging_efficiency_curve": {"1000": 0}},
    )
    assert result["step_id"] == "battery_specs"
    assert result["errors"] == {"battery_charging_efficiency_curve": "invalid_efficiency_curve"}


async def test_no_price_provider(hass: HomeAssistant) -> None:
    result = await _to_price_provider(hass)
    result = await _configure(hass, result, "price_provider", {"price_providers": []})
    assert result["errors"] == {"base": "no_price_provider"}


async def test_import_legacy_pyscript_config(hass: HomeAssistant, tmp_path: Path) -> None:
    """A PyScript battmind_config.yaml in the config dir is offered and its values prefill the pages."""
    shutil.copy(FIXTURES / "battmind_config.yaml", tmp_path / "battmind_config.yaml")
    hass.config.config_dir = str(tmp_path)
    result = await _start(hass)
    assert result["step_id"] == "import_legacy"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"import_source": "battmind"})
    assert result["step_id"] == "battery_entities"
    assert form_defaults(result)["powerwall_battery_level_entity_id"] == "sensor.batteries_state_of_capacity"

    # accept every page as prefilled
    while result["type"] is FlowResultType.FORM:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], form_defaults(result))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["legacy_prefix"] == "battmind"
    assert result["data"]["initial_settings"]["cheap_price_periods"] == 2
    options = result["options"]
    assert options["powerwall_battery_size"] == 10.0
    assert options["powerwall_charge_discharge_loss"] == 0.25
    assert options["ignore_consumption_from_entity_ids"] == ["sensor.bil_lader_power"]
    assert options["energidataservice_sell_entity_id"] == "sensor.energi_data_service_salg"
    assert options["language"] == "da"


async def test_options_flow_changes_battery_size(hass: HomeAssistant, loaded_entry) -> None:
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert "menu_battery" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "menu_battery"})
    assert result["step_id"] == "battery_entities"
    result = await hass.config_entries.options.async_configure(result["flow_id"], form_defaults(result))
    assert result["step_id"] == "battery_specs"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**form_defaults(result), "powerwall_battery_size": 20.0})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    assert loaded_entry.options["powerwall_battery_size"] == 20.0
    # the update listener reloads the entry with the new configuration
    assert loaded_entry.runtime_data.config["solar"]["powerwall_battery_size"] == 20.0
