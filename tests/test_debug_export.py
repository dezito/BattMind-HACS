"""Debug export for GitHub issues (management/debug_export.py) and its replay (tests/debug_replay.py)."""
from __future__ import annotations

import datetime
import gzip
import json
import logging
from pathlib import Path

import pytest

from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.battmind.const import DOMAIN
from custom_components.battmind.management.debug_export import DEBUG_DIR, KEEP_FILES, Redactor

from . import debug_replay
from .conftest import CONSUMPTION, PREFIX, PRICE

BUTTON = f"button.{PREFIX}_export_debug"


@pytest.fixture
def config_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    hass.config.config_dir = str(tmp_path)
    return tmp_path / DEBUG_DIR


def _read(path: Path) -> tuple[dict, str]:
    with gzip.open(path, "rt", encoding="utf-8") as file:
        text = file.read()
    return json.loads(text), text


async def test_export_is_anonymised(hass: HomeAssistant, config_dir: Path, loaded_entry) -> None:
    hass.config.location_name = "Sommerhuset Bakkebo"
    hass.states.async_set("person.thomas", "home", {"friendly_name": "Thomas Sloth", "latitude": 55.6761, "longitude": 12.5683})
    logging.getLogger("custom_components.battmind.engine").warning(
        "Thomas Sloth at 192.168.1.20 (thomas@example.com, https://example.com/x) has %s", CONSUMPTION
    )

    response = await hass.services.async_call(DOMAIN, "export_debug", {}, blocking=True, return_response=True)
    file = response["files"][PREFIX]
    data, text = _read(config_dir.parent / file)

    assert data["integration"] == DOMAIN and data["prefix"] == PREFIX
    assert data["config"]["solar"]["battery_temperature_min"] == 0.0
    assert data["config"]["solar"]["battery_temperature_max"] == 50.0
    assert data["jobs"] and data["snapshots"] and data["engine"]["plan"] is not None
    assert set(data["databases"]) >= {"charging_history", "power_values"}
    # other integrations' entities are named after their config key, own entities are kept
    for secret in (CONSUMPTION, PRICE, "Thomas", "Sloth", "Bakkebo", "192.168.1.20", "example.com"):
        assert secret not in text, secret
    assert "sensor.power_consumption" in text
    assert data["config"]["home"]["entity_ids"]["power_consumption_entity_id"] == "sensor.power_consumption"
    assert f"sensor.{PREFIX}_" in text
    # the replay falls back to these prices when the snapshots have none
    assert data["price_data"]["attributes"]
    message = next(record["message"] for record in data["log"] if "192.168" not in record["message"] and "<ip>" in record["message"])
    assert message == "<redacted> at <ip> (<email>, <url>) has sensor.power_consumption"

    item = hass.data["persistent_notification"][f"{PREFIX}_debug_export"]
    assert "issues/new?template=bug_report.yml" in item["message"]
    assert file.split("/")[-1] in item["message"]


async def test_button_keeps_the_newest_files(hass: HomeAssistant, config_dir: Path, loaded_entry) -> None:
    config_dir.mkdir(parents=True)
    for index in range(KEEP_FILES + 2):
        (config_dir / f"{PREFIX}_debug_20200101_00000{index}.json.gz").write_bytes(b"")
    (config_dir / "other_debug_20200101_000000.json.gz").write_bytes(b"")
    await hass.services.async_call("button", "press", {"entity_id": BUTTON}, blocking=True)
    own = sorted(p.name for p in config_dir.iterdir() if p.name.startswith(f"{PREFIX}_debug_"))
    assert len(own) == KEEP_FILES and not own[-1].startswith(f"{PREFIX}_debug_2020")
    assert (config_dir / "other_debug_20200101_000000.json.gz").exists()


async def test_download_view(hass: HomeAssistant, config_dir: Path, loaded_entry, hass_client_no_auth) -> None:
    await async_setup_component(hass, "http", {})
    response = await hass.services.async_call(DOMAIN, "export_debug", {}, blocking=True, return_response=True)
    name = response["files"][PREFIX].split("/")[-1]
    client = await hass_client_no_auth()
    assert (await client.get(f"/api/{DOMAIN}/debug/{name}")).status == 401
    signed = async_sign_path(hass, f"/api/{DOMAIN}/debug/{name}", datetime.timedelta(minutes=5), use_content_user=True)
    download = await client.get(signed)
    assert download.status == 200
    assert gzip.decompress(await download.read()).startswith(b"{")
    assert (await client.get(async_sign_path(hass, f"/api/{DOMAIN}/debug/..%2Fsecrets.yaml", datetime.timedelta(minutes=5), use_content_user=True))).status == 404


async def test_replay_reproduces_the_plan(hass: HomeAssistant, config_dir: Path, loaded_entry) -> None:
    coordinator = loaded_entry.runtime_data
    await coordinator.async_run_job("charge_if_needed", True, include_forecast=True)
    recorded = coordinator.data["current_charging_rule"]
    response = await hass.services.async_call(DOMAIN, "export_debug", {}, blocking=True, return_response=True)
    export = debug_replay.load_export(str(config_dir.parent / response["files"][PREFIX]))
    assert export["snapshots"][-1]["job"] == "charge_if_needed"
    assert export["planning_job"]["job"] == "charge_if_needed"

    result = await hass.async_add_executor_job(debug_replay.replay, export)
    assert result.error is None
    assert result.value == coordinator.engine.last_powerwall_action
    assert result.states["current_charging_rule"] == recorded


async def test_replay_adds_temperature_defaults_to_old_export(hass: HomeAssistant, config_dir: Path, loaded_entry) -> None:
    response = await hass.services.async_call(DOMAIN, "export_debug", {}, blocking=True, return_response=True)
    export = debug_replay.load_export(str(config_dir.parent / response["files"][PREFIX]))
    export["config"]["solar"].pop("battery_temperature_min")
    export["config"]["solar"].pop("battery_temperature_max")

    engine = debug_replay.build_engine(export)

    assert engine.config["solar"]["battery_temperature_min"] == 0.0
    assert engine.config["solar"]["battery_temperature_max"] == 50.0


def test_redactor_aliases() -> None:
    config = {
        "solar": {"entity_ids": {"forecast_entity_id": "sensor.solcast_home_forecast_today"}},
        "home": {"entity_ids": {"ignore_consumption_from_entity_ids": ["sensor.thomas_car", "switch.pool"]}},
    }
    redactor = Redactor("bm", config, ["Thomas"])
    assert redactor.text("sensor.solcast_home_forecast_day_3") == "sensor.forecast_day_3"
    assert redactor.text("sensor.thomas_car, switch.pool") == "sensor.ignore_consumption_from_1, switch.ignore_consumption_from_2"
    assert redactor.text("sensor.bm_overview custom_components/battmind/sensor.py") == "sensor.bm_overview custom_components/battmind/sensor.py"
    assert redactor.text("sensor.unknown_thing") == "sensor.entity_4"
    assert redactor.value({"latitude": 55.6761, "elevation": 37, "api_key": "abc", "friendly_name": "Thomas"}) == {
        "latitude": 56, "elevation": 0, "api_key": "<redacted>", "friendly_name": "<redacted>"
    }
