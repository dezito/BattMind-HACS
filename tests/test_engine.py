"""BattMind's algorithm without Home Assistant: the worker jobs on a hand made Snapshot.

These tests run the same code as the worker thread, so a scenario can be set up at any
time of day and battery level without waiting for the clock.
"""
from __future__ import annotations

import datetime
import json
import zoneinfo
from pathlib import Path

import pytest

from custom_components.battmind.codec import decode, encode
from custom_components.battmind.const import POWERWALL_ACTION_STATES, build_config, default_options
from custom_components.battmind.engine import BattMindEngine, Snapshot

TZ = zoneinfo.ZoneInfo("Europe/Copenhagen")
DAY = datetime.date(2026, 10, 22)
CHEAP_HOURS = set(range(7)) | {21, 22, 23}
EXPENSIVE_HOURS = set(range(17, 21))

OPTIONS = {
    **default_options(),
    "powerwall_battery_level_entity_id": "sensor.battery_level",
    "powerwall_watt_flow_entity_id": "sensor.battery_power",
    "power_consumption_entity_id": "sensor.consumption",
    "energidataservice_buy_entity_id": "sensor.price",
}


def _texts() -> dict[str, str]:
    """The Danish runtime texts, flattened like the coordinator does."""
    path = Path(__file__).parents[1] / "custom_components" / "battmind" / "translations" / "da.json"
    texts: dict[str, str] = {}

    def flatten(data: dict, prefix: str = "") -> None:
        for key, value in data.items():
            if isinstance(value, dict):
                flatten(value, f"{prefix}{key}.")
            else:
                texts[f"{prefix}{key}"] = value

    flatten(json.loads(path.read_text(encoding="utf-8"))["runtime"])
    return texts


TEXTS = _texts()


def _price_rows(day: datetime.date, base: float) -> list[dict]:
    rows = []
    for hour in range(24):
        price = base + (1.5 if hour in EXPENSIVE_HOURS else 0.6 if 7 <= hour <= 16 else 0.0) + hour / 100
        rows.append({"hour": datetime.datetime.combine(day, datetime.time(hour), tzinfo=TZ).isoformat(), "price": round(price, 3)})
    return rows


def _power_values_db() -> dict:
    """10 days of history: 600 W, 1500 W in the evening."""
    database = {}
    for hour in range(24):
        watt = 1500.0 if 17 <= hour <= 21 else 600.0
        stamps = [datetime.datetime.combine(DAY - datetime.timedelta(days=day + 1), datetime.time(hour, 59)) for day in range(10)]
        values = {
            "power_consumption": watt,
            "ignored_consumption": 0.0,
            "powerwall_charging_consumption": 0.0,
            "powerwall_discharging_consumption": 0.0,
            "solar_production": 0.0,
            "power_consumption_without_ignored": watt,
            "power_consumption_without_all_exclusion": watt,
        }
        database[hour] = {key: [[stamp, value] for stamp in stamps] for key, value in values.items()}
    return database


def snapshot(now: datetime.datetime, battery_level: float, settings: dict | None = None) -> Snapshot:
    tomorrow = now.date() + datetime.timedelta(days=1)
    price_attributes = {
        "raw_today": _price_rows(now.date(), 1.0),
        "raw_tomorrow": _price_rows(tomorrow, 0.8),
        "tomorrow_valid": True,
        "forecast": _price_rows(tomorrow + datetime.timedelta(days=1), 0.9) + _price_rows(tomorrow + datetime.timedelta(days=2), 1.1),
    }
    return Snapshot(
        now=now,
        tz=TZ,
        settings=settings or {},
        entity_states={"sensor.battery_level": str(battery_level), "sensor.battery_power": "0", "sensor.consumption": "650", "sensor.price": "1.2"},
        units={"sensor.battery_power": "W", "sensor.consumption": "W"},
        price_data={"attributes": {"sensor.price": price_attributes}, "values": {}},
    )


def started_engine(now: datetime.datetime, battery_level: float, with_history: bool = True) -> BattMindEngine:
    engine = BattMindEngine(build_config(OPTIONS), "test", TEXTS)
    databases = {"power_values": _power_values_db()} if with_history else {}
    result = engine.run_job(snapshot(now, battery_level), engine.startup, databases)
    assert result.error is None, result.error
    return engine


def plan(engine: BattMindEngine, now: datetime.datetime, battery_level: float, settings: dict | None = None):
    result = engine.run_job(snapshot(now, battery_level, settings), engine.charge_if_needed, True)
    assert result.error is None, result.error
    return result


def charge_hours(engine: BattMindEngine) -> list[datetime.datetime]:
    return sorted(ts for ts in engine.charge_hours if isinstance(ts, datetime.datetime))


@pytest.mark.parametrize("hour", [1, 5, 10, 18, 23])
@pytest.mark.parametrize("battery_level", [5, 50, 100])
def test_action_is_always_valid(hour: int, battery_level: float) -> None:
    now = datetime.datetime.combine(DAY, datetime.time(hour, 30))
    engine = started_engine(now, battery_level)
    result = plan(engine, now, battery_level)
    assert result.states["powerwall_action"] in POWERWALL_ACTION_STATES
    assert result.states["current_charging_rule"]
    assert 0 <= result.states["battery_level_needed"] <= 100


@pytest.mark.parametrize("battery_level", [5, 50])
def test_grid_charging_only_in_cheap_hours(battery_level: float) -> None:
    now = datetime.datetime.combine(DAY, datetime.time(3, 30))
    engine = started_engine(now, battery_level)
    plan(engine, now, battery_level)
    hours = charge_hours(engine)
    assert hours, "an empty battery and 1.5 kr/kWh price spread should give grid charging"
    assert not [ts for ts in hours if ts.hour not in CHEAP_HOURS]
    assert not [ts for ts in hours if ts.hour in EXPENSIVE_HOURS]


def test_low_battery_charges_before_the_morning_price_rise() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(5, 30))
    engine = started_engine(now, 5)
    result = plan(engine, now, 5)
    assert result.states["powerwall_action"] == "grid_charging"
    assert datetime.datetime.combine(DAY, datetime.time(5)) in charge_hours(engine)


def test_full_battery_does_not_charge_today() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(5, 30))
    engine = started_engine(now, 100)
    result = plan(engine, now, 100)
    assert result.states["powerwall_action"] != "grid_charging"
    assert not [ts for ts in charge_hours(engine) if ts.date() == DAY]


def test_no_history_means_no_grid_charging() -> None:
    """Without consumption history there is nothing to cover with cheap grid power."""
    now = datetime.datetime.combine(DAY, datetime.time(3, 30))
    engine = started_engine(now, 5, with_history=False)
    plan(engine, now, 5)
    assert charge_hours(engine) == []


def test_deactivated_script_keeps_last_action() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(5, 30))
    engine = started_engine(now, 5)
    plan(engine, now, 5)
    result = plan(engine, now, 5, {"deactivate_script": True})
    assert "powerwall_action" not in result.states
    assert result.states["current_charging_rule"].startswith("⛔")


def test_hour_end_stores_power_values() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12, 59))
    engine = started_engine(now, 50)
    result = engine.run_job(snapshot(now, 50), engine.hour_end, now.replace(minute=0))
    assert result.error is None, result.error
    assert "power_values" in result.db_saves
    assert decode(result.db_saves["power_values"])[12]["power_consumption"][0][0] == now
    assert result.value["prices"]["buy_price"] == pytest.approx(1.0 + 0.6 + 0.12)


def test_startup_saves_every_database_losslessly() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    engine = BattMindEngine(build_config(OPTIONS), "test", TEXTS)
    history = _power_values_db()
    result = engine.run_job(snapshot(now, 50), engine.startup, {"power_values": history})
    assert set(result.db_saves) == {"charging_history", "power_values", "kwh_avg_prices", "solar_available"}
    assert decode(json.loads(json.dumps(result.db_saves["power_values"]))) == history


def test_missing_prices_fall_back_to_offline_prices() -> None:
    """Price integration not loaded: plan with prices estimated from the price history."""
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    engine = BattMindEngine(build_config(OPTIONS), "test", TEXTS)
    empty = snapshot(now, 50)
    empty.price_data = {"attributes": {}, "values": {}}
    assert engine.run_job(empty, engine.startup, {"power_values": _power_values_db()}).error is None
    result = engine.run_job(empty, engine.charge_if_needed, True)
    assert result.error is None
    assert engine.grid_price_cache["using_offline_prices"] is True
    assert result.states["powerwall_action"] in POWERWALL_ACTION_STATES


def test_codec_roundtrip() -> None:
    data = {
        0: {"a": [[datetime.datetime(2026, 1, 1, 12), 1.5]]},
        (1, 2): "tuple key",
        "__dt__": "reserved key",
        "nested": {"when": datetime.datetime(2026, 5, 5)},
    }
    assert decode(json.loads(json.dumps(encode(data)))) == data
