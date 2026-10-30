"""The analysis (engine/analysis): hourly recording, the day summary and the monthly sensor."""
from __future__ import annotations

import datetime

import pytest

from custom_components.battmind.engine.analysis.helpers import (
    evaluate_price_day,
    grid_cost,
    hindsight_cost,
    rules_of_entries,
    split_flows,
    summarize_day,
)

from .test_efficiency import _battery
from .test_engine import DAY, TZ, _price_rows, snapshot, started_engine

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
    # 4 kWh moved from 1.00 to 3.00 price hours (within a step of the 60 battery levels)
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


def test_efficiency_curves_are_published_for_the_analysis_chart() -> None:
    engine = started_engine(datetime.datetime.combine(DAY, datetime.time(0, 5)), 50.0)
    engine.config["solar"].update(
        battery_charging_efficiency_curve={"5000": 96, "1000": 80},
        battery_discharging_efficiency_curve={"500": 82, "3000": 94},
    )

    result = engine.run_job(snapshot(datetime.datetime.combine(DAY, datetime.time(0, 5)), 50.0), engine.publish_analysis)

    assert result.attributes["analysis"]["efficiency_curves"] == {
        "enabled": True,
        "charging": [{"power": 1000, "efficiency": 80.0}, {"power": 5000, "efficiency": 96.0}],
        "discharging": [{"power": 500, "efficiency": 82.0}, {"power": 3000, "efficiency": 94.0}],
        "source": {"charging": "configured", "discharging": "configured"},
        "measured": {"charging": [], "discharging": []},
    }


def _measured_engine():
    """Engine after an hour end with a night of charging at 3 kW (90 %) and discharging at 2 kW (95 %)
    in the buffers (sensor.battery_power: + = discharging)."""
    start = datetime.datetime.combine(DAY, datetime.time(1, 0), tzinfo=TZ).timestamp()
    flow, levels = _battery([(3 * 3600, 3000, 0.9), (600, 0, 1.0), (3 * 3600, -2000, 0.95)], level=10.0, sign=-1.0, start=start)
    now = datetime.datetime.combine(DAY, datetime.time(8, 59))
    engine = started_engine(now, levels[-1][1])
    snap = snapshot(now, levels[-1][1])
    snap.buffers = {"sensor.battery_power": flow, "sensor.battery_level": levels}
    snap.units["sensor.battery_level"] = "%"
    result = engine.run_job(snap, engine.hour_end, now.replace(minute=0))
    assert result.error is None, result.error
    return engine, now, result, snap.buffers


def test_efficiency_is_measured_every_hour_and_used_without_a_configured_curve() -> None:
    engine, now, result, buffers = _measured_engine()

    curves = result.attributes["analysis"]["efficiency_curves"]
    assert curves["source"] == {"charging": "measured", "discharging": "measured"}
    assert curves["charging"] == [{"power": 3250, "efficiency": 90.0}]
    assert curves["discharging"] == [{"power": 2250, "efficiency": 95.0}]
    assert curves["measured"]["charging"][0]["used"] is True
    assert engine.analysis_db["efficiency"]["until"] > 0
    snap = snapshot(now, 50)
    assert engine.run_job(snap, engine.charging_efficiency, 1.0).value == pytest.approx(0.9, abs=1e-4)
    assert engine.run_job(snap, engine.discharging_efficiency, 5.0).value == pytest.approx(0.95, abs=1e-4)

    # the next hour continues where the last one stopped: nothing is counted twice
    before = {direction: dict(engine.analysis_db["efficiency"][direction]) for direction in ("charging", "discharging")}
    later = now.replace(hour=9)
    snap = snapshot(later, 50)
    snap.buffers = buffers
    assert engine.run_job(snap, engine._analysis_record_efficiency).error is None
    assert {direction: engine.analysis_db["efficiency"][direction] for direction in before} == before


def test_configured_curve_takes_precedence_over_the_measured_one() -> None:
    engine, now, _, _ = _measured_engine()
    engine.config["solar"]["battery_charging_efficiency_curve"] = {"1000": 80, "5000": 96}
    snap = snapshot(now, 50)

    assert engine.run_job(snap, engine.charging_efficiency, 1.0).value == pytest.approx(0.8)
    assert engine.run_job(snap, engine.discharging_efficiency, 5.0).value == pytest.approx(0.95, abs=1e-4)
    snap.settings["enhance_battery_efficiency"] = False
    assert engine.run_job(snap, engine.discharging_efficiency, 5.0).value == pytest.approx(0.9**0.5)


def test_what_if_uses_the_measured_curves_as_configured() -> None:
    engine, now, _, _ = _measured_engine()

    config, _settings = engine.run_job(snapshot(now, 50), engine._what_if_setup, {}).value

    assert config["solar"]["battery_charging_efficiency_curve"] == {"3250": 90.0}
    assert config["solar"]["battery_discharging_efficiency_curve"] == {"2250": 95.0}
    assert engine.config["solar"]["battery_charging_efficiency_curve"] == {}




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


def test_price_day_is_judged_per_days_ahead() -> None:
    rows = [{"real": hour / 10, "1": hour / 10 + 0.1, "2": (23 - hour) / 10, **({"3": 1.0} if hour < 10 else {})} for hour in range(24)]
    day = evaluate_price_day(rows)
    # 1 day ahead: 0.1 too high but the same order
    assert day["1"]["n"] == 24
    assert day["1"]["abs"] / 24 == pytest.approx(0.1)
    assert day["1"]["bias"] / 24 == pytest.approx(0.1)
    assert (day["1"]["cheap"], day["1"]["cheap_miss"], day["1"]["dear"], day["1"]["dear_miss"]) == (4, 0.0, 4, 0.0)
    # 2 days ahead: the order turned around, the "cheap" hours were the 4 most expensive
    assert day["2"]["cheap"] == 0
    assert day["2"]["cheap_miss"] == pytest.approx(2.0)
    assert day["2"]["dear_miss"] == pytest.approx(2.0)
    # 3 days ahead: too few hours
    assert "3" not in day


def test_price_forecast_is_compared_with_the_real_prices() -> None:
    tomorrow = DAY + datetime.timedelta(days=1)
    engine = started_engine(datetime.datetime.combine(DAY, datetime.time(0, 5)), 50.0)

    def run(now: datetime.datetime, snap):
        assert engine.run_job(snap, engine.update_grid_prices).error is None
        result = engine.run_job(snap, engine.charge_if_needed, False)
        assert result.error is None, result.error
        result = engine.run_job(snap, engine.hour_end, now.replace(minute=0))
        assert result.error is None, result.error
        return result

    # the morning: tomorrow is only a forecast (0.1 above the real prices that come at 13)
    morning = datetime.datetime.combine(DAY, datetime.time(10, 59))
    snap = snapshot(morning, 50.0)
    snap.price_data["attributes"]["sensor.price"].update(
        raw_tomorrow=[], tomorrow_valid=False,
        forecast=_price_rows(tomorrow, 0.9) + _price_rows(tomorrow + datetime.timedelta(days=1), 1.1),
    )
    run(morning, snap)
    hours = engine.analysis_db["price_hours"]
    forecast = {row["hour"][11:13]: row["price"] for row in _price_rows(tomorrow, 0.9)}
    # without the daily adder of the providers
    assert hours[f"{tomorrow.isoformat()}T05"]["1"] == pytest.approx(forecast["05"], abs=0.006)
    assert "2" in hours[f"{(tomorrow + datetime.timedelta(days=1)).isoformat()}T05"]
    assert "real" not in hours[f"{tomorrow.isoformat()}T05"]
    assert not engine.analysis_db["price_days"].get(tomorrow.isoformat())

    # 13: the real prices of tomorrow are known
    afternoon = datetime.datetime.combine(DAY, datetime.time(13, 59))
    result = run(afternoon, snapshot(afternoon, 50.0))
    judged = engine.analysis_db["price_days"][tomorrow.isoformat()]["1"]
    assert judged["n"] == 24
    assert judged["bias"] / 24 == pytest.approx(0.1, abs=0.006)
    assert (judged["cheap"], judged["dear"]) == (4, 4)

    published = result.attributes["analysis"]["price_forecast"]
    assert published["last_30"][0]["lead"] == 1
    assert published["last_30"][0]["mae"] == pytest.approx(0.1, abs=0.006)
    assert published["days"][-1]["date"] == tomorrow.isoformat()
    row = next(row for row in published["hours"] if row["t"] == f"{tomorrow.isoformat()}T05")
    assert row["real"] is not None and row["d1"] is not None


def test_price_forecast_is_corrected_with_its_bias() -> None:
    tomorrow = DAY + datetime.timedelta(days=1)
    now = datetime.datetime.combine(DAY, datetime.time(10, 5))
    engine = started_engine(now, 50.0)
    days = engine.analysis_db["price_days"]
    for back in range(1, 8):
        days[(DAY - datetime.timedelta(days=back)).isoformat()] = {
            # 1 day ahead 0.2 too high; 3 days ahead 2.00 too low (capped at 0.5)
            "1": {"n": 24, "abs": 4.8, "bias": 4.8},
            "3": {"n": 24, "abs": 48.0, "bias": -48.0},
            # 2 days ahead: too few days
            **({"2": {"n": 24, "abs": 2.4, "bias": 2.4}} if back <= 3 else {}),
        }
    assert engine.price_forecast_corrections() == {1: -0.2, 3: 0.5}

    snap = snapshot(now, 50.0)
    snap.price_data["attributes"]["sensor.price"].update(
        raw_tomorrow=[], tomorrow_valid=False,
        forecast=_price_rows(tomorrow, 0.9) + _price_rows(tomorrow + datetime.timedelta(days=1), 1.1),
    )
    assert engine.run_job(snap, engine.update_grid_prices).error is None
    provider = engine.price_provider.buy_prices
    prices = engine.get_grid_prices()
    hour = datetime.datetime.combine(tomorrow, datetime.time(5))
    later = hour + datetime.timedelta(days=1)
    today = datetime.datetime.combine(DAY, datetime.time(15))
    assert prices[hour] == pytest.approx(provider[hour] - 0.2)
    assert prices[later] == provider[later]
    assert prices[today] == provider[today]  # real prices are never corrected

    result = engine.run_job(snap, engine.hour_end, now.replace(minute=0))
    published = result.attributes["analysis"]["price_forecast"]
    assert {entry["lead"]: entry["correction"] for entry in published["last_30"]} == {1: -0.2, 2: None, 3: 0.5}
    row = next(row for row in published["hours"] if row["t"] == f"{tomorrow.isoformat()}T05")
    assert row["corrected"] == pytest.approx(row["d1"] - 0.2)

    # the switch off: the cached prices are recalculated without a new price update
    snap.settings["price_forecast_correction"] = False
    assert engine.run_job(snap, engine.hour_end, now.replace(minute=0)).error is None
    assert engine.run_job(snap, engine.price_forecast_corrections).value == {}
    assert engine.run_job(snap, engine.get_grid_prices).value[hour] == provider[hour]
