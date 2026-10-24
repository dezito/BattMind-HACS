"""The analysis (engine_analysis.py): hourly recording, the day summary and the monthly sensor."""
from __future__ import annotations

import datetime

import pytest

from custom_components.battmind.engine_analysis import (
    grid_cost,
    hindsight_cost,
    rules_of_entries,
    split_flows,
    summarize_day,
)

from .test_engine import DAY, TZ, snapshot, started_engine

CHARGE_HOURS = (1, 2, 3)
DISCHARGE_HOURS = (17, 18, 19, 20)


def _level(hour: int) -> float:
    """Battery level at the end of hour (10 kWh battery: 3 kW charging, 1.5 kW discharging)."""
    level = 20.0
    for h in range(hour + 1):
        level += 30.0 if h in CHARGE_HOURS else -15.0 if h in DISCHARGE_HOURS else 0.0
    return level


def _buffers() -> dict:
    """600 W in the house all day; the battery charges at night and discharges in the evening
    (sensor.battery_power: + = discharging)."""
    start = datetime.datetime.combine(DAY, datetime.time(0), tzinfo=TZ)
    consumption, flow, level = [], [], []
    for hour in range(-1, 24):
        ts = (start + datetime.timedelta(hours=hour)).timestamp()
        consumption.append((ts, 600.0))
        flow.append((ts, -3000.0 if hour in CHARGE_HOURS else 1500.0 if hour in DISCHARGE_HOURS else 0.0))
        level.append((ts + 3599, _level(hour) if hour >= 0 else 20.0))
    return {"sensor.consumption": consumption, "sensor.battery_power": flow, "sensor.battery_level": level}


def _run_day(engine) -> None:
    buffers = _buffers()
    for hour in range(24):
        now = datetime.datetime.combine(DAY, datetime.time(hour, 59))
        snap = snapshot(now, _level(hour))
        snap.buffers = buffers
        result = engine.run_job(snap, engine.charge_if_needed, False)
        assert result.error is None, result.error
        result = engine.run_job(snap, engine.hour_end, now.replace(minute=0))
        assert result.error is None, result.error
    return result


def test_split_flows_solar_first() -> None:
    flows = split_flows(home=2.0, solar=3.0, charge=0.5, discharge=0.0)
    assert flows["solar_home"] == 2.0
    assert flows["solar_battery"] == 0.5
    assert flows["export_solar"] == pytest.approx(0.5)
    assert flows["grid_home"] == 0.0
    flows = split_flows(home=2.0, solar=0.5, charge=0.0, discharge=1.0)
    assert (flows["solar_home"], flows["battery_home"], flows["grid_home"]) == (0.5, 1.0, 0.5)


def test_hindsight_moves_energy_to_the_expensive_hours() -> None:
    rows = [{"load": 1.0, "buy": 1.0, "sell": 0.5}] * 4 + [{"load": 1.0, "buy": 3.0, "sell": 0.5}] * 4
    no_battery = sum(grid_cost(r["load"], r["buy"], r["sell"]) for r in rows)
    optimal = hindsight_cost(rows, start=1.0, end=1.0, low=1.0, high=9.0, max_charge=3.0, max_discharge=3.0, efficiency=1.0, wear=0.0)
    # 4 kWh moved from 1 kr to 3 kr hours (within a step of the 60 battery levels)
    assert optimal == pytest.approx(no_battery - 8.0, abs=0.15)
    lossy = hindsight_cost(rows, start=1.0, end=1.0, low=1.0, high=9.0, max_charge=3.0, max_discharge=3.0, efficiency=0.9, wear=0.1)
    assert optimal < lossy < no_battery


def test_rules_split_grid_and_solar_charging() -> None:
    entries = [
        {"rules": {"cheapest_hour_fill_planner": True, "solar": True},
         "prices": {"buy_price": 1.0, "sell_price": 0.5},
         "charged": {"kWh": 3.0, "kWh_from_local_energy": 1.0, "cost": 2.5}},
        {"rules": {"force_discharge": True, "most_profit": True},
         "discharged": {"kWh": 2.0, "cost": 1.0, "savings": 3.0}},
        {"rules": {}, "discharged": {"kWh": 1.0, "cost": 0.5, "savings": 1.0}},
    ]
    rules = rules_of_entries(entries)
    assert rules["solar"]["c"] == 1.0
    assert rules["solar"]["cc"] == pytest.approx(0.5)  # 1 kWh at 0.5 against 2 kWh at 1.0
    assert rules["cheapest_hour_fill_planner"] == {"c": 2.0, "cc": 2.0, "d": 0.0, "v": 0.0, "s": 0.0}
    assert rules["most_profit"]["s"] == 3.0
    assert rules["most_profit"]["v"] == 4.0
    assert rules["discharging"]["d"] == 1.0


def test_expensive_hours_with_an_empty_battery() -> None:
    rows = [{"home": 1.0, "solar": 0.0, "charge": 0.0, "discharge": 0.0, "soc": 5.0, "buy": float(hour)} for hour in range(24)]
    day = summarize_day(rows, soc_start=5.0, minimum=5.0)
    assert day["expensive"]["hours"] == 4
    assert day["expensive"]["cost"] == pytest.approx(20 + 21 + 22 + 23)
    rows = [{**row, "soc": 50.0} for row in rows]
    assert summarize_day(rows, soc_start=50.0, minimum=5.0)["expensive"]["hours"] == 0


def test_a_day_is_recorded_and_published() -> None:
    engine = started_engine(datetime.datetime.combine(DAY, datetime.time(0, 5)), 20.0)
    result = _run_day(engine)

    day = engine.analysis_db["days"][DAY.isoformat()]
    assert day["final"] is True
    assert day["hours"] == 24
    assert day["charged"] == pytest.approx(9.0, abs=0.1)
    assert day["discharged"] == pytest.approx(6.0, abs=0.1)
    assert day["battery_home"] == pytest.approx(6.0, abs=0.1)
    assert day["soc_end"] == _level(23)
    assert set(day["forecast"]) == {"hours", "use", "ema", "trend", "average"}
    score = day["score"]
    assert score["optimal"] <= score["actual"]
    # tomorrow's plan is saved at 23
    assert (DAY + datetime.timedelta(days=1)).isoformat() in engine.analysis_db["plans"]

    attributes = result.attributes["analysis"]
    month = attributes["months"][-1]
    assert month["month"] == DAY.strftime("%Y-%m")
    assert month["charged"] == pytest.approx(9.0, abs=0.1)
    assert month["self_sufficiency"] == pytest.approx(day["battery_home"] / day["home"], abs=0.01)
    assert {rule["rule"] for rule in attributes["rules"]} >= {"discharging"}


def test_backfill_from_the_statistics() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(12, 5))
    engine = started_engine(now, 50.0)
    first = datetime.datetime.combine(DAY - datetime.timedelta(days=10), datetime.time(0), tzinfo=TZ)
    hours = [(first + datetime.timedelta(hours=h)).timestamp() for h in range(10 * 24 + 12)]
    stats = {
        "sensor.consumption": [(ts, 500.0) for ts in hours],
        "sensor.battery_power": [(ts, 1000.0 if datetime.datetime.fromtimestamp(ts, TZ).hour == 18 else 0.0) for ts in hours],
        "sensor.battery_level": [(ts, 50.0) for ts in hours],
        "sensor.price": [(ts, 1.5) for ts in hours],
    }
    result = engine.run_job(snapshot(now, 50.0), engine.analysis_update, {"stats": stats, "price_entity_id": "sensor.price"})
    assert result.error is None, result.error
    assert engine.analysis_backfilled()
    old = engine.analysis_db["days"][(DAY - datetime.timedelta(days=5)).isoformat()]
    assert old["statistics"] is True
    assert old["discharged"] == pytest.approx(1.0)
    assert old["price_hours"] == 24
    # the last two days are kept per hour, today is not finished
    assert any(key.startswith(DAY.isoformat()) for key in engine.analysis_db["hours"])
    assert not engine.analysis_db["days"][DAY.isoformat()].get("final")
    months = result.attributes["analysis"]["months"]
    assert months[-1]["avg_price"] == pytest.approx(1.5)

    # an older backfill (without prices) is read again and replaces the days from the statistics
    engine.analysis_db["backfilled"] = True
    assert not engine.analysis_backfilled()
    stats["sensor.price"] = [(ts, 2.0) for ts in hours]
    result = engine.run_job(snapshot(now, 50.0), engine.analysis_update, {"stats": stats, "price_entity_id": "sensor.price"})
    assert result.error is None, result.error
    assert engine.analysis_backfilled()
    assert engine.analysis_db["days"][(DAY - datetime.timedelta(days=5)).isoformat()]["price_sum"] == pytest.approx(48.0)
