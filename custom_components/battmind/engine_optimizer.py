"""Whole horizon optimisation - dynamic programming over the battery level.

Instead of rules run one after another, every hour of the plan (days_to_predict) gets one
of the actions charge_if_needed() can execute, chosen so the total cost of the horizon is
as low as possible. One action per hour means the battery never charges and discharges in
the same hour. It controls the battery (switch optimizer_control) or is shown next to the
rules; the rule based plan is simulated with the same model, so the totals can be compared.
"""
from __future__ import annotations

from dataclasses import dataclass, field

ACTIONS = ("discharge_allowed", "force_solar_only_charging", "grid_charging", "force_discharge", "blocked_solar_charge")
# charge_if_needed() replans every cron interval from the new battery level, so a charge hour
# stops when its planned kWh are in the battery, and force discharge runs per sell period
# (15 min): grid charging and selling can be a part of the hour
FRACTIONS = (0.25, 0.5, 0.75, 1.0)


@dataclass
class OptimizerHour:
    timestamp: object
    buy: float  # currency/kWh
    sell: float  # currency/kWh
    consumption: float  # kWh used by the house (0 when the solar surplus covers it)
    solar: float  # kWh solar surplus after the house consumption
    can_sell: bool = True  # False in the excluded sell hours
    fraction: float = 1.0  # part of the hour left (the current hour); scales the charge/discharge rate
    forbidden: set = field(default_factory=set)  # actions not allowed (their rule switch is off)


@dataclass
class OptimizerConfig:
    battery_size: float  # kWh
    level_min: float  # %
    level_max: float  # %
    max_charge: float  # kWh per hour (AC)
    max_discharge: float  # kWh per hour (AC)
    efficiency: float  # one way, round trip = efficiency²
    wear: float  # currency per kWh out of the battery
    min_profit_per_kwh: float = 0.0  # steers the choice only: a kWh out of the battery has to earn this much
    min_sell_kwh: float = 0.0
    min_total_profit: float = 0.0  # steers the choice only: a charge hour / sale hour has to earn this much
    step: float = 0.5  # % resolution of the battery level


@dataclass
class OptimizerResult:
    actions: list[str] = field(default_factory=list)
    grid_kwh: list[float] = field(default_factory=list)  # kWh bought for the battery per hour
    sold_kwh: list[float] = field(default_factory=list)  # kWh sold from the battery per hour
    levels: list[float] = field(default_factory=list)  # % at the end of each hour
    costs: list[float] = field(default_factory=list)  # currency per hour (negative = earned)
    total: float = 0.0  # currency incl. the value of the energy left at the end
    end_value: float = 0.0


def step_hour(
    hour: OptimizerHour, level: float, action: str, cfg: OptimizerConfig, limit: float | None = None, details: dict | None = None
) -> tuple[float, float, float, float]:
    """(new level %, cost, kWh out of the battery, kWh bought for the battery) of one hour with one action.

    limit: max kWh from the grid when grid charging / max kWh sold when force discharging
    (None = the full rate). details (optional) gets the kWh flows of the hour.
    """
    to_kwh = cfg.battery_size / 100.0
    room = max(cfg.level_max - level, 0.0) * to_kwh  # kWh battery side
    available = max(level - cfg.level_min, 0.0) * to_kwh
    eff = cfg.efficiency
    max_charge = cfg.max_charge * hour.fraction
    max_discharge = cfg.max_discharge * hour.fraction
    bought = hour.consumption
    exported = hour.solar
    stored = 0.0  # kWh into the battery (battery side)
    delivered = 0.0  # kWh out of the battery (AC side)

    solar_in = house = 0.0
    if action in ("discharge_allowed", "force_solar_only_charging", "grid_charging") and hour.solar > 0.0:
        solar_in = min(hour.solar, max_charge, room / eff)
        stored += solar_in * eff
        exported -= solar_in
    if action == "grid_charging":
        grid_in = min(max_charge - (stored / eff), (room - stored) / eff)
        grid_in = max(min(grid_in, max_charge if limit is None else limit), 0.0)
        stored += grid_in * eff
        bought += grid_in
    elif action == "discharge_allowed" and hour.solar <= 0.0:
        delivered = house = min(hour.consumption, max_discharge, available * eff)
        bought -= delivered
    elif action == "force_discharge":
        # the house first, then the planned kWh to the grid
        wanted = max_discharge if limit is None else hour.consumption + limit
        delivered = min(max_discharge, wanted, available * eff)
        house = min(delivered, hour.consumption)
        bought -= house
        exported += delivered - house

    cost = bought * hour.buy - exported * hour.sell + delivered * cfg.wear
    new_level = level + (stored - delivered / eff) / to_kwh
    grid_kwh = bought - hour.consumption if action == "grid_charging" else 0.0
    if details is not None:
        details.update(solar_in=solar_in, house=house, sold=delivered - house, grid=grid_kwh, delivered=delivered)
    return min(max(new_level, cfg.level_min), cfg.level_max), cost, delivered, grid_kwh


def _allowed(hour: OptimizerHour, level: float, action: str, cfg: OptimizerConfig, delivered: float) -> bool:
    if action in hour.forbidden:
        return False
    if action == "force_discharge":
        return hour.can_sell and delivered - min(delivered, hour.consumption) >= max(cfg.min_sell_kwh, 0.01)
    if action == "grid_charging":
        return level < cfg.level_max - 0.01
    if action == "blocked_solar_charge":
        return hour.solar > 0.0
    return True


def optimize(hours: list[OptimizerHour], start_level: float, cfg: OptimizerConfig, end_value_per_kwh: float) -> OptimizerResult:
    """Cheapest action per hour for the whole horizon (backward dynamic programming).

    end_value_per_kwh values the energy left in the battery (battery side) at the end of the
    horizon, else the cheapest plan would always empty the battery in the last hours.
    """
    steps = int(round((cfg.level_max - cfg.level_min) / cfg.step))
    levels = [cfg.level_min + i * cfg.step for i in range(steps + 1)]
    to_kwh = cfg.battery_size / 100.0

    def index(level: float) -> int:
        return min(max(int(round((level - cfg.level_min) / cfg.step)), 0), steps)

    # value[i] = lowest cost from this hour to the end starting at levels[i]
    value = [-(level - cfg.level_min) * to_kwh * end_value_per_kwh for level in levels]
    options = [(action, None) for action in ACTIONS if action not in ("grid_charging", "force_discharge")]
    options += [("grid_charging", cfg.max_charge * fraction) for fraction in FRACTIONS]
    options += [("force_discharge", cfg.max_discharge * fraction) for fraction in FRACTIONS]  # capped per hour by its rate
    choice: list[list[tuple[str, float | None]]] = []
    for hour in reversed(hours):
        new_value, new_choice = [], []
        for level in levels:
            best_cost, best_option = None, options[0]
            for action, limit in options:
                new_level, cost, delivered, _ = step_hour(hour, level, action, cfg, limit)
                if not _allowed(hour, level, action, cfg, delivered):
                    continue
                # min_profit_per_kwh / min_total_profit only steer the choice, the reported costs are real money
                total = cost + delivered * cfg.min_profit_per_kwh + value[index(new_level)]
                if action in ("grid_charging", "force_discharge"):
                    total += cfg.min_total_profit
                if best_cost is None or total < best_cost - 1e-9:
                    best_cost, best_option = total, (action, limit)
            new_value.append(best_cost)
            new_choice.append(best_option)
        value, choice = new_value, [new_choice, *choice]

    result = OptimizerResult()
    level = start_level
    for hour, hour_choice in zip(hours, choice):
        action, limit = hour_choice[index(level)]
        level, cost, delivered, grid_kwh = step_hour(hour, level, action, cfg, limit)
        result.actions.append(action)
        result.grid_kwh.append(round(grid_kwh, 2))
        result.sold_kwh.append(round(delivered - min(delivered, hour.consumption), 2) if action == "force_discharge" else 0.0)
        result.levels.append(round(level, 1))
        result.costs.append(round(cost, 3))
    result.end_value = (level - cfg.level_min) * to_kwh * end_value_per_kwh
    result.total = sum(result.costs) - result.end_value
    return result


def simulate(
    hours: list[OptimizerHour],
    start_level: float,
    cfg: OptimizerConfig,
    actions: list[str],
    end_value_per_kwh: float,
    limits: list[float | None] | None = None,
) -> OptimizerResult:
    """Cost of a given action per hour (e.g. the rule based plan) with the same model.

    limits: planned kWh per grid charging / force discharge hour (None = the full rate).
    """
    result = OptimizerResult(actions=list(actions))
    level = start_level
    for index_, (hour, action) in enumerate(zip(hours, actions)):
        limit = limits[index_] if limits else None
        level, cost, _, grid_kwh = step_hour(hour, level, action, cfg, limit)
        result.grid_kwh.append(round(grid_kwh, 2))
        result.levels.append(round(level, 1))
        result.costs.append(round(cost, 3))
    result.end_value = (level - cfg.level_min) * cfg.battery_size / 100.0 * end_value_per_kwh
    result.total = sum(result.costs) - result.end_value
    return result


# The rule an optimised action corresponds to -> the switches that allow it. An action whose
# rule is switched off is forbidden in that hour and the period is optimised again.
_USE = ("cheapest_hour_fill_planner", "most_expensive_planner", "needed_before_max_level_planner")
_SELL = ("most_profit", "sell_excess_kwh_available", "force_powerwall_max_hours_old_discharge")
RULE_SWITCHES = {
    "cheapest_hour_fill_planner": _USE,
    "most_expensive_planner": _USE,
    "needed_before_max_level_planner": _USE,
    "negative_price_planner": ("negative_price_planner", *_USE),
    "grid_charge_to_sell": ("grid_charge_to_sell",),
    "most_profit": _SELL,
    "discharge_before_solar": ("discharge_before_solar", *_SELL),
    "prioritize_discharge_hours_by_energy_cost": ("prioritize_discharge_hours_by_energy_cost", "only_discharge_on_profit"),
    "optimize_solar_charging": ("optimize_solar_charging",),
}


def explain(hours: list[OptimizerHour], result: OptimizerResult, cfg: OptimizerConfig) -> list[dict | None]:
    """Which rule each optimised action corresponds to, and the hour it is for (index into hours).

    {"rule": key of RULE_SWITCHES, "target": index or None, "forbid": (index, action) to forbid when
    the rule is switched off, "kind": detail}, None for plain self consumption.
    """
    count = len(hours)
    full = cfg.level_max - 0.5
    empty = cfg.level_min + 0.5
    actions, levels = result.actions, result.levels

    def charging(index: int) -> bool:
        return actions[index] == "grid_charging" and result.grid_kwh[index] > 0.01

    def selling(index: int) -> bool:
        return actions[index] == "force_discharge" and result.sold_kwh[index] > 0.01

    explanations: list[dict | None] = []
    for i, hour in enumerate(hours):
        explanation = None
        if charging(i):
            # until the next charge block the charged kWh are sold, used before the sun fills the battery, or used
            block_end = next((j for j in range(i + 1, count) if not charging(j)), count)
            end = next((j for j in range(block_end, count) if charging(j)), count)
            window = range(block_end, end)
            sales = [j for j in window if selling(j)]
            uses = [j for j in window if actions[j] in ("discharge_allowed", "force_discharge") and hours[j].consumption > 0.0]
            solar_full = next((j for j in window if hours[j].solar > 0.0 and levels[j] >= full), None)
            if sales:
                target = max(sales, key=lambda j: (hours[j].sell, -j))
                # buying to sell switched off: the sale goes, the charge may still be used for the house
                explanation = {"rule": "grid_charge_to_sell", "target": target, "forbid": (target, "force_discharge")}
            elif hour.buy <= 0.0:
                explanation = {"rule": "negative_price_planner", "target": None}
            elif solar_full is not None:
                before = [j for j in uses if j < solar_full]
                explanation = {"rule": "needed_before_max_level_planner", "target": max(before, key=lambda j: (hours[j].buy, -j)) if before else None}
            else:
                same_day = sorted(h.buy for h in hours if h.timestamp.date() == hour.timestamp.date())
                cheapest = hour.buy <= same_day[min(2, len(same_day) - 1)] + 0.005
                explanation = {
                    "rule": "cheapest_hour_fill_planner" if cheapest else "most_expensive_planner",
                    "target": max(uses, key=lambda j: (hours[j].buy, -j)) if uses else None,
                }
        elif selling(i):
            source = None
            for j in range(i - 1, -1, -1):
                if charging(j):
                    source = j
                    break
                if levels[j] <= empty or (levels[j] >= full and hours[j].solar > 0.0):
                    break
            refill = recharge = None
            for j in range(i + 1, min(count, i + 24)):
                if charging(j):
                    recharge = j
                    break
                if levels[j] <= empty and hours[j].solar <= 0.0:
                    break
                if hours[j].solar > 0.0 and levels[j] >= full:
                    refill = j
                    break
            if source is not None:
                explanation = {"rule": "grid_charge_to_sell", "target": source, "kind": "bought"}
            elif recharge is not None:
                explanation = {"rule": "grid_charge_to_sell", "target": recharge, "kind": "recharged"}
            elif refill is not None:
                explanation = {"rule": "discharge_before_solar", "target": refill}
            else:
                explanation = {"rule": "most_profit", "target": None}
        elif actions[i] == "force_solar_only_charging" and hour.consumption > 0.0 and (levels[i - 1] if i else levels[i]) > empty:
            target = next(
                (j for j in range(i + 1, count) if actions[j] in ("discharge_allowed", "force_discharge") and hours[j].buy > hour.buy and hours[j].consumption > 0.0),
                None,
            )
            explanation = {"rule": "prioritize_discharge_hours_by_energy_cost", "target": target}
        elif actions[i] == "blocked_solar_charge":
            target = next((j for j in range(i + 1, count) if hours[j].solar > 0.0 and actions[j] != "blocked_solar_charge"), None)
            explanation = {"rule": "optimize_solar_charging", "target": target}
        explanations.append(explanation)
    return explanations


def optimize_with_rules(
    hours: list[OptimizerHour], start_level: float, cfg: OptimizerConfig, end_value_per_kwh: float, enabled, rounds: int = 6
) -> tuple[OptimizerResult, list[dict | None]]:
    """optimize(), but actions whose rule is switched off (enabled(switch) is False) are forbidden in that hour."""
    for hour in hours:
        hour.forbidden = set()
    result, explanations = None, []
    for _ in range(rounds):
        result = optimize(hours, start_level, cfg, end_value_per_kwh)
        explanations = explain(hours, result, cfg)
        changed = False
        for index, (action, explanation) in enumerate(zip(result.actions, explanations)):
            if explanation and not any(enabled(switch) for switch in RULE_SWITCHES[explanation["rule"]]):
                forbid_index, forbid_action = explanation.get("forbid", (index, action))
                if forbid_action not in hours[forbid_index].forbidden:
                    hours[forbid_index].forbidden.add(forbid_action)
                    changed = True
        if not changed:
            break
    return result, explanations


def breakdown(
    hours: list[OptimizerHour],
    start_level: float,
    cfg: OptimizerConfig,
    actions: list[str],
    end_value_per_kwh: float,
    limits: list[float | None] | None = None,
) -> dict:
    """The electricity bill of the period without and with the battery, item by item.

    Costs are positive, income negative; the items of each column add up to its bill. Without a
    battery the house buys all its consumption, all solar surplus is sold and the energy in the
    battery now stays (its value is deducted); with the battery the plan's flows are used and the
    energy left at the end is deducted. The profit is the difference of the bills.
    """
    keys = ("house_kwh", "house_cost", "battery_kwh", "battery_cost", "solar_kwh", "solar_income", "sold_kwh", "sold_income", "wear")
    without, with_battery = dict.fromkeys(keys, 0.0), dict.fromkeys(keys, 0.0)
    delivered_house = charged_solar = 0.0
    level = start_level
    for index, (hour, action) in enumerate(zip(hours, actions)):
        details: dict = {}
        level, _, _, _ = step_hour(hour, level, action, cfg, limits[index] if limits else None, details)
        without["house_kwh"] += hour.consumption
        without["house_cost"] += hour.consumption * hour.buy
        without["solar_kwh"] += hour.solar
        without["solar_income"] -= hour.solar * hour.sell
        with_battery["house_kwh"] += hour.consumption - details["house"]
        with_battery["house_cost"] += (hour.consumption - details["house"]) * hour.buy
        with_battery["battery_kwh"] += details["grid"]
        with_battery["battery_cost"] += details["grid"] * hour.buy
        with_battery["solar_kwh"] += hour.solar - details["solar_in"]
        with_battery["solar_income"] -= (hour.solar - details["solar_in"]) * hour.sell
        with_battery["sold_kwh"] += details["sold"]
        with_battery["sold_income"] -= details["sold"] * hour.sell
        with_battery["wear"] += details["delivered"] * cfg.wear
        delivered_house += details["house"]
        charged_solar += details["solar_in"]
    to_kwh = cfg.battery_size / 100.0
    without["energy_value"] = -(start_level - cfg.level_min) * to_kwh * end_value_per_kwh
    with_battery["energy_value"] = -(level - cfg.level_min) * to_kwh * end_value_per_kwh
    for column in (without, with_battery):
        column["paid"] = column["house_cost"] + column["battery_cost"]
        column["earned"] = column["solar_income"] + column["sold_income"]
        column["bill"] = column["paid"] + column["earned"] + column["wear"] + column["energy_value"]
    profit = without["bill"] - with_battery["bill"]
    cycled = delivered_house + with_battery["sold_kwh"]

    def rounded(column: dict) -> dict:
        return {key: round(value, 2) for key, value in column.items()}

    return {
        "without": rounded(without),
        "with": rounded(with_battery),
        "profit": round(profit, 2),
        "profit_per_kwh": round(profit / cycled, 2) if cycled > 0.0 else 0.0,
        "delivered_house_kwh": round(delivered_house, 2),
        "charged_grid_kwh": round(with_battery["battery_kwh"], 2),
        "charged_solar_kwh": round(charged_solar, 2),
        "end_level": round(level, 2),
        "start": hours[0].timestamp if hours else None,
        "end": hours[-1].timestamp if hours else None,
    }


def optimize_per_day(
    hours: list[OptimizerHour], start_level: float, cfg: OptimizerConfig, end_value_per_kwh: float, enabled
) -> tuple[OptimizerResult, list[dict | None]]:
    """optimize_with_rules() for each day on its own (max 24 hours, ends at midnight - use_midnight_battery_level).

    A day starts at the battery level the day before ends with; the energy left at midnight is
    valued like at the end of the period.
    """
    merged = OptimizerResult()
    explanations: list[dict | None] = []
    level = start_level
    days: dict = {}
    for hour in hours:
        days.setdefault(hour.timestamp.date(), []).append(hour)
    for day_hours in days.values():
        offset = len(merged.actions)
        result, day_explanations = optimize_with_rules(day_hours, level, cfg, end_value_per_kwh, enabled)
        for name in ("actions", "levels", "costs", "grid_kwh", "sold_kwh"):
            getattr(merged, name).extend(getattr(result, name))
        for explanation in day_explanations:
            if explanation is not None:
                explanation = dict(explanation)
                if explanation.get("target") is not None:
                    explanation["target"] += offset
                if "forbid" in explanation:
                    explanation["forbid"] = (explanation["forbid"][0] + offset, explanation["forbid"][1])
            explanations.append(explanation)
        level = result.levels[-1] if result.levels else level
    merged.end_value = (level - cfg.level_min) * cfg.battery_size / 100.0 * end_value_per_kwh
    merged.total = sum(merged.costs) - merged.end_value
    return merged, explanations
