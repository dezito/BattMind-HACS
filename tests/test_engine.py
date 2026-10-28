"""BattMind's algorithm without Home Assistant: the worker jobs on a hand made Snapshot.

These tests run the same code as the worker thread, so a scenario can be set up at any
time of day and battery level without waiting for the clock.
"""
from __future__ import annotations

import datetime
import json
import zoneinfo
from copy import deepcopy
from pathlib import Path

import pytest

from custom_components.battmind.utils.codec import decode, encode
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
    path = Path(__file__).parents[1] / "custom_components" / "battmind" / "texts" / "da.json"
    texts: dict[str, str] = {}

    def flatten(data: dict, prefix: str = "") -> None:
        for key, value in data.items():
            if isinstance(value, dict):
                flatten(value, f"{prefix}{key}.")
            else:
                texts[f"{prefix}{key}"] = value

    flatten(json.loads(path.read_text(encoding="utf-8")))
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


@pytest.mark.parametrize(
    ("temperature", "unit", "expected_action"),
    [(20, "°C", None), (-1, "°C", "battery_stopped"), (51, "°C", "battery_stopped"),
     (104, "°F", None), (122, "°F", None), (123, "°F", "battery_stopped"), (273.15, "K", None)],
)
def test_battery_temperature_safety(temperature: float, unit: str, expected_action: str | None) -> None:
    now = datetime.datetime.combine(DAY, datetime.time(10, 30))
    options = {**OPTIONS, "battery_temperature_entity_id": "sensor.battery_temperature",
               "battery_temperature_min": 0, "battery_temperature_max": 50}
    engine = BattMindEngine(build_config(options), "test", TEXTS)
    snap = snapshot(now, 50)
    snap.entity_states["sensor.battery_temperature"] = str(temperature)
    snap.units["sensor.battery_temperature"] = unit
    assert engine.run_job(snap, engine.startup, {"power_values": _power_values_db()}).error is None
    result = engine.run_job(snap, engine.charge_if_needed, True)
    assert result.error is None, result.error
    if expected_action is None:
        assert result.states["powerwall_action"] != "battery_stopped"
    else:
        assert result.states["powerwall_action"] == expected_action


@pytest.mark.parametrize("state", [None, "unknown", "unavailable", "not-a-number"])
def test_unavailable_battery_temperature_stops_battery(state: str | None) -> None:
    now = datetime.datetime.combine(DAY, datetime.time(10, 30))
    options = {**OPTIONS, "battery_temperature_entity_id": "sensor.battery_temperature",
               "battery_temperature_min": 0, "battery_temperature_max": 50}
    engine = BattMindEngine(build_config(options), "test", TEXTS)
    snap = snapshot(now, 50)
    if state is not None:
        snap.entity_states["sensor.battery_temperature"] = state
    assert engine.run_job(snap, engine.startup, {"power_values": _power_values_db()}).error is None
    result = engine.run_job(snap, engine.charge_if_needed, True)
    assert result.error is None, result.error
    assert result.states["powerwall_action"] == "battery_stopped"


# only the rules that charge for the own consumption from the cheap hours
CHEAPEST_ONLY = {"optimizer_control": False, "most_expensive_planner": False, "grid_charge_to_sell": False, "most_profit": False}


@pytest.mark.parametrize("battery_level", [5, 50])
def test_grid_charging_only_in_cheap_hours(battery_level: float) -> None:
    now = datetime.datetime.combine(DAY, datetime.time(3, 30))
    engine = started_engine(now, battery_level)
    plan(engine, now, battery_level, CHEAPEST_ONLY)
    hours = charge_hours(engine)
    assert hours, "an empty battery and 1.5/kWh price spread should give grid charging"
    assert not [ts for ts in hours if ts.hour not in CHEAP_HOURS]
    assert not [ts for ts in hours if ts.hour in EXPENSIVE_HOURS]


def test_default_rules_never_charge_in_the_expensive_hours() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(3, 30))
    engine = started_engine(now, 5)
    plan(engine, now, 5)
    assert charge_hours(engine)
    assert not [ts for ts in charge_hours(engine) if ts.hour in EXPENSIVE_HOURS]


def test_low_battery_charges_before_the_morning_price_rise() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(5, 30))
    engine = started_engine(now, 5)
    result = plan(engine, now, 5)
    assert result.states["powerwall_action"] == "grid_charging"
    assert datetime.datetime.combine(DAY, datetime.time(5)) in charge_hours(engine)


def test_full_battery_does_not_charge_today() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(5, 30))
    engine = started_engine(now, 100)
    result = plan(engine, now, 100, CHEAPEST_ONLY)
    assert result.states["powerwall_action"] != "grid_charging"
    assert not [ts for ts in charge_hours(engine) if ts.date() == DAY]


def test_full_battery_is_not_charged_now_with_the_default_rules() -> None:
    """Later top ups for the expensive evening are fine, but there is no room right now."""
    now = datetime.datetime.combine(DAY, datetime.time(5, 30))
    engine = started_engine(now, 100)
    result = plan(engine, now, 100)
    assert result.states["powerwall_action"] != "grid_charging"


def test_no_history_means_no_grid_charging_for_the_own_consumption() -> None:
    """Without consumption history there is nothing to cover - only buy-to-sell charges, each for a sale."""
    now = datetime.datetime.combine(DAY, datetime.time(3, 30))
    engine = started_engine(now, 5, with_history=False)
    plan(engine, now, 5, {"optimizer_control": False})
    assert all(engine.charge_hours[ts].get("grid_charge_to_sell") for ts in charge_hours(engine))
    sales = [sale for day in engine.plan if isinstance(day, int) for sale in engine.plan[day]["force_discharge_timestamps"].values()]
    assert bool(charge_hours(engine)) == any(sale["title"] == "Sælger strøm købt billigt" for sale in sales)
    plan(engine, now, 5, CHEAPEST_ONLY)
    assert charge_hours(engine) == []


@pytest.mark.parametrize("settings", [{"min_profit_per_kwh": 5.0}, {"min_total_profit": 100.0}])
def test_min_profit_applies_to_grid_charging_without_the_profit_switch(settings: dict) -> None:
    """min_profit_per_kwh / min_total_profit apply to every rule, not only to only_discharge_on_profit."""
    now = datetime.datetime.combine(DAY, datetime.time(3, 30))
    engine = started_engine(now, 5)
    plan(engine, now, 5, {"only_discharge_on_profit": False, **settings})
    assert charge_hours(engine) == []


def test_round_trip_loss_in_price_and_battery_level() -> None:
    """1 kWh out of the battery costs price / (1 - loss); a grid kWh adds only sqrt(1 - loss) kWh."""
    now = datetime.datetime.combine(DAY, datetime.time(3, 30))
    engine = BattMindEngine(
        build_config({**OPTIONS, "powerwall_charge_discharge_loss": 0.2, "use_charge_discharge_loss_from_history": False}), "test", TEXTS
    )
    assert engine.run_job(snapshot(now, 5), engine.startup, {"power_values": _power_values_db()}).error is None
    plan(engine, now, 5)
    assert engine.calc_battery_loss_cost(1.0) == pytest.approx(0.25)
    assert engine.battery_efficiency() == pytest.approx(0.8**0.5)
    hours = charge_hours(engine)
    assert hours
    for ts in hours:
        session = engine.charge_hours[ts]
        assert session["battery_level"] == pytest.approx(engine.kwh_to_percentage(session["kWh"]) * 0.8**0.5, abs=0.05)


def test_configured_efficiency_curves_override_the_round_trip_loss() -> None:
    options = {
        **OPTIONS,
        "battery_charging_efficiency_curve": {"1000": 80, "5000": 96},
        "battery_discharging_efficiency_curve": {"1000": 75, "5000": 95},
    }
    engine = BattMindEngine(build_config(options), "test", TEXTS)

    assert engine.charging_efficiency(1.0) == pytest.approx(0.8)
    assert engine.charging_efficiency(3.0) == pytest.approx(0.88)
    assert engine.discharging_efficiency(1.0) == pytest.approx(0.75)
    assert engine.battery_efficiency() == pytest.approx((0.96 * 0.95) ** 0.5)
    assert engine.calc_battery_loss_cost(1.0) == pytest.approx((1.0 - 0.96 * 0.95) / (0.96 * 0.95))
    snap = snapshot(datetime.datetime.combine(DAY, datetime.time(3, 30)), 50)
    snap.settings["enhance_battery_efficiency"] = False
    assert engine.run_job(snap, engine.charging_efficiency, 1.0).value == pytest.approx(0.9**0.5)
    assert engine.run_job(snap, engine.discharging_efficiency, 1.0).value == pytest.approx(0.9**0.5)
    assert engine.run_job(snap, engine.effective_round_trip_loss).value == pytest.approx(0.1)


ALL_RULES = {
    key: True
    for key in (
        "cheapest_hour_fill_planner", "most_expensive_planner", "needed_before_max_level_planner", "only_discharge_on_profit",
        "prioritize_discharge_hours_by_energy_cost", "sell_excess_kwh_available", "most_profit",
        "force_powerwall_max_hours_old_discharge", "optimize_solar_charging",
    )
}
SOLAR_SURPLUS = [0.0] * 9 + [3.0] * 7 + [0.0] * 8  # 3 kWh/h 09-15 fills the battery on its own


def solar_engine(
    now: datetime.datetime, battery_level: float, solar: bool, extra_options: dict | None = None
) -> tuple[BattMindEngine, Snapshot]:
    """Engine with a sell price entity (buy - 0.9) and optionally a solar surplus prediction."""
    options = {**OPTIONS, "energidataservice_sell_entity_id": "sensor.sell", **(extra_options or {})}
    engine = BattMindEngine(build_config(options), "test", TEXTS)

    def with_sell_prices(snap: Snapshot) -> Snapshot:
        buy = snap.price_data["attributes"]["sensor.price"]
        snap.price_data["attributes"]["sensor.sell"] = {
            key: [{**row, "price": round(max(row["price"] - 0.9, 0.1), 3)} for row in rows] if isinstance(rows, list) else rows
            for key, rows in buy.items()
        }
        snap.entity_states["sensor.sell"] = "0.3"
        return snap

    assert engine.run_job(with_sell_prices(snapshot(now, battery_level)), engine.startup, {"power_values": _power_values_db()}).error is None
    if solar:
        engine.solar_prediction_db = {day: {"total": SOLAR_SURPLUS, "total_sell": [0.5] * 24} for day in range(8)}
    return engine, with_sell_prices(snapshot(now, battery_level, ALL_RULES))


@pytest.mark.parametrize("solar", [False, True])
@pytest.mark.parametrize("hour", [0, 7, 15, 19])
@pytest.mark.parametrize("battery_level", [5, 95])
def test_all_rules_plan_without_errors(solar: bool, hour: int, battery_level: float) -> None:
    """The rules catch their exceptions and notify, so check the notifications."""
    now = datetime.datetime.combine(DAY, datetime.time(hour, 30))
    engine, snap = solar_engine(now, battery_level, solar)
    result = engine.run_job(snap, engine.charge_if_needed, True)
    assert result.error is None, result.error
    assert not [message for message, title, _ in result.notifications if "error" in title.lower()]


def test_solar_surplus_has_priority_over_grid_charging() -> None:
    """Only the morning shortfall is bought before the sun fills the battery."""
    now = datetime.datetime.combine(DAY, datetime.time(3, 30))
    morning_kwh = {}
    for solar in (False, True):
        engine, snap = solar_engine(now, 30, solar)
        assert engine.run_job(snap, engine.charge_if_needed, True).error is None
        morning_kwh[solar] = sum(engine.charge_hours[ts]["kWh"] for ts in charge_hours(engine) if ts.date() == DAY and ts.hour < 9)
        if solar:
            # the sun still fills the battery completely
            assert max(sum(engine.plan[0]["battery_level_flow"][hour]) for hour in range(9, 16)) == pytest.approx(100.0)
    assert morning_kwh[True] < 2.0
    assert morning_kwh[False] > 5.0


def test_sell_value_with_fixed_solar_sell_price() -> None:
    """A fixed price settles the difference to the hourly price: fixed - hourly, also negative."""
    engine = BattMindEngine(build_config(OPTIONS), "test", TEXTS)
    assert engine.sell_value(5.0) == pytest.approx(5.0)
    engine = BattMindEngine(build_config({**OPTIONS, "production_price": 1.0}), "test", TEXTS)
    assert engine.sell_value(5.0) == pytest.approx(-4.0)
    assert engine.sell_value(0.5) == pytest.approx(0.5)


def test_fixed_solar_sell_price_values_every_sale_in_the_plan() -> None:
    """The optimiser sees fixed - hourly sell price per hour, not clamped at 0."""
    now = datetime.datetime.combine(DAY, datetime.time(3, 30))
    hourly, hourly_snap = solar_engine(now, 50, True)
    fixed, fixed_snap = solar_engine(now, 50, True, {"production_price": 1.0})
    assert hourly.run_job(hourly_snap, hourly.charge_if_needed, True).error is None
    assert fixed.run_job(fixed_snap, fixed.charge_if_needed, True).error is None
    raw = {row["hour"]: row["sell"] for row in hourly.optimizer_shadow["hours"]}
    values = {row["hour"]: row["sell"] for row in fixed.optimizer_shadow["hours"]}
    assert values.keys() == raw.keys()
    for hour, price in raw.items():
        assert values[hour] == pytest.approx(1.0 - price, abs=0.011)
    assert min(values.values()) < 0.0  # the evening sell price is above the fixed price


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
    assert set(result.db_saves) == {"charging_history", "power_values", "kwh_avg_prices", "solar_available", "analysis"}
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


def _price_history() -> dict:
    """Price history where the price tells the weekday: 2.<weekday> in every hour."""
    history = {hour: {dow: [2.0 + dow / 10] * 3 for dow in range(7)} for hour in range(24)}
    return {"history": history, "history_sell": deepcopy(history)}


def test_missing_hours_are_filled_from_the_price_history() -> None:
    """Only today's prices are published: the rest of the plan uses the price history of that weekday."""
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    snap = snapshot(now, 50)
    attributes = snap.price_data["attributes"]["sensor.price"]
    attributes.update(raw_tomorrow=[], tomorrow_valid=False, forecast=[])
    engine = BattMindEngine(build_config(OPTIONS), "test", TEXTS)
    assert engine.run_job(snap, engine.startup, {"power_values": _power_values_db(), "kwh_avg_prices": _price_history()}).error is None
    result = engine.run_job(snap, engine.charge_if_needed, True)
    assert result.error is None, result.error

    prices = engine.grid_price_cache["prices"]
    sources = engine.grid_price_cache["sources"]
    today_17 = now.replace(hour=17)
    assert prices[today_17] == pytest.approx(1.0 + 1.5 + 0.17)  # published price kept
    assert sources[today_17] == "energidataservice:real"
    for days in (1, 2):
        timestamp = now.replace(hour=17) + datetime.timedelta(days=days)
        assert sources[timestamp] == "database"
        assert prices[timestamp] == pytest.approx(round(2.0 + timestamp.weekday() / 10 + days / 30, 2))
    assert max(prices) == datetime.datetime.combine(DAY + datetime.timedelta(days=2), datetime.time(23))
    assert engine.grid_price_cache["using_offline_prices"] is False
    # the filled hours are planned with, so every hour of the plan has a price
    assert len(engine.optimizer_shadow["hours"]) == 12 + 24 + 24


def test_offline_prices_use_the_same_weekday() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    engine = BattMindEngine(build_config(OPTIONS), "test", TEXTS)
    empty = snapshot(now, 50)
    empty.price_data = {"attributes": {}, "values": {}}
    assert engine.run_job(empty, engine.startup, {"power_values": _power_values_db(), "kwh_avg_prices": _price_history()}).error is None
    assert engine.run_job(empty, engine.charge_if_needed, True).error is None
    prices = engine.grid_price_cache["prices"]
    for days in range(7):
        timestamp = now + datetime.timedelta(days=days)
        assert prices[timestamp] == pytest.approx(round(2.0 + timestamp.weekday() / 10 + days / 30, 2))


def test_price_history_stores_only_the_providers_prices() -> None:
    """Estimated hours are never stored as history, and sell prices are stored without solar panels too."""
    now = datetime.datetime.combine(DAY, datetime.time(12, 0))
    engine, snap = solar_engine(now, 50, False)
    engine.kwh_avg_prices_db = {}
    assert engine.run_job(snap, engine.update_grid_prices).error is None
    dow = DAY.weekday()
    history = engine.kwh_avg_prices_db
    assert history["history"][17][dow][0] == pytest.approx(1.0 + 1.5 + 0.17)
    assert history["history_sell"][17][dow][0] == pytest.approx(engine.price_provider.sell_prices[now.replace(hour=17)])

    # 3 hours of today missing: they are filled from the history, but the day isn't stored
    engine, snap = solar_engine(now, 50, False)
    engine.kwh_avg_prices_db = deepcopy(_price_history())
    rows = snap.price_data["attributes"]["sensor.price"]["raw_today"]
    snap.price_data["attributes"]["sensor.price"]["raw_today"] = [row for row in rows if row["hour"][11:13] not in ("14", "15", "16")]
    assert engine.run_job(snap, engine.update_grid_prices).error is None
    assert engine.grid_price_cache["sources"][now.replace(hour=15)] == "database"
    assert engine.kwh_avg_prices_db["history"][15][dow] == _price_history()["history"][15][dow]


def test_codec_roundtrip() -> None:
    data = {
        0: {"a": [[datetime.datetime(2026, 1, 1, 12), 1.5]]},
        (1, 2): "tuple key",
        "__dt__": "reserved key",
        "nested": {"when": datetime.datetime(2026, 5, 5)},
    }
    assert decode(json.loads(json.dumps(encode(data)))) == data
