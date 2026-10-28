"""The newer planner rules and the whole horizon optimiser (shadow plan)."""
from __future__ import annotations

import datetime

import pytest

from custom_components.battmind.const import build_config
from custom_components.battmind.engine import BattMindEngine, Snapshot
from custom_components.battmind.engine.optimizer import (
    RULE_SWITCHES,
    OptimizerConfig,
    OptimizerHour,
    optimize,
    simulate,
    step_hour,
)
from custom_components.battmind.engine.planner.what_if import _nearest_eds_tariffs
from custom_components.battmind.prices.base import calculate_sell_price

from .test_engine import DAY, OPTIONS, SOLAR_SURPLUS, TEXTS, _power_values_db, charge_hours, snapshot

NEGATIVE_HOURS = (1, 2, 3, 12, 13)
OFF = {
    "optimizer_control": False,
    "cheapest_hour_fill_planner": False,
    "needed_before_max_level_planner": False,
    "only_discharge_on_profit": False,
}


def test_what_if_uses_nearest_historical_eds_tariff_and_live_sell_formula() -> None:
    history = [
        (
            100.0,
            {
                "additional_tariffs": {
                    "transmissions_nettarif": 0.1,
                    "systemtarif": 0.1,
                    "elafgift": 0.5,
                },
                "tariffs": {"12": 0.2},
            },
        ),
        (
            300.0,
            {
                "additional_tariffs": {
                    "transmissions_nettarif": 0.1,
                    "systemtarif": 0.1,
                    "elafgift": 0.8,
                },
                "tariffs": {"12": 0.3},
            },
        ),
    ]

    nearest = _nearest_eds_tariffs(history, 200.0, 12)

    assert nearest is not None
    assert nearest == (
        {
            "transmissions_nettarif": 0.1,
            "systemtarif": 0.1,
            "elafgift": 0.5,
            "tariffs": 0.2,
            "surcharge": 0.0,
        },
        False,
    )
    assert calculate_sell_price(0.8, nearest[0], 0.03) == pytest.approx(0.13)


def realistic_snapshot(now: datetime.datetime, battery_level: float, settings: dict | None = None, negative: bool = False) -> Snapshot:
    """Sell price = buy - 0.9; negative: buy -0.4 (sell -0.9) in NEGATIVE_HOURS today."""
    snap = snapshot(now, battery_level, settings)
    buy = snap.price_data["attributes"]["sensor.price"]
    if negative:
        for row in buy["raw_today"]:
            if datetime.datetime.fromisoformat(row["hour"]).hour in NEGATIVE_HOURS:
                row["price"] = -0.4
    snap.price_data["attributes"]["sensor.sell"] = {
        key: [{**row, "price": round(row["price"] - (0.9 if row["price"] > 0 else 0.5), 3)} for row in rows] if isinstance(rows, list) else rows
        for key, rows in buy.items()
    }
    snap.entity_states["sensor.sell"] = "0.3"
    return snap


def planned(hour: int, battery_level: float, settings: dict, solar: bool = False, negative: bool = False, days: int | None = None) -> tuple[BattMindEngine, object]:
    now = datetime.datetime.combine(DAY, datetime.time(hour, 30))
    options = {**OPTIONS, "energidataservice_sell_entity_id": "sensor.sell"}
    if days:
        options["days_to_predict"] = days
    engine = BattMindEngine(build_config(options), "test", TEXTS)
    assert engine.run_job(realistic_snapshot(now, battery_level, negative=negative), engine.startup, {"power_values": _power_values_db()}).error is None
    if solar:
        engine.solar_prediction_db = {day: {"total": SOLAR_SURPLUS, "total_sell": [0.5] * 24} for day in range(9)}
    result = engine.run_job(realistic_snapshot(now, battery_level, settings, negative=negative), engine.charge_if_needed, True)
    assert result.error is None, result.error
    assert not [message for message, title, _ in result.notifications if "error" in title.lower()]
    return engine, result


def sales(engine: BattMindEngine) -> dict:
    return {ts: sale for day in engine.plan if isinstance(day, int) for ts, sale in engine.plan[day]["force_discharge_timestamps"].items()}


def test_negative_prices_fill_the_battery() -> None:
    engine, _ = planned(0, 30, {**OFF, "negative_price_planner": True}, negative=True)
    hours = [ts for ts in charge_hours(engine) if engine.charge_hours[ts].get("negative_price_planner")]
    assert hours and {ts.hour for ts in hours} <= set(NEGATIVE_HOURS)
    assert max(sum(engine.plan[0]["battery_level_flow"][hour]) for hour in (1, 2, 3)) == pytest.approx(100.0, abs=1.0)


def test_grid_charge_to_sell_buys_cheap_and_sells_expensive() -> None:
    engine, _ = planned(0, 5, {**OFF, "grid_charge_to_sell": True})
    bought = [ts for ts in charge_hours(engine) if engine.charge_hours[ts].get("grid_charge_to_sell")]
    sold = {ts: sale for ts, sale in sales(engine).items() if sale["title"] == "Sælger strøm købt billigt"}
    assert bought and sold
    assert all(sale["profit"] > 0.0 for sale in sold.values())
    assert min(sold) > min(bought)


def test_discharge_before_solar_when_the_solar_export_costs_money() -> None:
    """Solar surplus sold at -0.9 at noon: empty the battery in the morning so the sun has room."""
    engine, _ = planned(4, 95, {**OFF, "discharge_before_solar": True}, solar=True, negative=True)
    before_solar = [ts for ts, sale in sales(engine).items() if sale["title"] == "Sælger før solen fylder batteriet"]
    assert before_solar and all(ts.date() == DAY and ts.hour < 9 for ts in before_solar)


@pytest.mark.parametrize("optimizer_control", [False, True])
@pytest.mark.parametrize("negative", [False, True])
def test_never_charge_and_sell_in_the_same_hour(negative: bool, optimizer_control: bool) -> None:
    rules = {
        key: True
        for key in (
            "most_expensive_planner", "prioritize_discharge_hours_by_energy_cost", "sell_excess_kwh_available", "most_profit",
            "negative_price_planner", "grid_charge_to_sell", "discharge_before_solar", "optimize_solar_charging",
        )
    }
    engine, _ = planned(0, 30, {**rules, "optimizer_control": optimizer_control}, solar=True, negative=negative)
    charged = {ts.replace(minute=0) for ts in charge_hours(engine)}
    sold = {ts.replace(minute=0) for ts in sales(engine)}
    assert charged and not charged & sold


@pytest.mark.parametrize("days", [2, 5])
def test_shadow_plan_covers_days_to_predict_and_beats_the_rules(days: int) -> None:
    engine, result = planned(3, 30, {"optimizer_control": False, "most_profit": True}, solar=True, days=days)
    shadow = engine.optimizer_shadow
    # prices exist for today + 3 days, the horizon is days_to_predict as far as there are prices
    assert len(shadow["hours"]) == 21 + (min(days, 4) - 1) * 24
    assert shadow["optimal"] <= shadow["rules"] + 0.01
    assert shadow["optimal"] <= shadow["self_consumption"] + 0.01
    summary = result.attributes["powerwall_action"]["optimizer_shadow_summary"]
    assert summary["optimal"] == shadow["optimal"]


def test_what_if_simulator_does_not_change_the_active_plan() -> None:
    now = datetime.datetime.combine(DAY, datetime.time(3, 30))
    engine, _ = planned(3, 40, {"optimizer_control": True}, solar=True)
    active_plan = engine.optimizer_shadow
    snap = realistic_snapshot(now, 40, {"optimizer_control": True})
    ids = engine.config["home"]["entity_ids"]
    solar_id = engine.config["solar"]["entity_ids"]["production_entity_id"]
    buy_id, sell_id = "sensor.historical_buy", "sensor.historical_sell"
    start = datetime.datetime.combine(DAY - datetime.timedelta(days=1), datetime.time())
    timestamps = [(start + datetime.timedelta(hours=hour)).timestamp() for hour in range(48)]
    data = {
        "buy_entity_id": buy_id,
        "sell_entity_id": sell_id,
        "stats": {
            ids["power_consumption_entity_id"]: [(ts, 1000.0) for ts in timestamps],
            ids["powerwall_watt_flow_entity_id"]: [(ts, 0.0) for ts in timestamps],
            solar_id: [
                (ts, 1500.0 if 9 <= datetime.datetime.fromtimestamp(ts).hour <= 15 else 0.0)
                for ts in timestamps
            ],
            buy_id: [(ts, 0.5 if datetime.datetime.fromtimestamp(ts).hour < 6 else 2.0) for ts in timestamps],
            sell_id: [(ts, 0.4 if datetime.datetime.fromtimestamp(ts).hour < 6 else 1.5) for ts in timestamps],
        },
    }
    result = engine.run_job(
        snap,
        engine.what_if,
        data,
        {
            "battery_size": 20,
            "max_charging_power": 8000,
            "round_trip_loss": 5,
            "wear_cost_per_kwh": 0,
        },
    )

    assert result.error is None, result.error
    simulation = result.value
    assert simulation["assumptions"]["battery_size"] == 20
    assert simulation["assumptions"]["max_charging_power"] == 8000
    assert simulation["assumptions"]["round_trip_loss"] == pytest.approx(5)
    assert simulation["scenario"]["start"] == simulation["current"]["start"]
    assert simulation["period"]["hours"] == 48
    assert simulation["period"]["days"] == 2.0
    assert simulation["data_sources"]["long_term_statistics_samples"] > 0
    assert simulation["data_sources"]["recorder_history_fallback_samples"] == 0
    assert "plan" not in simulation
    assert engine.optimizer_shadow is active_plan

    unchanged = engine.run_job(snap, engine.what_if, data, {})
    assert unchanged.error is None, unchanged.error
    assert unchanged.value["difference"] == {"profit": 0.0, "bill": 0.0}

    no_sell_history = engine.run_job(
        snap,
        engine.what_if,
        {**data, "sell_price_history_available": False},
        {},
    )
    assert no_sell_history.error is None, no_sell_history.error
    assert no_sell_history.value["assumptions"]["sell_price_history_available"] is False
    assert no_sell_history.value["current"]["without"]["solar_income"] == 0.0

    changed_rules = engine.run_job(
        snap,
        engine.what_if,
        data,
        {"most_profit": False, "min_profit_per_kwh": 0.25},
    )
    assert changed_rules.error is None, changed_rules.error
    assert (
        changed_rules.value["current_assumptions"]["rules"]["most_profit"]
        == engine.enabled("most_profit")
    )
    assert changed_rules.value["assumptions"]["rules"]["most_profit"] is False
    assert changed_rules.value["assumptions"]["min_profit_per_kwh"] == pytest.approx(0.25)

    profitable_sell_data = {
        **data,
        "stats": {
            **data["stats"],
            sell_id: [
                (ts, 3.0 if datetime.datetime.fromtimestamp(ts).hour >= 6 else 0.4)
                for ts in timestamps
            ],
        },
    }
    no_sales = engine.run_job(
        snap,
        engine.what_if,
        profitable_sell_data,
        {
            "most_profit": False,
            "sell_excess_kwh_available": False,
            "force_powerwall_max_hours_old_discharge": False,
            "discharge_before_solar": False,
            "grid_charge_to_sell": False,
        },
    )
    assert no_sales.error is None, no_sales.error
    assert no_sales.value["scenario"]["profit"] < no_sales.value["current"]["profit"]

    eds_buy_id, eds_sell_id = "sensor.eds_buy", "sensor.eds_sell"
    provider_stats = {
        **data["stats"],
        buy_id: [(ts, value) for ts, value in data["stats"][buy_id] if ts >= timestamps[24]],
        sell_id: [(ts, value) for ts, value in data["stats"][sell_id] if ts >= timestamps[24]],
        eds_buy_id: [(ts, 1.0) for ts in timestamps],
        eds_sell_id: [(ts, 0.8) for ts in timestamps],
    }
    eds_provider = {
        "name": "Energi Data Service",
        "buy_entity_id": eds_buy_id,
        "sell_entity_id": eds_sell_id,
        "sell_fee": 0.03,
        "sell_tariff_history": [
            (
                timestamps[0],
                {
                    "additional_tariffs": {
                        "transmissions_nettarif": 0.1,
                        "systemtarif": 0.1,
                        "elafgift": 0.5,
                    },
                    "tariffs": {str(hour): 0.2 for hour in range(24)},
                },
            ),
            (
                timestamps[24],
                {
                    "additional_tariffs": {
                        "transmissions_nettarif": 0.1,
                        "systemtarif": 0.1,
                        "elafgift": 0.8,
                    },
                    "tariffs": {str(hour): 0.3 for hour in range(24)},
                },
            ),
        ],
    }
    providers = [
        {"name": "Strømligning", "buy_entity_id": buy_id, "sell_entity_id": sell_id},
        eds_provider,
    ]
    provider_data = {**data, "stats": provider_stats, "price_providers": providers}
    provider_result = engine.run_job(snap, engine.what_if, provider_data, {})
    assert provider_result.error is None, provider_result.error
    assert provider_result.value["price_provider"] == "Energi Data Service"
    assert provider_result.value["period"]["hours"] == len(timestamps)
    assert provider_result.value["tariff_estimated_hours"] == len(timestamps) - 2
    assert provider_result.value["tariff_unavailable_hours"] == 0
    missing_tariffs = engine.run_job(
        snap,
        engine.what_if,
        {
            **provider_data,
            "price_providers": [
                providers[0],
                {**eds_provider, "sell_tariff_history": []},
            ],
        },
        {},
    )
    assert missing_tariffs.error is None, missing_tariffs.error
    assert missing_tariffs.value["tariff_unavailable_hours"] == len(timestamps)
    assert missing_tariffs.value["current"]["without"]["solar_income"] == 0.0
    assert missing_tariffs.value["current"]["with"]["sold_kwh"] == 0.0

    missing_indices = (12, 35)
    incomplete_stats = dict(data["stats"])
    for entity_id, gap_index in zip(
        (buy_id, ids["powerwall_watt_flow_entity_id"]), missing_indices
    ):
        incomplete_stats[entity_id] = [
            row for row in incomplete_stats[entity_id] if row[0] != timestamps[gap_index]
        ]
    incomplete = engine.run_job(
        snap, engine.what_if, {**data, "stats": incomplete_stats}, {}
    )
    assert incomplete.error is None, incomplete.error
    assert incomplete.value["period"]["hours"] == len(timestamps) - len(missing_indices)
    assert incomplete.value["period"]["start"] == datetime.datetime.fromtimestamp(
        timestamps[0], snap.tz
    ).replace(tzinfo=None).isoformat()
    assert incomplete.value["period"]["end"] == datetime.datetime.fromtimestamp(
        timestamps[-1], snap.tz
    ).replace(tzinfo=None).isoformat()
    statistics_per_hour = (
        unchanged.value["data_sources"]["long_term_statistics_samples"] // len(timestamps)
    )
    assert incomplete.value["data_sources"]["long_term_statistics_samples"] == (
        unchanged.value["data_sources"]["long_term_statistics_samples"]
        - statistics_per_hour * len(missing_indices)
    )


CFG = OptimizerConfig(battery_size=10.0, level_min=5.0, level_max=100.0, max_charge=5.0, max_discharge=5.0, efficiency=0.95, wear=0.1)


def test_optimizer_charges_cheap_for_the_expensive_hour() -> None:
    hours = [OptimizerHour(0, 0.5, 0.0, 1.0, 0.0), OptimizerHour(1, 3.0, 0.0, 3.0, 0.0)]
    result = optimize(hours, 5.0, CFG, end_value_per_kwh=0.0)
    assert result.actions == ["grid_charging", "discharge_allowed"]
    assert result.total < simulate(hours, 5.0, CFG, ["discharge_allowed"] * 2, 0.0).total


def test_optimizer_keeps_energy_worth_more_than_selling_it() -> None:
    hours = [OptimizerHour(0, 1.0, 0.2, 0.0, 0.0)]
    assert optimize(hours, 80.0, CFG, end_value_per_kwh=1.0).actions == ["discharge_allowed"]
    assert optimize(hours, 80.0, CFG, end_value_per_kwh=0.0).actions == ["force_discharge"]


def test_optimizer_respects_min_profit_per_kwh() -> None:
    """A grid charging hour charges at full rate (like charge_if_needed), ~0.13/kWh profit here."""
    hours = [OptimizerHour(0, 1.0, 0.0, 0.0, 0.0), OptimizerHour(1, 1.4, 0.0, 5.0, 0.0)]
    assert optimize(hours, 5.0, CFG, 0.0).actions[0] == "grid_charging"
    strict = OptimizerConfig(**{**CFG.__dict__, "min_profit_per_kwh": 0.5})
    assert optimize(hours, 5.0, strict, 0.0).actions[0] != "grid_charging"


def test_optimizer_never_sells_in_excluded_hours() -> None:
    hours = [OptimizerHour(0, 1.0, 5.0, 0.0, 0.0, can_sell=False)]
    assert optimize(hours, 80.0, CFG, end_value_per_kwh=0.0).actions != ["force_discharge"]


def test_usage_reserve_is_kept_for_house_consumption() -> None:
    """Forced export keeps the reserve, while normal self-consumption may use it."""
    cfg = OptimizerConfig(**{**CFG.__dict__, "usage_reserve": 40.0})
    sale = step_hour(OptimizerHour(0, 1.0, 5.0, 0.0, 0.0), 50.0, "force_discharge", cfg)
    house = step_hour(OptimizerHour(0, 5.0, 0.0, 3.0, 0.0), 50.0, "discharge_allowed", cfg)

    assert sale[0] == pytest.approx(40.0, abs=0.1)
    assert house[0] < 40.0


def test_house_use_has_priority_over_export_reserve() -> None:
    """House use may cross the reserve in a sale hour, but no energy is exported below it."""
    cfg = OptimizerConfig(**{**CFG.__dict__, "usage_reserve": 40.0})
    details = {}
    level, _, _, _ = step_hour(
        OptimizerHour(0, 5.0, 5.0, 2.0, 0.0), 50.0, "force_discharge", cfg, details=details
    )

    assert level < 40.0
    assert details["house"] == pytest.approx(2.0)
    assert details["sold"] == pytest.approx(0.0)


def test_simulated_sale_is_limited_to_the_planned_kwh() -> None:
    """A rule sale of 0.5 kWh in a 15 min period must not be simulated as a full hour of discharging."""
    hours = [OptimizerHour(0, 1.0, 0.5, 0.0, 0.0)]
    full = simulate(hours, 80.0, CFG, ["force_discharge"], 0.0)
    planned = simulate(hours, 80.0, CFG, ["force_discharge"], 0.0, [0.5])
    assert planned.levels[0] == pytest.approx(80.0 - 0.5 / 0.95 / 10.0 * 100.0, abs=0.1)
    assert full.levels[0] < planned.levels[0]


def test_optimizer_interpolates_efficiency_at_the_actual_power() -> None:
    cfg = OptimizerConfig(
        **{
            **CFG.__dict__,
            "charge_efficiency_curve": ((1000.0, 0.8), (5000.0, 0.96)),
            "discharge_efficiency_curve": ((1000.0, 0.75), (5000.0, 0.95)),
        }
    )
    hour = OptimizerHour(0, 1.0, 0.0, 1.0, 0.0)
    charged = step_hour(hour, 5.0, "grid_charging", cfg, limit=1.0)
    discharged = step_hour(hour, 50.0, "discharge_allowed", cfg)

    assert charged[0] == pytest.approx(13.0)
    assert discharged[0] == pytest.approx(50.0 - 1.0 / 0.75 / 10.0 * 100.0)


def test_min_total_profit_does_not_block_using_the_battery() -> None:
    """min_total_profit is for trades - an hour of house consumption earns less than 1.00 but must not be blocked."""

    def no_profit_blocks(min_total_profit: float) -> set:
        engine, _ = planned(17, 90, {"only_discharge_on_profit": True, "min_total_profit": min_total_profit})
        return {
            ts for day in engine.plan if isinstance(day, int)
            for ts, reason in engine.plan[day]["blocked_discharge_timestamps"].items() if "Ingen fortjeneste" in reason
        }

    assert no_profit_blocks(100.0) == no_profit_blocks(0.0)



@pytest.mark.parametrize("negative", [False, True])
def test_optimizer_controls_the_battery(negative: bool) -> None:
    """The optimiser's actions become the plan charge_if_needed() executes, and its battery levels match the plan."""
    engine, result = planned(0, 30, {}, solar=True, negative=negative)
    shadow = engine.optimizer_shadow
    assert shadow["controls"] is True
    assert shadow["optimal"] <= shadow["rules"] + 0.01
    for row in shadow["hours"]:
        day = engine.plan[(row["hour"].date() - DAY).days]
        if row["action"] == "grid_charging" and row["grid_kwh"] >= 0.1:
            assert any(engine.charge_hours[row["hour"]].get(rule) for rule in RULE_SWITCHES)
            assert engine.charge_hours[row["hour"]]["kWh"] == pytest.approx(row["grid_kwh"], abs=0.01)
        elif row["action"] == "force_discharge" and row["sold_kwh"] > 0.01:
            sold = sum(sale["kwh"] for ts, sale in day["force_discharge_timestamps"].items() if ts.replace(minute=0) == row["hour"])
            assert sold == pytest.approx(row["sold_kwh"], abs=0.01)
        elif row["action"] == "discharge_allowed":
            assert row["hour"] in day["discharge_timestamps"]
        assert sum(day["battery_level_flow"][row["hour"].hour]) == pytest.approx(row["battery_level"], abs=1.0)
    assert result.states["powerwall_action"] in ("grid_charging", "discharge_allowed", "force_solar_only_charging", "force_discharge", "blocked_solar_charge")


def test_optimizer_respects_min_total_profit() -> None:
    hours = [OptimizerHour(0, 1.0, 0.0, 0.0, 0.0), OptimizerHour(1, 1.4, 0.0, 5.0, 0.0)]
    assert optimize(hours, 5.0, CFG, 0.0).actions[0] == "grid_charging"
    strict = OptimizerConfig(**{**CFG.__dict__, "min_total_profit": 5.0})
    assert optimize(hours, 5.0, strict, 0.0).actions[0] != "grid_charging"



def test_optimizer_respects_switched_off_rules() -> None:
    """With the optimiser in control the rule switches decide which actions it may use."""
    engine, _ = planned(0, 30, {"grid_charge_to_sell": False, "optimize_solar_charging": False}, solar=True, negative=True)
    assert not [ts for ts in charge_hours(engine) if engine.charge_hours[ts].get("grid_charge_to_sell")]
    assert not [sale for sale in sales(engine).values() if sale["title"] in ("Sælger strøm købt billigt", "Sælger før billig opladning")]
    assert not any(engine.plan[day]["blocked_solar_charge_timestamps"] for day in engine.plan if isinstance(day, int))
    # the negative prices are still used for the own consumption
    assert [ts for ts in charge_hours(engine) if ts.date() == DAY and ts.hour in NEGATIVE_HOURS]


def test_optimizer_actions_are_explained_as_rules() -> None:
    engine, _ = planned(0, 30, {}, solar=True, negative=True)
    for ts in charge_hours(engine):
        reason = engine.plan[(ts.date() - DAY).days]["charging_sessions"][ts]["reason"]
        assert any(title in reason for title in ("Billigste timer", "Dyreste timer", "Brug for", "Negativ pris", "Køb for at sælge"))
    titles = {sale["title"] for sale in sales(engine).values()}
    assert titles <= {"Sælger strøm købt billigt", "Sælger før billig opladning", "Sælger før solen fylder batteriet", "Sælger i stedet for at bruge"}


def test_hour_rules_group_consecutive_hours_with_the_same_rule() -> None:
    from custom_components.battmind.engine.planner._helpers import _group_hour_rules, _joined_reason

    def hour(h: int) -> datetime.datetime:
        return datetime.datetime.combine(DAY, datetime.time(h))

    def reason(title: str, values: str) -> str:
        return f"<details><summary>⏳{title} ({values})</summary>body {values}<br></details>"

    rows = [
        (hour(1), reason("Dyreste timer", "1.00/10%")),
        (hour(2), reason("Dyreste timer", "2.00/20%")),
        (hour(4), reason("Dyreste timer", "3.00/30%")),  # not consecutive
        (hour(5), reason("Billigste timer", "0.50/5%")),
    ]
    groups = _group_hour_rules(rows)
    assert [len(group) for group in groups] == [2, 1, 1]
    joined = _joined_reason(groups[0])
    assert joined.startswith("<details><summary>📦⏳Dyreste timer (1.50/15%)</summary>")
    assert "<summary>01:00 (1.00/10%)</summary>body 1.00/10%" in joined
    # clock times are kept, not averaged
    same = _joined_reason([(hour(1), reason("Prioriteret", "17:00")), (hour(2), reason("Prioriteret", "17:00"))])
    assert "Prioriteret (17:00)</summary>" in same
    differ = _joined_reason([(hour(1), reason("Prioriteret", "17:00")), (hour(2), reason("Prioriteret", "19:00"))])
    assert "Prioriteret (17:00-19:00)</summary>" in differ


def test_single_sell_period_shows_its_end_time() -> None:
    from custom_components.battmind.engine.planner._helpers import _join_timestamps_by_hour

    timestamp = datetime.datetime.combine(DAY, datetime.time(20))
    reason = "<details><summary>Sælger</summary>body</details>"
    joined = _join_timestamps_by_hour(
        {timestamp: {"title": "Sælger", "emoji": "", "kwh": 0.9, "profit": 2.0, "reason": reason}},
        "kr",
        "I alt",
        lambda _timestamp: 4,
    )

    assert joined[timestamp]["period_end"] == timestamp + datetime.timedelta(minutes=15)


@pytest.mark.parametrize("optimizer_control", [True, False])
def test_overview_has_the_plan_and_its_totals(optimizer_control: bool) -> None:
    engine, _ = planned(0, 30, {"optimizer_control": optimizer_control}, solar=True, negative=True)
    overview = engine.last_overview
    assert "## 📅 Ladnings- og afladningsplan ##" in overview
    assert "Hour rules" not in overview and "📖" not in overview
    summary = engine.optimizer_shadow["summary_optimal" if optimizer_control else "summary_rules"]
    assert f"💰 I alt: {summary['profit']:+.2f} kr ({summary['profit_per_kwh']:.2f} kr/kWh)" in overview
    assert f"**{-summary['profit']:+.2f}** |" in overview  # the bill row's difference = the title
    without, with_battery = summary["without"], summary["with"]
    assert summary["profit"] == pytest.approx(without["bill"] - with_battery["bill"], abs=0.02)
    for column in (without, with_battery):
        # the items add up to the bill
        assert column["paid"] + column["earned"] + column["wear"] + column["energy_value"] == pytest.approx(column["bill"], abs=0.03)
        assert column["house_cost"] + column["battery_cost"] == pytest.approx(column["paid"], abs=0.02)
        assert column["solar_income"] + column["sold_income"] == pytest.approx(column["earned"], abs=0.02)
    assert ("Optimering af hele perioden" if optimizer_control else "Reglerne") in overview


def test_bill_with_the_battery_is_the_simulated_cost() -> None:
    """breakdown()'s bill with the battery is the same as simulate() of the plan, without = no battery at all."""
    from custom_components.battmind.engine.optimizer import breakdown

    hours = [OptimizerHour(0, 0.5, 0.2, 1.0, 0.0), OptimizerHour(1, 3.0, 2.0, 2.0, 0.0), OptimizerHour(2, 1.0, 0.5, 0.0, 3.0)]
    actions = ["grid_charging", "force_discharge", "discharge_allowed"]
    result = breakdown(hours, 50.0, CFG, actions, 1.0)
    assert result["with"]["bill"] == pytest.approx(simulate(hours, 50.0, CFG, actions, 1.0).total, abs=0.01)
    assert result["without"]["bill"] == pytest.approx(simulate(hours, 50.0, CFG, ["blocked_solar_charge"] * 3, 1.0).total, abs=0.01)
    assert result["without"]["house_kwh"] == pytest.approx(3.0)
    assert result["without"]["solar_kwh"] == pytest.approx(3.0)


def test_battery_price_is_a_running_average() -> None:
    """Charged kWh come in at their price, used/sold kWh leave at the average (the price of the rest stays)."""
    from custom_components.battmind.engine.planner._helpers import _running_battery_price

    # 5 kWh at 2.00, 5 kWh charged at 1.00 -> 1.50
    assert _running_battery_price(5.0, 2.0, [(5.0, 5.0, 1.0, 0.5)]) == pytest.approx(1.5)
    # using 4 kWh doesn't change the price of the rest
    assert _running_battery_price(5.0, 2.0, [(5.0, 5.0, 1.0, 0.5), (-4.0, 0.0, 0.0, 0.0)]) == pytest.approx(1.5)
    # emptied and refilled with solar (sold for 0.50 otherwise): the old price is gone
    assert _running_battery_price(5.0, 2.0, [(-5.0, 0.0, 0.0, 0.0), (4.0, 0.0, 0.0, 0.5)]) == pytest.approx(0.5)
    # a charge hour with solar on top: the grid part at the grid price, the rest at the solar price
    assert _running_battery_price(0.0, 2.0, [(4.0, 1.0, 1.0, 0.5)]) == pytest.approx((1.0 + 3 * 0.5) / 4)
    # nothing in the battery: the start price
    assert _running_battery_price(0.0, 2.0, []) == 2.0


def test_midnight_battery_level_optimises_every_day_on_its_own() -> None:
    """use_midnight_battery_level: every day of days_to_predict is optimised until midnight (max 24 hours)."""
    from custom_components.battmind.engine.optimizer import optimize_per_day, optimize_with_rules

    engine, _ = planned(18, 60, {"use_midnight_battery_level": True}, solar=True, days=3)
    shadow = engine.optimizer_shadow
    assert shadow["controls"] is True
    assert len({row["hour"].date() for row in shadow["hours"]}) == 3

    # the same as each day optimised alone, starting at the level the day before ends with
    def hour(day: int, h: int, buy: float, sell: float) -> OptimizerHour:
        return OptimizerHour(datetime.datetime.combine(DAY + datetime.timedelta(days=day), datetime.time(h)), buy, sell, 1.0, 0.0)

    hours = [hour(0, 22, 0.5, 0.2), hour(0, 23, 3.0, 2.0), hour(1, 0, 0.4, 0.1), hour(1, 1, 2.5, 1.5)]
    merged, explanations = optimize_per_day(hours, 30.0, CFG, 1.0, lambda _switch: True)
    first, _ = optimize_with_rules(hours[:2], 30.0, CFG, 1.0, lambda _switch: True)
    second, _ = optimize_with_rules(hours[2:], first.levels[-1], CFG, 1.0, lambda _switch: True)
    assert merged.actions == first.actions + second.actions
    assert merged.levels == first.levels + second.levels
    assert len(explanations) == 4
