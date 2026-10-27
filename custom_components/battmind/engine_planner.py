"""Charging planner (engine mixin) - port of cheap_grid_charge_hours()."""
from __future__ import annotations

import datetime
import logging
import math
import re
from collections import defaultdict
from copy import deepcopy
from dataclasses import replace

from .engine_optimizer import OptimizerConfig, OptimizerHour, breakdown, optimize_per_day, optimize_with_rules, simulate
from .efficiency import efficiency_at_power
from .engine_energy import derive_power_values
from .power import unit_factor_to_w
from .formatting import emoji_parse, emoji_text_format, get_list_values
from .helpers import (
    average,
    calculate_ema,
    calculate_trend,
    check_next_24_hours_diff,
    date_to_string,
    day_of_week_text,
    days_between,
    end_of_day,
    hour_in,
    hours_between,
    in_between,
    reset_time_to_hour,
    reverse_list,
    round_up,
    start_of_day,
)

_LOGGER = logging.getLogger(__name__)


def _optimize_solar_blocking(hours, delta, available, prices, candidates, start_level, minimum, maximum):
    """Pick the hours where solar charging is blocked (the surplus is sold instead).

    The battery has to end at least as high as without blocking and may not run
    empty more than without blocking, so the surplus is sold in the expensive hours
    and the battery is filled from the cheapest solar hours.
    hours: ordered hours of the day, delta: {hour: % change without solar},
    available: {hour: % solar surplus}, prices: {hour: sell price per kWh}.
    Returns (blocked hours, {hour: [level before, change]}, {hour: % charged from solar},
    {hour: % charged from solar without blocking}).
    """
    eps = 0.000001

    def simulate(blocked):
        level, flow, charged, shortage = start_level, {}, {}, 0.0
        for h in hours:
            before = level
            level += delta.get(h, 0.0)
            solar = 0.0 if h in blocked else available.get(h, 0.0)
            charged[h] = max(min(solar, maximum - level), 0.0)
            level = min(level + charged[h], maximum)
            if level < minimum:
                shortage += minimum - level
                level = minimum
            flow[h] = [before, level - before]
        return flow, charged, level, shortage

    original_flow, original_charged, target_level, target_shortage = simulate(set())

    def evaluate(blocked):
        flow, charged, level, shortage = simulate(blocked)
        if level < target_level - eps or shortage > target_shortage + eps:
            return None
        return sum(charged[h] * prices.get(h, 0.0) for h in charged), flow, charged

    def better(a, b):
        # cheaper charging wins, with equal cost the fewest blocked hours
        return a[0] < b[0] - eps or (abs(a[0] - b[0]) <= eps and len(a[1]) < len(b[1]))

    cost, flow, charged = evaluate(set())
    best = (cost, frozenset(), flow, charged)

    # Fill from the cheapest hours first: block everything, unblock from the cheapest
    # until the battery reaches the same level again
    blocked = set(candidates)
    for h in sorted(candidates, key=lambda h: (prices.get(h, 0.0), h)):
        if evaluate(blocked) is not None:
            break
        blocked.discard(h)
    if (result := evaluate(blocked)) is not None and better((result[0], blocked), best):
        best = (result[0], frozenset(blocked), result[1], result[2])

    # Local search: block/unblock one hour, swap two or block two at once
    for _ in range(100):
        current = best[1]
        free = [h for h in candidates if h not in current]
        moves = [current ^ {h} for h in candidates]
        moves += [(current - {a}) | {b} for a in current for b in free]
        moves += [current | {a, b} for i, a in enumerate(free) for b in free[i + 1:]]
        improved = None
        for test in moves:
            result = evaluate(test)
            if result is None:
                continue
            candidate = (result[0], frozenset(test), result[1], result[2])
            if better(candidate, improved or best):
                improved = candidate
        if improved is None:
            break
        best = improved

    if not best[1]:
        return set(), original_flow, original_charged, original_charged
    return set(best[1]), best[2], best[3], original_charged


class PlannerMixin:
    """The charging / discharging planner."""

    def what_if(self, data: dict, overrides: dict) -> dict:
        """Compare battery assumptions against historical hourly statistics."""
        stats = data.get("stats") or {}
        units = self._snap.units if self._snap else {}
        home = self.config["home"]
        ids = home["entity_ids"]

        def series(entity_id: str, power: bool = True) -> dict[float, float]:
            factor = unit_factor_to_w(units.get(entity_id)) if power else 1.0
            return {
                float(ts): float(value) * factor
                for ts, value in stats.get(entity_id) or []
                if isinstance(value, (int, float))
            }

        consumption = series(ids["power_consumption_entity_id"])
        flow = series(ids["powerwall_watt_flow_entity_id"])
        solar = series(self.config["solar"]["entity_ids"]["production_entity_id"])
        ignored = [series(entity_id) for entity_id in ids["ignore_consumption_from_entity_ids"] or [] if entity_id]
        buy = series(data["buy_entity_id"], power=False)
        sell = series(data["sell_entity_id"], power=False)
        positive_is_charging = home["invert_powerwall_watt_flow_entity_id"]
        tz = self._snap.tz if self._snap else None
        hours = []
        for ts in sorted(set(consumption) & set(buy)):
            battery = flow.get(ts, 0.0)
            charging = max(battery if positive_is_charging else -battery, 0.0)
            discharging = max(-battery if positive_is_charging else battery, 0.0)
            values = derive_power_values(
                home,
                abs(consumption[ts]),
                sum(abs(item.get(ts, 0.0)) for item in ignored),
                charging,
                discharging,
                abs(solar.get(ts, 0.0)),
            )
            row = self._analysis_row(values)
            net_use = row["use"]
            produced = row["solar"]
            hours.append(OptimizerHour(
                datetime.datetime.fromtimestamp(ts, tz).replace(tzinfo=None),
                buy[ts],
                self.sell_value(sell.get(ts, buy[ts])),
                max(net_use - produced, 0.0),
                max(produced - net_use, 0.0),
            ))
        if not hours:
            raise ValueError("No matching hourly consumption and price statistics were found")

        solar_cfg = self.config["solar"]
        max_charge = abs(self.max_kwh_charging)
        max_discharge = abs(self.max_kwh_discharging)
        fallback = self._fallback_efficiency()
        base_cfg = OptimizerConfig(
            battery_size=solar_cfg["powerwall_battery_size"],
            level_min=solar_cfg["powerwall_battery_level_min"],
            level_max=solar_cfg["powerwall_battery_level_max"],
            max_charge=max_charge,
            max_discharge=max_discharge,
            efficiency=fallback,
            wear=abs(solar_cfg["powerwall_wear_cost_per_kwh"]),
            charge_efficiency_curve=self.charging_efficiency_curve(),
            discharge_efficiency_curve=self.discharging_efficiency_curve(),
            curve_fallback_efficiency=fallback,
            min_profit_per_kwh=self.get_min_profit_per_kwh(),
            min_sell_kwh=self.number("min_sell_kwh"),
            min_total_profit=self.get_min_total_profit(),
            usage_reserve=self.number("usage_reserve"),
        )
        cfg = base_cfg
        changes = {}
        mapping = {
            "battery_size": "battery_size",
            "minimum_level": "level_min",
            "maximum_level": "level_max",
            "wear_cost_per_kwh": "wear",
            "usage_reserve": "usage_reserve",
        }
        for input_key, config_key in mapping.items():
            if input_key in overrides:
                changes[config_key] = float(overrides[input_key])
        if "max_charging_power" in overrides:
            changes["max_charge"] = float(overrides["max_charging_power"]) / 1000.0
        if "max_discharging_power" in overrides:
            changes["max_discharge"] = float(overrides["max_discharging_power"]) / 1000.0
        if "round_trip_loss" in overrides:
            loss = float(overrides["round_trip_loss"]) / 100.0
            efficiency = math.sqrt(1.0 - loss)
            changes.update(
                efficiency=efficiency,
                curve_fallback_efficiency=efficiency,
                charge_efficiency_curve=(),
                discharge_efficiency_curve=(),
            )
        cfg = replace(cfg, **changes)

        def effective_efficiency(calc_cfg: OptimizerConfig) -> OptimizerConfig:
            curve_fallback = calc_cfg.curve_fallback_efficiency or calc_cfg.efficiency
            charge_efficiency = efficiency_at_power(
                calc_cfg.charge_efficiency_curve, calc_cfg.max_charge * 1000.0, curve_fallback
            )
            discharge_efficiency = efficiency_at_power(
                calc_cfg.discharge_efficiency_curve, calc_cfg.max_discharge * 1000.0, curve_fallback
            )
            return replace(calc_cfg, efficiency=math.sqrt(charge_efficiency * discharge_efficiency))

        base_cfg = effective_efficiency(base_cfg)
        cfg = effective_efficiency(cfg)

        requested_start = overrides.get("starting_level")
        start_level = float(requested_start if requested_start is not None else cfg.level_min)
        current_start_level = float(requested_start if requested_start is not None else base_cfg.level_min)
        if cfg.level_min >= cfg.level_max:
            raise ValueError("Minimum level must be lower than maximum level")
        if not cfg.level_min <= start_level <= cfg.level_max:
            raise ValueError("Starting level must be between minimum and maximum level")
        if not base_cfg.level_min <= current_start_level <= base_cfg.level_max:
            raise ValueError("Starting level must also fit the configured battery limits")

        def calculate(calc_cfg: OptimizerConfig, initial_level: float) -> dict:
            end_value = calc_cfg.efficiency * max(
                average([hour.buy for hour in hours]) - calc_cfg.wear - calc_cfg.min_profit_per_kwh, 0.0
            )
            result, _ = optimize_per_day(deepcopy(hours), initial_level, calc_cfg, end_value, self.enabled)
            limits = [
                grid if action == "grid_charging" else sold if action == "force_discharge" else None
                for action, grid, sold in zip(result.actions, result.grid_kwh, result.sold_kwh)
            ]
            summary = breakdown(hours, initial_level, calc_cfg, result.actions, end_value, limits)
            summary["start"] = summary["start"].isoformat()
            summary["end"] = summary["end"].isoformat()
            return summary

        current = calculate(base_cfg, current_start_level)
        summary = calculate(cfg, start_level)
        return {
            "period": {
                "start": hours[0].timestamp.isoformat(),
                "end": hours[-1].timestamp.isoformat(),
                "hours": len(hours),
                "days": len({hour.timestamp.date() for hour in hours}),
            },
            "assumptions": {
                "battery_size": cfg.battery_size,
                "minimum_level": cfg.level_min,
                "maximum_level": cfg.level_max,
                "starting_level": start_level,
                "max_charging_power": cfg.max_charge * 1000.0,
                "max_discharging_power": cfg.max_discharge * 1000.0,
                "round_trip_loss": round((1.0 - cfg.efficiency**2) * 100.0, 2),
                "wear_cost_per_kwh": cfg.wear,
                "usage_reserve": cfg.usage_reserve,
            },
            "current": current,
            "scenario": summary,
            "difference": {
                "profit": round(summary["profit"] - current["profit"], 2),
                "bill": round(summary["with"]["bill"] - current["with"]["bill"], 2),
            },
        }

    def cheap_grid_charge_hours(self, force_recalculate: bool = False):  # noqa: C901
        config = self.config
        t = self.t
        name = self.name

        if self.price_provider is None or not self.price_provider.buy_prices_available():
            if not self.grid_price_cache.get("prices"):
                entity = self.price_provider.buy_price_entity_id() if self.price_provider else "-"
                _LOGGER.error("%s not available", entity)
                self.notify(
                    t("ui.cheap_grid_charge_hours.entity_not_in_domain", entity=entity),
                    title=f"{self.title} warning",
                    notification_id=f"{name}_cheap_grid_charge_hours_real_prices_not_found",
                )
                return None

        self.get_forecast_type()
        self.current_battery_level_expenses()
        self.local_energy_prediction()

        inputs_hash = self.plan_inputs_hash()
        if inputs_hash == self.last_plan_hash and not force_recalculate:
            _LOGGER.debug("Plan inputs unchanged, keeping the current plan")
            return None

        forecast_type = self.forecast_type
        max_kwh_charging = self.max_kwh_charging
        max_kwh_discharging = self.max_kwh_discharging
        kwh_to_percentage = self.kwh_to_percentage
        percentage_to_kwh = self.percentage_to_kwh
        calc_battery_loss_cost = self.calc_battery_loss_cost
        get_battery_level = self.get_battery_level
        get_min_profit_per_kwh = self.get_min_profit_per_kwh
        # one way efficiency: x kWh charged adds x * efficiency, y kWh used/sold takes y / efficiency
        efficiency = self.battery_efficiency()
        valuta_kwh = t("ui.common.valuta_kwh")
        valuta = t("ui.common.valuta")

        def r(key: str, **fmt) -> str:
            """Planner explanation text (ui.reason.<key> of texts/<language>.json)."""
            return t(f"ui.reason.{key}", **fmt)

        amount_of_days = int(config["days_to_predict"])

        # read-only below
        grid_prices = self.get_grid_prices()
        grid_sell_prices = self.get_sell_prices()
        battery_expenses = self.battery_expenses
        sorted_by_cheapest_price = sorted(grid_prices.items(), key=lambda kv: (kv[1], kv[0]))
        battery_expenses_keys = sorted(key for key in battery_expenses if isinstance(key, datetime.datetime))

        now = self.now()
        current_hour = reset_time_to_hour(now)
        current_sell_price_timestamp = self.price_provider.current_price_timestamp(now, sell_prices=True) if self.price_provider else None
        if current_sell_price_timestamp is None:
            current_sell_price_timestamp = current_hour

        charge_hours: dict = {}
        total_cost = 0.0
        total_kwh = 0.0
        solar_over_production: dict = {}
        charging_plan: dict = {}

        def solar_surplus_in_hour(timestamp):
            """Predicted solar surplus (after the house consumption) in this hour, also when it is sold."""
            if not self.enabled("solar_charging"):
                return 0.0
            data = charging_plan.get(days_between(current_hour, timestamp))
            if data is None:
                return 0.0
            solar = data["solar_kwh_prediction"]
            return solar[timestamp.hour] if timestamp.hour < len(solar) else 0.0

        def solar_kwh_in_hour(timestamp):
            """Predicted solar surplus charging the battery in this hour (0 when blocked/disabled)."""
            data = charging_plan.get(days_between(current_hour, timestamp))
            if data is None or hour_in(timestamp, data["blocked_solar_charge_timestamps"]):
                return 0.0
            return min(solar_surplus_in_hour(timestamp), max_kwh_charging)

        def kwh_available_in_hour(hour):
            """Grid charging capacity left in the hour - solar surplus charges first."""
            hour_in_charge_hours = False
            kwh_available = max_kwh_charging
            try:
                kwh = max_kwh_charging
                kwh -= kwh * (hour.minute / 60)
                kwh_available = max(kwh - solar_kwh_in_hour(hour), 0.0)
                data = charging_plan.get(days_between(current_hour, hour))
                if data is not None and hour_in(hour, data["force_discharge_timestamps"]) and hour not in charge_hours:
                    # selling in this hour - the battery can't charge at the same time
                    kwh_available = 0.0
                if hour in charge_hours:
                    hour_in_charge_hours = True
                    kwh_available -= charge_hours[hour]["kWh"]
                elif kwh_available <= 0.0:
                    # the sun uses the whole charge rate, treat it like a full charge hour
                    hour_in_charge_hours = True
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("kwh_available_in_hour(%s): %s", hour, err)
            return hour_in_charge_hours, kwh_available

        def add_to_charge_hours(kwh_needed, total_cost, total_kwh, hour, price, kwh_available, battery_level=None, rules=None):
            rules = list(rules or [])
            min_charging_kwh_cron = 0.0
            min_charging_kwh_ev = 0.0
            try:
                min_charging_kwh_cron = config["cron_interval"] / 60 * max_kwh_charging / 2
            except Exception:  # noqa: BLE001
                pass
            try:
                min_charging_kwh_ev = config["solar"]["powerwall_battery_size"] / 100 / 2
            except Exception:  # noqa: BLE001
                pass

            battery_level_added = False
            cost = 0.0
            try:
                kwh = max_kwh_charging
                min_charging_kwh = max(min(min_charging_kwh_cron, min_charging_kwh_ev), 0.2)
                price = float(price)
                kwh_needed = float(kwh_needed)
                kwh -= kwh * (hour.minute / 60)

                if battery_level:
                    battery_level_diff = round(
                        config["solar"]["powerwall_battery_level_max"] - (battery_level + kwh_to_percentage(kwh) * efficiency), 2
                    )
                    kwh_diff = round(percentage_to_kwh(battery_level_diff) / efficiency, 2)
                    if kwh_diff < 0.0:
                        kwh = max(kwh + kwh_diff, 0.0)

                kwh = min(kwh, kwh_available)

                if kwh > min_charging_kwh:
                    if hour not in charge_hours:
                        charge_hours[hour] = {"Cost": 0.0, "kWh": 0.0, "battery_level": 0.0, "Price": round(price, 2)}

                    if (kwh_needed - kwh) < 0.0:
                        kwh = kwh_needed
                        kwh_needed = 0.0
                    else:
                        kwh_needed = round(kwh_needed - kwh, 2)

                    cost = kwh * price
                    total_cost = total_cost + cost
                    battery_level_added = kwh_to_percentage(kwh) * efficiency

                    charge_hours[hour]["Cost"] = round(charge_hours[hour]["Cost"] + cost, 2)
                    charge_hours[hour]["kWh"] = round(charge_hours[hour]["kWh"] + kwh, 2)
                    charge_hours[hour]["battery_level"] = round(charge_hours[hour]["battery_level"] + battery_level_added, 2)
                    total_kwh = round(total_kwh + kwh, 2)

                    if not rules:
                        rules.append("no_rule")
                    for rule in rules:
                        if rule not in charge_hours[hour]:
                            charge_hours[hour][rule] = True
                    _LOGGER.debug(
                        "Adding %s %s %s %s%% %skWh with keys %s",
                        hour, price, valuta_kwh, charge_hours[hour]["battery_level"], charge_hours[hour]["kWh"], rules,
                    )
            except Exception as err:  # noqa: BLE001
                message = (
                    f"Error adding to charge hours kwh_needed:{kwh_needed} total_cost:{total_cost} total_kwh:{total_kwh} "
                    f"hour:{hour} price:{price} kwh_available:{kwh_available} battery_level:{battery_level} rules:{rules} error:{err}"
                )
                _LOGGER.error(message)
                self.notify(message, title=f"{self.title} error", notification_id=f"{name}_add_to_charge_hours_error_{hour}")

            return kwh_needed, total_cost, total_kwh, battery_level_added, cost

        def battery_level_flow_prediction(day, from_hour, reset_hour=False):
            min_level = config["solar"]["powerwall_battery_level_min"]
            max_level = config["solar"]["powerwall_battery_level_max"]
            if reset_hour:
                from_hour = max(from_hour - 1, 0)

            for hour in range(from_hour, 24):
                if reset_hour:
                    charging_plan[day]["battery_level_flow"][hour] = []
                    charging_plan[day]["battery_level_flow_without_solar"][hour] = []

                if hour == from_hour:
                    battery_level = get_battery_level() if day == 0 else sum(charging_plan[day - 1]["battery_level_flow"][23])
                    battery_level_without_solar = (
                        get_battery_level() if day == 0 else sum(charging_plan[day - 1]["battery_level_flow_without_solar"][23])
                    )
                    if reset_hour:
                        charging_plan[day]["battery_level_start_of_day"] = []
                    charging_plan[day]["battery_level_start_of_day"].append(battery_level)
                else:
                    if hour == 0 and day > 0:
                        battery_level = sum(charging_plan[day - 1]["battery_level_flow"][23])
                        battery_level_without_solar = sum(charging_plan[day - 1]["battery_level_flow_without_solar"][23])
                    elif hour - 1 in charging_plan[day]["battery_level_flow"]:
                        battery_level = sum(charging_plan[day]["battery_level_flow"][hour - 1])
                        battery_level_without_solar = sum(charging_plan[day]["battery_level_flow_without_solar"][hour - 1])
                    else:
                        _LOGGER.debug("Missing battery level flow for day:%s hour:%s", day, hour - 1)
                        continue

                    if hour == 23:
                        if reset_hour:
                            charging_plan[day]["battery_level_end_of_day"] = []
                        charging_plan[day]["battery_level_end_of_day"].append(battery_level)

                if hour not in charging_plan[day]["battery_level_flow"]:
                    continue

                charging_plan[day]["battery_level_flow"][hour].append(min(max(battery_level, min_level), max_level))
                charging_plan[day]["battery_level_flow_without_solar"][hour].append(min(max(battery_level_without_solar, min_level), max_level))

                timestamp = start_of_day(now) + datetime.timedelta(days=day, hours=hour)
                grid_added = charging_plan[day]["charging_sessions"].get(timestamp, {}).get("battery_level", 0.0)
                solar_added = 0.0
                solar_surplus = 0.0

                if self.enabled("solar_charging"):
                    solar = charging_plan[day]["solar_kwh_prediction"]
                    solar_surplus = solar[hour] if hour < len(solar) else 0.0
                    if not hour_in(timestamp, charging_plan[day]["blocked_solar_charge_timestamps"]):
                        solar_added = kwh_to_percentage(min(solar_surplus, max_kwh_charging)) * efficiency

                if hour not in charging_plan[day]["hour_cost_prediction"][forecast_type]:
                    continue

                percentage_sold = 0.0
                percentage_used_without_solar = charging_plan[day]["hour_cost_prediction"][forecast_type][hour]["percentage"] / efficiency * -1
                if day == 0 and hour == now.hour:
                    # the flow starts at the battery level now: only the rest of the hour is still used
                    percentage_used_without_solar *= 1.0 - now.minute / 60.0
                # the solar prediction is the surplus after the consumption, so the sun covers the house in that hour
                percentage_used = 0.0 if solar_surplus > 0.0 else percentage_used_without_solar

                if not hour_in(timestamp, charging_plan[day]["discharge_timestamps"]):
                    percentage_used = 0.0
                    percentage_used_without_solar = 0.0

                # the battery can't charge and discharge in the same hour - same priority as charge_if_needed():
                # grid charging, then force discharge (no solar charging while selling)
                if grid_added <= 0.0:
                    for ts, sale in charging_plan[day]["force_discharge_timestamps"].items():
                        if ts.hour == timestamp.hour:
                            percentage_sold -= kwh_to_percentage(sale["kwh"]) / efficiency
                if percentage_sold < 0.0:
                    solar_added = 0.0
                    discharge_limit_percentage = kwh_to_percentage(abs(max_kwh_discharging)) / efficiency
                    # house + grid together within the discharge rate
                    percentage_sold = max(percentage_sold, min(-discharge_limit_percentage - percentage_used, 0.0))
                if grid_added > 0.0:
                    percentage_used = 0.0
                    percentage_used_without_solar = 0.0
                percentage_added = grid_added + solar_added

                power_balance = percentage_used + percentage_added + percentage_sold
                power_balance_without_solar = percentage_used_without_solar + grid_added + percentage_sold

                if (battery_level + power_balance) <= min_level:
                    charging_plan[day]["battery_level_flow"][hour] = [min_level]
                    power_balance = 0.0
                elif (battery_level + power_balance) >= max_level:
                    charging_plan[day]["battery_level_flow"][hour] = [max_level]
                    power_balance = 0.0

                if (battery_level + power_balance_without_solar) <= min_level:
                    charging_plan[day]["battery_level_flow_without_solar"][hour] = [min_level]
                    power_balance_without_solar = 0.0
                elif (battery_level + power_balance_without_solar) >= max_level:
                    charging_plan[day]["battery_level_flow_without_solar"][hour] = [max_level]
                    power_balance_without_solar = 0.0

                charging_plan[day]["battery_level_flow"][hour].append(power_balance)
                charging_plan[day]["battery_level_flow_without_solar"][hour].append(power_balance_without_solar)

                if sum(charging_plan[day]["battery_level_flow"][hour]) > max_level:
                    charging_plan[day]["battery_level_flow"][hour] = [max_level]
                if sum(charging_plan[day]["battery_level_flow_without_solar"][hour]) > max_level:
                    charging_plan[day]["battery_level_flow_without_solar"][hour] = [max_level]
                if sum(charging_plan[day]["battery_level_flow"][hour]) < min_level:
                    charging_plan[day]["battery_level_flow"][hour] = [min_level]
                if sum(charging_plan[day]["battery_level_flow_without_solar"][hour]) < min_level:
                    charging_plan[day]["battery_level_flow_without_solar"][hour] = [min_level]

        def battery_level_flow_prediction_recalc(final_recalc=False):
            for day in range(amount_of_days):
                battery_level_flow_prediction(day, now.hour if day == 0 else 0, reset_hour=True)

            if not final_recalc:
                return

            hours = 24 - now.hour
            current_battery_level = get_battery_level()
            lowest_timestamp = charging_plan[0]["end_of_day"]
            lowest_battery_level = sum(charging_plan[0]["battery_level_end_of_day"])

            if not self.enabled("use_midnight_battery_level") and 1 in charging_plan:
                tolerance = 3.0
                nextday_lowest_timestamp = lowest_timestamp
                nextday_lowest_battery_level = lowest_battery_level

                for hour in range(24):
                    timestamp = charging_plan[1]["start_of_day"] + datetime.timedelta(hours=hour)
                    what_day = days_between(current_hour, timestamp)
                    loop_hour = timestamp.hour
                    if what_day not in charging_plan or loop_hour not in charging_plan[what_day]["battery_level_flow"]:
                        continue
                    battery_level = sum(charging_plan[what_day]["battery_level_flow"].get(loop_hour, [0.0]))

                    if battery_level > nextday_lowest_battery_level or battery_level <= config["solar"]["powerwall_battery_level_min"]:
                        no_remaining_battery_level_found = False
                        for remaining_hour in range(hour, hour + 3):
                            remaining_timestamp = charging_plan[1]["start_of_day"] + datetime.timedelta(hours=remaining_hour)
                            remaining_what_day = days_between(current_hour, remaining_timestamp)
                            remaining_loop_hour = remaining_timestamp.hour
                            if (
                                remaining_what_day in charging_plan
                                and remaining_loop_hour in charging_plan[remaining_what_day]["battery_level_flow"]
                            ):
                                remaining_battery_level = sum(
                                    charging_plan[remaining_what_day]["battery_level_flow"].get(remaining_loop_hour, [0.0])
                                )
                                if remaining_battery_level - nextday_lowest_battery_level > tolerance:
                                    no_remaining_battery_level_found = True
                                    break
                                if remaining_battery_level < nextday_lowest_battery_level:
                                    nextday_lowest_timestamp = remaining_timestamp
                                    nextday_lowest_battery_level = remaining_battery_level
                        if no_remaining_battery_level_found:
                            break

                    if battery_level < nextday_lowest_battery_level:
                        nextday_lowest_timestamp = timestamp
                        nextday_lowest_battery_level = battery_level

                hours = hours_between(current_hour, nextday_lowest_timestamp) + 1

            battery_level_dict = {now: f"{int(round(current_battery_level, 0))} %"}
            for hour in range(hours):
                timestamp = current_hour.replace(minute=59, second=59, microsecond=0) + datetime.timedelta(hours=hour)
                what_day = days_between(current_hour, timestamp)
                loop_hour = timestamp.hour
                if what_day not in charging_plan or loop_hour not in charging_plan[what_day]["battery_level_flow"]:
                    continue
                battery_level = sum(charging_plan[what_day]["battery_level_flow"].get(loop_hour, [0.0]))
                battery_level_dict[timestamp] = f"{int(round(battery_level, 0))} %"
                if battery_level < lowest_battery_level:
                    lowest_battery_level = battery_level
                    lowest_timestamp = timestamp

            items = list(battery_level_dict.items())
            if not self.enabled("use_midnight_battery_level"):
                while len(items) > 1 and items[-1][1] == items[-2][1]:
                    items.pop()
            battery_level_dict = dict(items)

            needed = max(min(lowest_battery_level, config["solar"]["powerwall_battery_level_max"]), config["solar"]["powerwall_battery_level_min"])
            needed = (config["solar"]["powerwall_battery_level_max"] - needed) + config["solar"]["powerwall_battery_level_min"]
            self.set_state("battery_level_needed", int(round(needed, 0)))
            self.set_attr("battery_level_needed", "battery_level_flow_prediction", battery_level_dict)

        def future_charging(total_cost, total_kwh):

            def add_charging_session_to_day(timestamp, what_day, reason="unknown"):
                charging_plan[what_day]["charging_sessions"][timestamp] = charge_hours[timestamp]
                charging_plan[what_day]["charging_sessions"][timestamp]["reason"] = reason
                if hour_in(timestamp, charging_plan[what_day]["discharge_timestamps"]) and timestamp in charging_plan[what_day]["discharge_timestamps"]:
                    charging_plan[what_day]["discharge_timestamps"].remove(timestamp)

            def add_charging_to_days(timestamp):
                days = days_between(now, timestamp)
                for day in range(days, amount_of_days):
                    battery_level_flow_prediction(day, timestamp.hour if day == days else 0, reset_hour=True)

            def _get_predicted_battery_cost(day, hour=None):
                """(kWh price, loss, kWh price incl. loss and wear) of the energy in the battery at the end of the hour.

                A running average from now through the planned flow: grid charged kWh come in at the
                charge price, solar at what the surplus would have been sold for, and used/sold kWh
                leave at the average price (the price of what is left doesn't change).
                """
                target = charging_plan[day]["start_of_day"].replace(hour=23 if hour is None else hour)
                unit = battery_expenses.get("unit")
                if not isinstance(unit, (int, float)):
                    unit = self.get_powerwall_kwh_price()
                # amounts as kWh charged (AC), like the charging history's unit price
                steps = []
                timestamp = current_hour
                while timestamp <= target:
                    data = charging_plan.get(days_between(current_hour, timestamp))
                    if data is None:
                        break
                    flow = data["battery_level_flow"].get(timestamp.hour)
                    if flow:
                        sessions = [session for ts, session in data["charging_sessions"].items() if reset_time_to_hour(ts) == timestamp]
                        grid_kwh = sum(session["kWh"] for session in sessions)
                        steps.append((
                            percentage_to_kwh(sum(flow) - flow[0]) / efficiency,
                            grid_kwh,
                            sum(session["Cost"] for session in sessions) / grid_kwh if grid_kwh > 0.0 else 0.0,
                            sell_price_in_hour(timestamp),
                        ))
                    timestamp += datetime.timedelta(hours=1)
                start_kwh = percentage_to_kwh(max(get_battery_level() - config["solar"]["powerwall_battery_level_min"], 0.0)) / efficiency
                raw = _running_battery_price(start_kwh, unit, steps)
                loss_cost = calc_battery_loss_cost(raw)
                return raw, loss_cost, raw + loss_cost + abs(config["solar"]["powerwall_wear_cost_per_kwh"])

            def sell_price_in_hour(timestamp):
                """Sell price of the hour (average of its periods), else the predicted solar sell price."""
                hour_timestamp = reset_time_to_hour(timestamp)
                prices, _, _ = sell_prices_for_window(hour_timestamp, hour_timestamp + datetime.timedelta(hours=1))
                if prices:
                    return average(list(prices.values()))
                data = charging_plan.get(days_between(current_hour, hour_timestamp))
                predicted = data["solar_cost_prediction"] if data else []
                return float(predicted[hour_timestamp.hour]) if hour_timestamp.hour < len(predicted) else 0.0

            def grid_headroom_kwh(timestamp, price=None):
                """Grid kWh that can be charged at timestamp without pushing solar surplus out of the battery.

                Grid energy stays in the battery until it runs down to min level, so the highest
                predicted level (incl. solar) from the charge hour until then must stay below max.
                With price: solar first is soft - when the solar surplus that would be pushed out is
                sold for more than the grid kWh costs (e.g. negative buy prices), it may be pushed out.
                """
                minimum = config["solar"]["powerwall_battery_level_min"]
                maximum = config["solar"]["powerwall_battery_level_max"]
                highest = minimum
                loop_timestamp = reset_time_to_hour(timestamp)
                while (data := charging_plan.get(days_between(current_hour, loop_timestamp))) is not None:
                    flow = data["battery_level_flow"].get(loop_timestamp.hour)
                    if flow:
                        level = sum(flow)
                        if (
                            price is not None and loop_timestamp > timestamp and level >= maximum - 0.01
                            and solar_surplus_in_hour(loop_timestamp) > 0.0
                            and sell_price_in_hour(loop_timestamp) - price >= max(get_min_profit_per_kwh(), 0.01)
                        ):
                            # the battery is full of solar from here on anyway, the pushed out solar is sold well
                            break
                        highest = max(highest, level, flow[0])
                        if loop_timestamp > timestamp and level <= minimum + 0.01:
                            break
                    loop_timestamp += datetime.timedelta(hours=1)
                return percentage_to_kwh(max(maximum - highest, 0.0)) / efficiency

            def charge_kwh_profit(charge_price, use_price):
                """Profit per kWh charged at charge_price and used/sold at use_price, after loss and wear."""
                return use_price - (charge_price + calc_battery_loss_cost(charge_price) + abs(config["solar"]["powerwall_wear_cost_per_kwh"]))

            def is_profitable(kwh_profit, kwh, periods_in_hour=1):
                """min_profit_per_kwh and min_total_profit (per hour, split over the price periods)."""
                return kwh_profit >= get_min_profit_per_kwh() and kwh_profit * kwh >= self.get_min_total_profit() / periods_in_hour

            def allow_discharge(timestamp):
                """The battery is charged for this hour, so it has to be allowed to discharge in it."""
                data = charging_plan.get(days_between(current_hour, timestamp))
                if data is None or timestamp in data["charging_sessions"]:
                    return
                data["blocked_discharge_timestamps"].pop(timestamp, None)
                if timestamp not in data["discharge_timestamps"]:
                    data["discharge_timestamps"].append(timestamp)

            def uncovered_kwh(day, hour):
                """Grid kWh to charge (incl. loss) for the consumption the battery/solar doesn't cover in the current plan."""
                minimum = config["solar"]["powerwall_battery_level_min"]
                flow = charging_plan[day]["battery_level_flow"].get(hour)
                prediction = charging_plan[day]["hour_cost_prediction"][forecast_type].get(hour)
                if not flow or prediction is None:
                    return 0.0
                timestamp = charging_plan[day]["start_of_day"].replace(hour=hour)
                discharging = timestamp in charging_plan[day]["discharge_timestamps"]
                if solar_surplus_in_hour(timestamp) > 0.0 or (discharging and sum(flow) > minimum + 0.01):
                    return 0.0
                covered = percentage_to_kwh(max(flow[0] - minimum, 0.0)) * efficiency if discharging else 0.0
                return max(prediction["kwh"] - covered, 0.0) / efficiency**2

            def charging_window(day):
                from_timestamp = charging_plan[day]["start_of_day"] - datetime.timedelta(hours=6) if day > 0 else charging_plan[day]["start_of_day"]
                return from_timestamp, charging_plan[day]["end_of_day"]

            def other_day_text(day, what_day):
                if what_day != day:
                    return f"<br><center>**({charging_plan[day]['start_of_day'].date().strftime('%d/%m')})**</center>"
                return ""

            def cheapest_hour_fill_planner(day):
                nonlocal total_cost, total_kwh
                finished = False
                day_prices = {}
                timestamps = []
                remove_list = []
                ignore_price_diff = self.number("cheap_price_period_rise_threshold")

                from_timestamp, to_timestamp = charging_window(day)
                for timestamp, price in grid_prices.items():
                    if not in_between(timestamp, from_timestamp, to_timestamp) or timestamp < current_hour:
                        continue
                    day_prices[timestamp] = price

                for i in range(int(self.number("cheap_price_periods"))):
                    timestamp = self.find_nth_local_min(day_prices, i + 1, include_edge_last=False)
                    if timestamp is None:
                        break
                    timestamps.append((timestamp, day_prices[timestamp]))

                cheap_timestamps = []
                for timestamp, price in sorted(timestamps, key=lambda kv: (kv[1], kv[0])):
                    data = []
                    for hour in range(timestamp.hour, 24):
                        loop_timestamp = timestamp.replace(hour=hour)
                        loop_price = grid_prices.get(loop_timestamp)
                        if loop_price is None:
                            continue
                        if loop_price - price <= ignore_price_diff:
                            data.append(loop_timestamp)
                        else:
                            last_timestamp = data.pop(-1) if data else timestamp
                            timestamp = last_timestamp
                            hour_in_charge_hours, kwh_available = kwh_available_in_hour(last_timestamp)
                            if hour_in_charge_hours and kwh_available <= 0.0:
                                if last_timestamp not in charge_hours:
                                    remove_list.append(last_timestamp)
                                for item in reverse_list(data):
                                    last_timestamp = item
                                    hour_in_charge_hours, kwh_available = kwh_available_in_hour(last_timestamp)
                                    if hour_in_charge_hours and kwh_available <= 0.0:
                                        if item == data[-1]:
                                            break
                                        continue
                                    timestamp = last_timestamp
                                    break
                            break
                    if timestamp not in cheap_timestamps:
                        cheap_timestamps.append(timestamp)

                cheapest_price = min(day_prices.values()) if day_prices else None
                cheapest_price_rise_threshold = self.number("cheapest_price_rise_threshold")

                highest_battery_level = config["solar"]["powerwall_battery_level_min"]
                highest_battery_level_timestamp = charging_plan[day]["start_of_day"]
                for hour in range(0, 24):
                    if hour not in charging_plan[day]["battery_level_flow"]:
                        continue
                    battery_level = sum(charging_plan[day]["battery_level_flow"].get(hour, [0.0]))
                    if battery_level > highest_battery_level:
                        highest_battery_level = battery_level
                        highest_battery_level_timestamp = charging_plan[day]["start_of_day"].replace(hour=hour)

                if self.enabled("cheapest_hour_fill_up"):
                    percentage_needed_today = [config["solar"]["powerwall_battery_level_max"]]
                else:
                    percentage_needed_today = [
                        charging_plan[day]["hour_cost_prediction"][forecast_type][hour]["percentage"]
                        for hour in range(now.hour if day == 0 else 0, 24)
                    ]

                # grid kWh incl. the charging loss
                kwh_needed = self.kwh_needed_for_charging(
                    min(sum(percentage_needed_today), config["solar"]["powerwall_battery_level_max"]), highest_battery_level
                ) / efficiency
                if kwh_needed <= 0.0:
                    return

                for cheap_timestamp in cheap_timestamps:
                    for i in range(int(round_up(kwh_needed / max_kwh_charging) + 1)):
                        if finished:
                            break
                        timestamp = cheap_timestamp - datetime.timedelta(hours=i)
                        price = grid_prices.get(timestamp)
                        if price is None or timestamp < current_hour:
                            continue
                        what_day = days_between(current_hour, timestamp)
                        if what_day < day - 1 or what_day not in charging_plan:
                            continue
                        if cheapest_price is not None and price - cheapest_price > cheapest_price_rise_threshold:
                            continue

                        # the hours of the day (after the charge hour) where using the charged kWh pays off
                        kwh_with_profit = []
                        profit_with_kwh = []
                        for hour in charging_plan[day]["hour_cost_prediction"][forecast_type]:
                            loop_timestamp = charging_plan[day]["start_of_day"].replace(hour=hour)
                            loop_price = grid_prices.get(loop_timestamp)
                            if loop_price is None or loop_timestamp <= timestamp or solar_surplus_in_hour(loop_timestamp) > 0.0:
                                continue
                            profit = charge_kwh_profit(price, loop_price)
                            if profit < get_min_profit_per_kwh():
                                continue
                            kwh_with_profit.append(charging_plan[day]["hour_cost_prediction"][forecast_type][hour]["kwh"] / efficiency**2)
                            profit_with_kwh.append(profit * kwh_with_profit[-1])

                        hour_in_charge_hours, kwh_available = kwh_available_in_hour(timestamp)
                        if hour_in_charge_hours and kwh_available <= 0.0:
                            continue
                        full_charge_kwh_needed = percentage_to_kwh(
                            config["solar"]["powerwall_battery_level_max"] - config["solar"]["powerwall_battery_level_min"]
                        ) / efficiency
                        kwh_available = min(sum(kwh_with_profit), grid_headroom_kwh(timestamp, price), full_charge_kwh_needed, kwh_available)
                        if kwh_available <= 0.0:
                            continue
                        average_profit = sum(profit_with_kwh) / sum(kwh_with_profit)
                        if not is_profitable(average_profit, min(kwh_available, kwh_needed)):
                            _LOGGER.debug(
                                "Cheapest hour fill: skipping %s (%.2f %s x %.2f kWh below the minimum profit)",
                                timestamp, average_profit, valuta_kwh, min(kwh_available, kwh_needed),
                            )
                            continue

                        if round(kwh_needed, 1) > 0.0 and kwh_to_percentage(kwh_needed) > 0.0:
                            kwh_needed, total_cost, total_kwh, battery_level_added, cost_added = add_to_charge_hours(
                                kwh_needed, total_cost, total_kwh, timestamp, price, kwh_available,
                                rules=["cheapest_hour_fill_planner"],
                            )
                            if timestamp in charge_hours and battery_level_added:
                                charging_plan[day]["total_cost"] += cost_added
                                reason = (
                                    f"<details><summary>{emoji_parse({'charging': True})}{r('title.cheapest_hours')} ({price}/{battery_level_added:.0f}%)"
                                    f"{other_day_text(day, what_day)}</summary>"
                                    f"{r('highest_level_time')}: **{highest_battery_level_timestamp.strftime('%H:%M')}**<br>"
                                    f"{r('highest_level')}: **{highest_battery_level:.1f}%**<br>"
                                    f"{r('current_price')}: **{price}** {valuta_kwh}<br>"
                                    f"{r('avg_profit_use')}: **{average_profit:.2f} {valuta_kwh}**<br>"
                                    f"{r('level_added')}: **{battery_level_added:.2f}%**<br>"
                                    "</details>"
                                )
                                add_charging_session_to_day(timestamp, what_day, reason=reason)
                                add_charging_to_days(timestamp)
                        else:
                            finished = True

            def most_expensive_planner(day):
                nonlocal total_cost, total_kwh
                for sorted_hour in charging_plan[day]["sorted_hour_cost_prediction"][forecast_type]:
                    sorted_timestamp = current_hour.replace(hour=sorted_hour) + datetime.timedelta(days=day)
                    if sorted_timestamp <= current_hour:
                        continue
                    sorted_price = charging_plan[day]["hour_cost_prediction"][forecast_type][sorted_hour]["price"]
                    sorted_battery_level = (charging_plan[day]["battery_level_flow"].get(sorted_hour) or [0.0])[0]
                    # only the part of the hour the battery (incl. solar) doesn't cover already
                    sorted_kwh_needed = uncovered_kwh(day, sorted_hour)
                    if round(sorted_kwh_needed, 1) <= 0.0:
                        continue

                    remove_list = []
                    from_timestamp, to_timestamp = charging_window(day)

                    for timestamp, price in sorted_by_cheapest_price:
                        if not in_between(timestamp, from_timestamp, to_timestamp) or timestamp < current_hour:
                            continue
                        if timestamp >= sorted_timestamp:
                            continue
                        kwh_profit = charge_kwh_profit(price, sorted_price)
                        if kwh_profit < get_min_profit_per_kwh():
                            continue
                        hour_in_charge_hours, kwh_available = kwh_available_in_hour(timestamp)
                        if hour_in_charge_hours and kwh_available <= 0.0:
                            remove_list.append(timestamp)
                            continue

                        what_day = days_between(current_hour, timestamp)
                        if what_day < day - 1 or what_day not in charging_plan:
                            continue

                        headroom_kwh = grid_headroom_kwh(timestamp, price)
                        kwh_available = round(min(headroom_kwh, kwh_available), 1)
                        if kwh_available <= 0.0:
                            continue
                        kwh_needed = min(sorted_kwh_needed, kwh_available)
                        if not is_profitable(kwh_profit, kwh_needed):
                            continue

                        if round(kwh_needed, 1) > 0.0 and kwh_to_percentage(kwh_needed) > 0.0:
                            sorted_kwh_needed, total_cost, total_kwh, battery_level_added, cost_added = add_to_charge_hours(
                                sorted_kwh_needed, total_cost, total_kwh, timestamp, price, kwh_available,
                                rules=["most_expensive_planner"],
                            )
                            if timestamp in charge_hours and battery_level_added:
                                charging_plan[day]["total_cost"] += cost_added
                                allow_discharge(sorted_timestamp)
                                reason = (
                                    f"<details><summary>{emoji_parse({'charging': True})}{r('title.most_expensive_hours')} ({price}/{battery_level_added:.0f}%)"
                                    f"{other_day_text(day, what_day)}</summary>"
                                    f"{r('prioritized_hour')}: **{sorted_timestamp}**<br>"
                                    f"{r('prioritized_level')}: **{sorted_battery_level:.1f}%**<br>"
                                    f"{r('prioritized_price')}: **{sorted_price}** {valuta_kwh}<br>"
                                    f"{r('current_price')}: **{price} {valuta_kwh}**<br>"
                                    f"{r('profit_charge_for_hour')}: **{kwh_profit:.2f} {valuta_kwh}**<br>"
                                    f"{r('room_without_solar')}: **{headroom_kwh:.2f} kWh**<br>"
                                    f"{r('level_added')}: **{battery_level_added:.2f}%**<br>"
                                    "</details>"
                                )
                                add_charging_session_to_day(timestamp, what_day, reason=reason)
                                add_charging_to_days(timestamp)
                        else:
                            break

                    for remove_timestamp in remove_list:
                        item = (remove_timestamp, grid_prices.get(remove_timestamp))
                        if item in sorted_by_cheapest_price:
                            sorted_by_cheapest_price.remove(item)

            def needed_before_max_level_planner(day):
                """Charge the little the battery lacks before it reaches its highest level (e.g. solar fills it later)."""
                nonlocal total_cost, total_kwh
                highest_battery_level = get_battery_level() if day == 0 else sum(charging_plan[day]["battery_level_start_of_day"])
                highest_battery_level_timestamp = current_hour if day == 0 else charging_plan[day]["start_of_day"]
                from_hour = now.hour if day == 0 else 0

                for hour in range(from_hour, 24):
                    battery_level = sum(charging_plan[day]["battery_level_flow"].get(hour, [0.0]))
                    if battery_level > highest_battery_level:
                        highest_battery_level = battery_level
                        highest_battery_level_timestamp = highest_battery_level_timestamp.replace(hour=hour)

                from_timestamp, to_timestamp = charging_window(day)

                for hour in range(from_hour, highest_battery_level_timestamp.hour + 1):
                    needed_timestamp = charging_plan[day]["start_of_day"].replace(hour=hour)
                    if needed_timestamp <= current_hour:
                        continue
                    kwh_needed = uncovered_kwh(day, hour)
                    if round(kwh_needed, 1) <= 0.0:
                        continue
                    _LOGGER.debug("Needed before max level: day %s hour %02d needs %.2f kWh", day, hour, kwh_needed)
                    needed_price = grid_prices.get(needed_timestamp)
                    if needed_price is None:
                        continue

                    for timestamp, price in sorted_by_cheapest_price:
                        if round(kwh_needed, 1) <= 0.0 or kwh_to_percentage(kwh_needed) <= 0.0:
                            break
                        if not in_between(timestamp, from_timestamp, to_timestamp) or timestamp < current_hour:
                            continue
                        if timestamp >= needed_timestamp:
                            continue
                        kwh_profit = charge_kwh_profit(price, needed_price)
                        if kwh_profit < get_min_profit_per_kwh():
                            continue
                        hour_in_charge_hours, kwh_available = kwh_available_in_hour(timestamp)
                        if hour_in_charge_hours and kwh_available <= 0.0:
                            continue
                        what_day = days_between(current_hour, timestamp)
                        if what_day < day - 1 or what_day not in charging_plan:
                            continue
                        kwh_available = round(min(kwh_available, grid_headroom_kwh(timestamp, price)), 1)
                        if kwh_available <= 0.0 or not is_profitable(kwh_profit, min(kwh_needed, kwh_available)):
                            continue

                        kwh_needed, total_cost, total_kwh, battery_level_added, cost_added = add_to_charge_hours(
                            kwh_needed, total_cost, total_kwh, timestamp, price, kwh_available,
                            rules=["needed_before_max_level_planner"],
                        )
                        if timestamp in charge_hours and battery_level_added:
                            charging_plan[day]["total_cost"] += cost_added
                            allow_discharge(needed_timestamp)
                            reason = (
                                f"<details><summary>{emoji_parse({'needed_before_max_level_planner': True})}{r('title.needed')} "
                                f"({price}/{battery_level_added:.0f}%){other_day_text(day, what_day)}</summary>"
                                f"{r('needed_at')}: **{needed_timestamp.strftime('%H:%M')}** ({needed_price} {valuta_kwh})<br>"
                                f"{r('highest_level_time')}: **{highest_battery_level_timestamp.strftime('%H:%M')}**<br>"
                                f"{r('highest_level')}: **{highest_battery_level:.1f}%**<br>"
                                f"{r('current_price')}: **{price}** {valuta_kwh}<br>"
                                f"{r('profit_per_kwh')}: **{kwh_profit:.2f} {valuta_kwh}**<br>"
                                f"{r('level_added')}: **{battery_level_added:.2f}%**<br>"
                                "</details>"
                            )
                            add_charging_session_to_day(timestamp, what_day, reason=reason)
                            add_charging_to_days(timestamp)

            def discharge_amount(day):
                discharge_kwh = 0.0
                last_battery_level = 0.0
                for hour in charging_plan[day]["battery_level_flow"]:
                    if charging_plan[day]["battery_level_flow"][hour]:
                        level = sum(charging_plan[day]["battery_level_flow"][hour])
                        if level < last_battery_level:
                            discharge_kwh += percentage_to_kwh(last_battery_level - level)
                        last_battery_level = level
                charging_plan[day]["discharge_kwh"] = discharge_kwh

            def only_discharge_on_profit(day):
                for hour in range(current_hour.hour if day == 0 else 0, 24):
                    if hour not in charging_plan[day]["battery_level_flow"]:
                        continue
                    if hour not in charging_plan[day]["hour_cost_prediction"][forecast_type]:
                        continue
                    timestamp = charging_plan[day]["start_of_day"] + datetime.timedelta(hours=hour)
                    if timestamp not in charging_plan[day]["discharge_timestamps"]:
                        continue
                    price = grid_prices.get(timestamp)
                    if price is None:
                        continue
                    raw, loss_cost, battery_kwh_cost = _get_predicted_battery_cost(day, hour)
                    min_profit_per_kwh = get_min_profit_per_kwh()
                    profit = price - battery_kwh_cost
                    # per kWh only: min_total_profit is for trades (a charge hour / a sale) - not using the
                    # battery for the house earns nothing, so it must not block small hours
                    if profit < min_profit_per_kwh:
                        _LOGGER.debug(
                            "Only discharge on profit: blocking %s (price %.2f - battery cost %.2f = %.2f < %.2f)",
                            timestamp, price, battery_kwh_cost, profit, min_profit_per_kwh,
                        )
                        charging_plan[day]["discharge_timestamps"].remove(timestamp)
                        if sum(charging_plan[day]["battery_level_flow"][hour]) <= config["solar"]["powerwall_battery_level_min"]:
                            continue
                        charging_plan[day]["battery_level_flow"][hour].append(
                            charging_plan[day]["hour_cost_prediction"][forecast_type][hour]["percentage"]
                        )
                        charging_plan[day]["blocked_discharge_timestamps"][timestamp] = (
                            f"<details><summary>{emoji_parse({'only_discharge_on_profit': True})}{r('title.no_profit')} ({profit:.2f})</summary>"
                            f"{r('battery_level')}: **{sum(charging_plan[day]['battery_level_flow'][hour]):.1f}%**<br>"
                            f"{r('battery_cost_basis')}: **{raw:.2f} {valuta_kwh}**<br>"
                            f"{r('loss_cost')}: **{loss_cost:.2f} {valuta_kwh}**<br>"
                            f"{r('wear_cost')}: **{abs(config['solar']['powerwall_wear_cost_per_kwh']):.2f} {valuta_kwh}**<br>"
                            f"**{r('battery_cost_total')}**: **{battery_kwh_cost:.2f} {valuta_kwh}**<br>"
                            f"{r('current_price')}: **{price} {valuta_kwh}**<br>"
                            f"{r('min_profit_per_kwh')}: **{min_profit_per_kwh:.2f} {valuta_kwh}**<br>"
                            f"{r('profit_per_kwh')}: **{profit:.2f} {valuta_kwh}**<br>"
                            "</details>"
                        )

            def prioritize_discharge_hours_by_energy_cost(day):
                powerwall_kwh_min = percentage_to_kwh(config["solar"]["powerwall_battery_level_min"])
                highest_battery_level = get_battery_level() if day == 0 else config["solar"]["powerwall_battery_level_min"]
                highest_battery_level_timestamp = now if day == 0 else charging_plan[day]["start_of_day"]

                for hour in charging_plan[day]["hour_cost_prediction"][forecast_type].keys():
                    battery_level = sum(charging_plan[day]["battery_level_flow"].get(hour, [0.0]))
                    if battery_level > highest_battery_level:
                        highest_battery_level = battery_level
                        highest_battery_level_timestamp = current_hour.replace(hour=hour) + datetime.timedelta(days=day)

                # the battery only has to last until it is charged again (solar or grid)
                recharge_timestamp = charging_plan[day]["end_of_day"]
                for hour in range(highest_battery_level_timestamp.hour + 1, 24):
                    flow = charging_plan[day]["battery_level_flow"].get(hour)
                    if flow and sum(flow) > flow[0] + 0.01:
                        recharge_timestamp = charging_plan[day]["start_of_day"].replace(hour=hour)
                        break

                highest_battery_kwh = percentage_to_kwh(highest_battery_level)
                battery_kwh = highest_battery_kwh
                prioritized_hours = []
                prioritized_kwh = []

                for hour in charging_plan[day]["sorted_hour_cost_prediction"][forecast_type]:
                    timestamp = current_hour.replace(hour=hour) + datetime.timedelta(days=day)
                    if timestamp not in charging_plan[day]["discharge_timestamps"]:
                        continue
                    if timestamp < highest_battery_level_timestamp or timestamp >= recharge_timestamp:
                        continue
                    if solar_surplus_in_hour(timestamp) > 0.0:
                        continue
                    kwh_needed = charging_plan[day]["hour_cost_prediction"][forecast_type][hour]["kwh"] / efficiency
                    battery_kwh = max(battery_kwh - kwh_needed, powerwall_kwh_min)

                    if battery_kwh <= powerwall_kwh_min:
                        if prioritized_hours and hour > max(prioritized_hours):
                            continue
                        charging_plan[day]["discharge_timestamps"].remove(timestamp)
                        prioritized_hours_string = ", ".join(f"{h:02d}:00" for h in prioritized_hours)
                        prioritized_kwh_string = (
                            f"{', '.join(f'{kwh_to_percentage(k):.0f}%' for k in prioritized_kwh)} "
                            f"({kwh_to_percentage(sum(prioritized_kwh)):.1f}%)"
                        )
                        charging_plan[day]["blocked_discharge_timestamps"][timestamp] = (
                            f"<details><summary>{emoji_parse({'prioritize_discharge_hours_by_energy_cost': True})}{r('title.most_expensive_prioritized')} "
                            f"({prioritized_hours_string.replace(' ', '')})</summary>"
                            f"{r('reaches_minimum')}<br>"
                            f"{r('prioritized_hours')}: **{prioritized_hours_string}**<br>"
                            f"{r('prioritized_kwh')}: **{prioritized_kwh_string}**<br>"
                            f"{r('highest_level_time')}: **{highest_battery_level_timestamp}**<br>"
                            f"{r('highest_level')}: **{kwh_to_percentage(highest_battery_kwh):.1f}%**<br>"
                            f"{r('current_level')}: **{kwh_to_percentage(battery_kwh):.1f}%**<br>"
                            f"{r('lowest_allowed_level')}: **{kwh_to_percentage(powerwall_kwh_min):.1f}%**<br>"
                            f"{r('needed_for_discharge')}: **{kwh_to_percentage(kwh_needed):.1f}%**<br>"
                            "</details>"
                        )
                    else:
                        prioritized_hours.append(hour)
                        prioritized_kwh.append(kwh_needed)

            def lowest_battery_level_until_next_charge(day):
                """Lowest level from the end of the day until the battery is empty or full again.

                Scans every following day of the plan (days_to_predict): with a big battery or a low
                consumption the energy lasts longer than one day. Once the battery is full again
                (solar/grid), energy sold before that is refilled, so the scan stops there.
                """
                minimum = config["solar"]["powerwall_battery_level_min"]
                maximum = config["solar"]["powerwall_battery_level_max"]
                lowest_timestamp = charging_plan[day]["end_of_day"]
                lowest_battery_level = sum(charging_plan[day]["battery_level_end_of_day"])
                using_next_day = False
                if not self.enabled("use_midnight_battery_level"):
                    for next_day in range(day + 1, amount_of_days):
                        for hour in range(24):
                            battery_level = charging_plan[next_day]["battery_level_flow"].get(hour)
                            if not battery_level:
                                continue
                            battery_level = sum(battery_level)
                            if battery_level >= maximum - 0.01:
                                return lowest_timestamp, lowest_battery_level, using_next_day
                            if battery_level < lowest_battery_level:
                                using_next_day = True
                                lowest_battery_level = battery_level
                                lowest_timestamp = charging_plan[next_day]["start_of_day"].replace(hour=hour)
                            if lowest_battery_level <= minimum + 0.01:
                                return lowest_timestamp, lowest_battery_level, using_next_day
                return lowest_timestamp, lowest_battery_level, using_next_day

            def sell_prices_for_window(from_timestamp, to_timestamp):
                """What a sold kWh is worth per sell period (see sell_value - can be negative with a fixed price)."""
                sell_price = config["solar"]["production_price"]
                using_grid_sell_price = sell_price == -1.0
                prices = {
                    ts: price if using_grid_sell_price else round(self.sell_value(price), 2)
                    for ts, price in grid_sell_prices.items() if in_between(ts, from_timestamp, to_timestamp)
                }
                return prices, sell_price, using_grid_sell_price

            def most_profit(day):
                """Sell battery energy in the most expensive sell periods when that pays more than using it.

                The kWh sold are missing in hours the battery would otherwise cover, so the cheapest of
                those hours are bought from the grid instead. Only hours in the same discharge period
                count (the battery isn't full in between, else the saved energy just pushes solar out).
                A sale has to beat that buy price by min_profit_per_kwh, pay the battery kWh cost itself,
                sell at least min_sell_kwh and earn min_total_profit (per hour, split over the price periods).
                """
                minimum = max(config["solar"]["powerwall_battery_level_min"], self.number("usage_reserve"))
                maximum = config["solar"]["powerwall_battery_level_max"]
                min_profit_per_kwh = get_min_profit_per_kwh()
                # selling has to earn something more than using the energy, also with min_profit_per_kwh = 0
                min_gain_per_kwh = max(min_profit_per_kwh, 0.01)
                from_timestamp = max(charging_plan[day]["start_of_day"], current_sell_price_timestamp)
                lowest_timestamp, _, _ = lowest_battery_level_until_next_charge(day)
                grid_sell_prices_for_day, sell_price, using_grid_sell_price = sell_prices_for_window(
                    from_timestamp, lowest_timestamp + datetime.timedelta(hours=1)
                )
                exclude_hours = self.get_exclude_sell_hours()

                # kWh the battery can deliver at the end of each hour, the hours it is full and
                # the hours it covers (cheapest first)
                window, deliverable, full_before, usage = [], {}, {}, []
                full_count = 0
                loop_timestamp = reset_time_to_hour(from_timestamp)
                while loop_timestamp <= lowest_timestamp:
                    data = charging_plan.get(days_between(current_hour, loop_timestamp))
                    flow = data["battery_level_flow"].get(loop_timestamp.hour) if data else None
                    if flow:
                        window.append(loop_timestamp)
                        deliverable[loop_timestamp] = percentage_to_kwh(max(sum(flow) - minimum, 0.0)) * efficiency
                        full_before[loop_timestamp] = full_count
                        prediction = data["hour_cost_prediction"][forecast_type].get(loop_timestamp.hour)
                        grid_price = grid_prices.get(loop_timestamp)
                        if (
                            prediction is not None and grid_price is not None
                            and loop_timestamp in data["discharge_timestamps"] and solar_surplus_in_hour(loop_timestamp) <= 0.0
                        ):
                            kwh = min(prediction["kwh"], percentage_to_kwh(max(flow[0] - minimum, 0.0)) * efficiency)
                            if kwh > 0.01:
                                usage.append([grid_price, loop_timestamp, kwh])
                        # a full battery or a grid charge ends the discharge period: a charge after a sale
                        # replans from the lower level and buys the sold kWh back at its price
                        if sum(flow) >= maximum - 0.01 or hour_in(loop_timestamp, data["charging_sessions"]):
                            full_count += 1
                    loop_timestamp += datetime.timedelta(hours=1)
                usage.sort(key=lambda item: (item[0], item[1]))

                def compatible(usage_timestamp, sell_timestamp):
                    """No full battery / grid charge in between, else the kWh is just solar stored or bought back."""
                    if usage_timestamp == sell_timestamp:
                        return False  # selling and buying in the same hour earns nothing
                    first, last = min(usage_timestamp, sell_timestamp), max(usage_timestamp, sell_timestamp)
                    return full_before[last] - full_before[first] == 0

                changes = []  # (hour, kWh change of the battery content): sales negative, freed usage positive

                def shortfall(hour_timestamp, kwh, freed):
                    """kWh the battery lacks in any later hour when kwh is sold at hour_timestamp."""
                    planned = changes + [(hour_timestamp, -kwh)] + [(item[1], take) for item, take in freed]
                    worst = 0.0
                    for ts in window:
                        if ts < hour_timestamp:
                            continue
                        left = deliverable[ts] + sum(change for change_ts, change in planned if change_ts <= ts)
                        worst = max(worst, -left)
                    return worst

                for timestamp, price in sorted(grid_sell_prices_for_day.items(), key=lambda kv: (kv[1], kv[0]), reverse=True):
                    if not usage or price - usage[0][0] < min_gain_per_kwh:
                        break
                    what_day = days_between(current_hour, timestamp)
                    hour = timestamp.hour
                    hour_timestamp = reset_time_to_hour(timestamp)
                    if timestamp < from_timestamp or hour in exclude_hours or hour_timestamp not in deliverable:
                        continue
                    if timestamp in charging_plan[what_day]["force_discharge_timestamps"] or hour_in(timestamp, charging_plan[what_day]["charging_sessions"]):
                        continue
                    flow = charging_plan[what_day]["battery_level_flow"][hour]
                    raw, loss_cost, battery_kwh_cost = _get_predicted_battery_cost(what_day, hour)
                    if price - battery_kwh_cost < min_profit_per_kwh:
                        continue

                    periods_in_hour = self.price_provider.sell_periods_in_hour(timestamp)
                    min_sell_kwh = self.number("min_sell_kwh") / periods_in_hour
                    slot_kwh = abs(max_kwh_discharging) / periods_in_hour
                    taken = []
                    for _ in range(3):
                        # the cheapest usage hours in the same discharge period give up the kWh
                        taken, kwh_left = [], slot_kwh
                        for item in usage:
                            if kwh_left <= 0.0 or price - item[0] < min_gain_per_kwh:
                                break
                            if not compatible(item[1], hour_timestamp):
                                continue
                            take = min(kwh_left, item[2])
                            taken.append((item, take))
                            kwh_left -= take
                        missing = shortfall(hour_timestamp, slot_kwh - kwh_left, taken)
                        if missing <= 0.01:
                            break
                        slot_kwh = slot_kwh - kwh_left - missing
                        if slot_kwh < min_sell_kwh:
                            taken = []
                            break
                    kwh = sum(take for _, take in taken)
                    gain = sum((price - item[0]) * take for item, take in taken)
                    if kwh < min_sell_kwh or gain < self.get_min_total_profit() / periods_in_hour:
                        continue

                    usage_text = ""
                    changes.append((hour_timestamp, -kwh))
                    for item, take in taken:
                        item[2] -= take
                        changes.append((item[1], take))
                        usage_text += f"{r('at_time', time=item[1].strftime('%H:%M'))}: {take:.2f} kWh ({item[0]:.2f} {valuta_kwh})<br>"
                        if item[2] <= 0.01:
                            # the battery isn't used in this hour, the energy is sold instead
                            usage_data = charging_plan[days_between(current_hour, item[1])]
                            if item[1] in usage_data["discharge_timestamps"]:
                                usage_data["discharge_timestamps"].remove(item[1])
                            usage_data["blocked_discharge_timestamps"][item[1]] = (
                                f"<details><summary>{emoji_parse({'most_profit': True})}{r('title.sold_instead_at', time=timestamp.strftime('%H:%M'))}</summary>"
                                f"{r('price_now')}: **{item[0]:.2f} {valuta_kwh}**<br>"
                                f"{r('sell_price_at', time=timestamp.strftime('%H:%M'))}: **{price:.2f} {valuta_kwh}**<br>"
                                "</details>"
                            )
                    usage = [item for item in usage if item[2] > 0.01]

                    grid_sell_profit = (price - battery_kwh_cost) * kwh
                    _LOGGER.debug("Most profit: selling %.2f kWh at %s (%.2f more than using it)", kwh, timestamp, gain)
                    fixed_price_calc_text = (
                        f"{sell_price:.2f} - {grid_sell_prices.get(timestamp, 0.0):.2f} =" if not using_grid_sell_price else ""
                    )
                    charging_plan[what_day]["force_discharge_timestamps"][timestamp] = {
                        "title": r("title.sell_instead_of_use"),
                        "emoji": emoji_parse({"most_profit": True}),
                        "kwh": kwh,
                        "profit": grid_sell_profit,
                        "reason": (
                            f"<details><summary>{emoji_parse({'most_profit': True})}{r('title.sell_instead_of_use')}<br>"
                            f"({kwh:.1f} kWh / +{gain:.2f} {valuta})</summary>"
                            f"{r('battery_cost_basis')}: **{raw:.2f} {valuta_kwh}**<br>"
                            f"{r('loss_cost')}: **{loss_cost:.2f} {valuta_kwh}**<br>"
                            f"{r('wear_cost')}: **{abs(config['solar']['powerwall_wear_cost_per_kwh']):.2f} {valuta_kwh}**<br>"
                            f"**{r('battery_cost_total')}**: **{battery_kwh_cost:.2f} {valuta_kwh}**<br>"
                            f"{r('grid_sell_price')}: **{fixed_price_calc_text}{price:.2f} {valuta_kwh}**<br>"
                            f"{r('kwh_sold')}: **{kwh:.2f} kWh**<br>"
                            f"{r('profit_selling')}: **{grid_sell_profit:.2f} {valuta}**<br>"
                            f"{r('bought_instead')}:<br>{usage_text}"
                            f"{r('gain_vs_use')}: **{gain:.2f} {valuta}**<br>"
                            "</details>"
                        ),
                    }

            def sell_excess_kwh_available(day):
                from_timestamp = max(charging_plan[day]["start_of_day"], current_sell_price_timestamp)
                day_periods_in_hour = self.price_provider.sell_periods_in_hour(charging_plan[day]["start_of_day"])
                lowest_timestamp, lowest_battery_level, using_next_day = lowest_battery_level_until_next_charge(day)

                usage_minimum = max(config["solar"]["powerwall_battery_level_min"], self.number("usage_reserve"))
                lowest_battery_level = round(lowest_battery_level, 0) - usage_minimum
                excess_kwh_available = round(percentage_to_kwh(max(lowest_battery_level, 0.0)) * efficiency, 1)
                min_sell_kwh = self.number("min_sell_kwh") / day_periods_in_hour

                if excess_kwh_available < min_sell_kwh or excess_kwh_available <= 0.0:
                    charging_plan[day]["force_discharge_timestamps_empty"] = (
                        f"<details><summary>⚠️ {r('title.no_excess')}</summary>"
                        f"{r('lowest_level_time')}: **{lowest_timestamp}**<br>"
                        f"{r('lowest_level')}: **{lowest_battery_level:.1f}%**<br>"
                        f"{r('kwh_available_to_sell')}: **{excess_kwh_available:.2f} kWh**<br>"
                        f"{r('min_kwh_to_sell')}: **{min_sell_kwh:.2f} kWh**<br>"
                        "</details>"
                    )
                    return

                if using_next_day:
                    _LOGGER.debug("Using lowest battery level of next day (%s) for excess kWh on day %s", lowest_timestamp, day)

                discharge_hours_needed = int(round_up(excess_kwh_available / (abs(max_kwh_discharging) / day_periods_in_hour)))
                # until the lowest point (can be days ahead with a big battery), at least the evening + night
                grid_sell_prices_for_day, sell_price, using_grid_sell_price = sell_prices_for_window(
                    from_timestamp, max(charging_plan[day]["end_of_day"] + datetime.timedelta(hours=6), lowest_timestamp + datetime.timedelta(hours=1))
                )
                exclude_hours = self.get_exclude_sell_hours()
                previous_force_discharge = self.plan.get(day, {}).get("force_discharge_timestamps", {}) if day == 0 else {}

                for _ in range(discharge_hours_needed):
                    if round(excess_kwh_available, 1) <= 0.0:
                        break
                    for timestamp, price in sorted(grid_sell_prices_for_day.items(), key=lambda kv: (kv[1], kv[0]), reverse=True):
                        if round(excess_kwh_available, 1) <= 0.0:
                            break
                        if timestamp < from_timestamp:
                            continue
                        if exclude_hours and timestamp.hour in exclude_hours:
                            continue
                        what_day = days_between(charging_plan[0]["start_of_day"], timestamp)
                        if what_day not in charging_plan:
                            continue
                        if timestamp in charging_plan[what_day]["force_discharge_timestamps"]:
                            continue

                        if hour_in(timestamp, charging_plan[what_day]["charging_sessions"]):
                            continue
                        battery_level = charging_plan[what_day]["battery_level_flow"].get(timestamp.hour)
                        if not battery_level:
                            continue
                        loop_raw, loop_loss_cost, loop_battery_kwh_cost = _get_predicted_battery_cost(what_day, timestamp.hour)
                        periods_in_hour = self.price_provider.sell_periods_in_hour(timestamp)
                        usable_kwh = percentage_to_kwh(max(sum(battery_level) - usage_minimum, 0.0)) * efficiency
                        excess_current = min(excess_kwh_available, abs(max_kwh_discharging) / periods_in_hour, usable_kwh)
                        if excess_current < self.number("min_sell_kwh") / periods_in_hour:
                            continue
                        excess_profit = excess_current * price - excess_current * loop_battery_kwh_cost
                        kwh_profit = price - loop_battery_kwh_cost
                        min_total_profit = self.get_min_total_profit() / periods_in_hour

                        already_planned_now = (
                            day == 0
                            and timestamp in previous_force_discharge
                            and hours_between(now, timestamp) == 0
                            and excess_profit > 0.0
                        )
                        if not already_planned_now:
                            if kwh_profit < get_min_profit_per_kwh():
                                continue
                            if excess_profit < min_total_profit:
                                _LOGGER.debug(
                                    "Sell excess: day %s %s profit too low (%.2f < %.2f)",
                                    day, timestamp, excess_profit, min_total_profit,
                                )
                                continue
                        _LOGGER.debug(
                            "Sell excess: day %s selling %.2f kWh at %s (profit %.2f, min %.2f)",
                            day, excess_current, timestamp, excess_profit, min_total_profit,
                        )

                        excess_kwh_available -= excess_current
                        if timestamp in charging_plan[day]["discharge_timestamps"]:
                            charging_plan[day]["discharge_timestamps"].remove(timestamp)

                        other_day = (
                            f"<br><center>**({charging_plan[day]['start_of_day'].date().strftime('%d/%m')})**</center>"
                            if what_day > day else ""
                        )
                        fixed_price_calc_text = (
                            f"{sell_price:.2f} - {grid_sell_prices.get(timestamp, 0.0):.2f} =" if not using_grid_sell_price else ""
                        )
                        charging_plan[what_day]["force_discharge_timestamps"][timestamp] = {
                            "title": r("title.sell_excess"),
                            "emoji": emoji_parse({"sell_excess_kwh_available": True}),
                            "kwh": excess_current,
                            "profit": excess_profit,
                            "reason": (
                                f"<details><summary>{emoji_parse({'sell_excess_kwh_available': True})}{r('title.sell_excess')}<br>"
                                f"({excess_current:.1f} kWh / {excess_profit:.2f} {valuta}){other_day}</summary>"
                                f"{r('battery_cost_basis')}: **{loop_raw:.2f} {valuta_kwh}**<br>"
                                f"{r('loss_cost')}: **{loop_loss_cost:.2f} {valuta_kwh}**<br>"
                                f"{r('wear_cost')}: **{abs(config['solar']['powerwall_wear_cost_per_kwh']):.2f} {valuta_kwh}**<br>"
                                f"**{r('battery_cost_total')}**: **{loop_battery_kwh_cost:.2f} {valuta_kwh}**<br>"
                                f"{r('grid_sell_price')}: **{fixed_price_calc_text}{price:.2f} {valuta_kwh}**<br>"
                                f"{r('excess_kwh_sold')}: **{excess_current:.2f} kWh**<br>"
                                f"{r('profit_selling')}: **{excess_profit:.2f} {valuta}**<br>"
                                "</details>"
                            ),
                        }

                if len(charging_plan[day]["force_discharge_timestamps"]) == 0:
                    raw, loss_cost, battery_kwh_cost = _get_predicted_battery_cost(day)
                    sorted_sell = sorted(grid_sell_prices_for_day.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
                    max_price_timestamp = sorted_sell[0][0] if sorted_sell else None
                    max_price = sorted_sell[0][1] if sorted_sell else 0.0
                    profit = max_price - battery_kwh_cost
                    hour_excluded = ""
                    if exclude_hours and max_price_timestamp and max_price_timestamp.hour in exclude_hours:
                        hour_excluded = f"⚠️ {r('max_price_excluded')}: <b>{max_price_timestamp.hour}:00</b><br>"
                    charging_plan[day]["force_discharge_timestamps_empty"] = (
                        f"<details><summary>⚠️{r('title.no_good_sell_time')} ({profit:.2f})</summary>"
                        f"{hour_excluded}"
                        f"{r('battery_cost_basis')}: <b>{raw:.2f} {valuta_kwh}</b><br>"
                        f"{r('loss_cost')}: <b>{loss_cost:.2f} {valuta_kwh}</b><br>"
                        f"{r('wear_cost')}: <b>{abs(config['solar']['powerwall_wear_cost_per_kwh']):.2f} {valuta_kwh}</b><br>"
                        f"<b>{r('battery_cost_total')}: {battery_kwh_cost:.2f} {valuta_kwh}</b><br>"
                        f"{r('max_price_time')}: <b>{max_price_timestamp if max_price_timestamp else 'N/A'}</b><br>"
                        f"{r('max_sell_price')}: <b>{max_price:.2f} {valuta_kwh}</b><br>"
                        f"{r('min_profit_per_kwh')}: <b>{get_min_profit_per_kwh():.2f} {valuta_kwh}</b><br>"
                        f"{r('profit_selling')}: <b>{profit:.2f} {valuta_kwh}</b><br>"
                        "</details>"
                    )

            def force_powerwall_max_hours_old_discharge(day):
                """Energy older than powerwall_max_hours_old is used even without profit.

                When selling it in the most expensive sell periods pays more than using it (by
                min_profit_per_kwh / min_total_profit, at least min_sell_kwh), it is sold instead.
                """
                max_hours_old = self.number("powerwall_max_hours_old")
                max_days_old = max(int(round(max_hours_old / 24)), 0)
                if day > max_days_old:
                    _LOGGER.debug("Old energy: day %s exceeds the max age (%s days), skipping", day, max_days_old)
                    return battery_expenses_keys
                minimum = config["solar"]["powerwall_battery_level_min"]
                usage_minimum = max(minimum, self.number("usage_reserve"))
                from_timestamp = charging_plan[day]["start_of_day"].replace(hour=current_hour.hour if day == 0 else 0)
                old_keys = [
                    key for key in battery_expenses_keys
                    if reset_time_to_hour(key) + datetime.timedelta(hours=max_hours_old) <= charging_plan[day]["end_of_day"]
                ]
                if not old_keys:
                    return battery_expenses_keys
                old_from = max(min(reset_time_to_hour(key) + datetime.timedelta(hours=max_hours_old) for key in old_keys), from_timestamp)
                old_kwh_total = sum(battery_expenses[key]["kWh"] for key in old_keys)
                old_price = sum(battery_expenses[key]["price"] * battery_expenses[key]["kWh"] for key in old_keys) / old_kwh_total if old_kwh_total > 0.0 else 0.0
                old_flow = charging_plan[day]["battery_level_flow"].get(old_from.hour)
                old_kwh = min(old_kwh_total, percentage_to_kwh(max((old_flow or [minimum])[0] - minimum, 0.0)) * efficiency)
                battery_loss_cost = calc_battery_loss_cost(old_price)
                battery_kwh_cost = old_price + battery_loss_cost + abs(config["solar"]["powerwall_wear_cost_per_kwh"])

                # use it: allow discharging in every hour from when the energy is too old
                usage_prices = []
                for hour in range(old_from.hour, 24):
                    loop_hour = charging_plan[day]["start_of_day"].replace(hour=hour)
                    if hour_in(loop_hour, charging_plan[day]["charging_sessions"]):
                        continue
                    if hour_in(loop_hour, charging_plan[day]["force_discharge_timestamps"]):
                        continue
                    price = grid_prices.get(loop_hour, old_price)
                    usage_prices.append(price)
                    charging_plan[day]["blocked_discharge_timestamps"].pop(loop_hour, None)
                    if loop_hour in charging_plan[day]["discharge_timestamps"]:
                        continue
                    _LOGGER.debug("Old energy: allowing discharge at %s (energy older than %s hours)", loop_hour, max_hours_old)
                    charging_plan[day]["discharge_timestamps"].append(loop_hour)
                    profit = price - old_price
                    charging_plan[day]["force_powerwall_max_hours_old_discharge_timestamps"][loop_hour] = {
                        "kwh": old_kwh,
                        "profit": profit,
                        "reason": (
                            f"<details><summary>{emoji_parse({'force_powerwall_max_hours_old_discharge': True})}{r('title.old_energy_used')} ({profit:.2f})</summary>"
                            f"{r('battery_cost_basis')}: **{old_price:.2f} {valuta_kwh}**<br>"
                            f"{r('loss_cost')}: **{battery_loss_cost:.2f} {valuta_kwh}**<br>"
                            f"{r('wear_cost')}: **{abs(config['solar']['powerwall_wear_cost_per_kwh']):.2f} {valuta_kwh}**<br>"
                            f"**{r('battery_cost_total')}**: **{battery_kwh_cost:.2f} {valuta_kwh}**<br>"
                            f"{r('grid_buy_price')}: **{price:.2f} {valuta_kwh}**<br>"
                            f"{r('profit_using_battery')}: **{profit:.2f} {valuta}**<br>"
                            "</details>"
                        ),
                    }

                # or sell it in the most expensive periods when that pays more than using it
                usage_value = average(usage_prices) if usage_prices else 0.0
                exclude_hours = self.get_exclude_sell_hours()
                sell_prices, sell_price, using_grid_sell_price = sell_prices_for_window(
                    max(old_from, current_sell_price_timestamp), charging_plan[day]["end_of_day"]
                )
                for timestamp, price in sorted(sell_prices.items(), key=lambda kv: (kv[1], kv[0]), reverse=True):
                    gain_per_kwh = price - usage_value
                    if old_kwh <= 0.0 or gain_per_kwh < max(get_min_profit_per_kwh(), 0.01):
                        break
                    if timestamp.hour in exclude_hours or timestamp in charging_plan[day]["force_discharge_timestamps"]:
                        continue
                    if hour_in(timestamp, charging_plan[day]["charging_sessions"]):
                        continue
                    flow = charging_plan[day]["battery_level_flow"].get(timestamp.hour)
                    if not flow:
                        continue
                    periods_in_hour = self.price_provider.sell_periods_in_hour(timestamp)
                    kwh = min(old_kwh, abs(max_kwh_discharging) / periods_in_hour, percentage_to_kwh(max(sum(flow) - usage_minimum, 0.0)) * efficiency)
                    if kwh < self.number("min_sell_kwh") / periods_in_hour or gain_per_kwh * kwh < self.get_min_total_profit() / periods_in_hour:
                        continue
                    old_kwh -= kwh
                    _LOGGER.debug("Old energy: selling %.2f kWh at %s (%.2f %s more than using it)", kwh, timestamp, gain_per_kwh * kwh, valuta)
                    fixed_price_calc_text = (
                        f"{sell_price:.2f} - {grid_sell_prices.get(timestamp, 0.0):.2f} =" if not using_grid_sell_price else ""
                    )
                    charging_plan[day]["force_discharge_timestamps"][timestamp] = {
                        "title": r("title.old_energy_sold"),
                        "emoji": emoji_parse({"force_powerwall_max_hours_old_discharge": True}),
                        "kwh": kwh,
                        "profit": (price - battery_kwh_cost) * kwh,
                        "reason": (
                            f"<details><summary>{emoji_parse({'force_powerwall_max_hours_old_discharge': True})}{r('title.old_energy_sold')}<br>"
                            f"({kwh:.1f} kWh / +{gain_per_kwh * kwh:.2f} {valuta})</summary>"
                            f"**{r('battery_cost_total')}**: **{battery_kwh_cost:.2f} {valuta_kwh}**<br>"
                            f"{r('grid_sell_price')}: **{fixed_price_calc_text}{price:.2f} {valuta_kwh}**<br>"
                            f"{r('avg_price_use')}: **{usage_value:.2f} {valuta_kwh}**<br>"
                            f"{r('kwh_sold')}: **{kwh:.2f} kWh**<br>"
                            f"{r('gain_vs_use')}: **{gain_per_kwh * kwh:.2f} {valuta}**<br>"
                            "</details>"
                        ),
                    }

                for key in old_keys:
                    battery_expenses_keys.remove(key)
                return battery_expenses_keys

            def optimize_solar_charging(day):
                """Sell the solar surplus in the expensive hours and fill the battery in the cheapest solar hours.

                Only when the sun fills the battery to max anyway; the battery still ends the
                day at the same level, so the plan for the following days is unchanged.
                """
                cfg = config["solar"]
                data = charging_plan[day]
                start = data["start_of_day"]
                solar = data.get("solar_kwh_prediction", [])
                predicted_prices = data.get("solar_cost_prediction", [])
                base = data.get("battery_level_flow_without_solar", {})
                original = data.get("battery_level_flow", {})
                blocked = data["blocked_solar_charge_timestamps"] = {}
                if not self.enabled("solar_charging") or not base or not original:
                    return

                minimum = cfg["powerwall_battery_level_min"]
                maximum = cfg["powerwall_battery_level_max"]
                from_hour = now.hour if day == 0 else 0
                hours = [h for h in sorted(base) if h >= from_hour and original.get(h)]
                if not any(sum(original[h]) >= maximum - 0.01 for h in hours):
                    _LOGGER.debug("Optimize solar charging: day %s the battery doesn't reach %s%%, nothing to sell", day, maximum)
                    return

                # What the sold surplus is worth from the real sell prices when known, else the predicted ones (rounded to 0.1)
                sell_by_hour = defaultdict(list)
                for ts, price in grid_sell_prices.items():
                    if ts.date() == start.date():
                        sell_by_hour[ts.hour].append(self.sell_value(price))
                busy = {
                    ts.hour for ts in [*data.get("charging_sessions", {}), *data["force_discharge_timestamps"]]
                    if ts.date() == start.date()
                }
                delta, available, prices = {}, {}, {}
                for h in hours:
                    # the solar surplus covers the house, so no consumption from the battery in solar hours
                    delta[h] = base[h][1] if len(base[h]) > 1 and not (h < len(solar) and solar[h] > 0) else 0.0
                    if h < len(solar) and h < len(predicted_prices) and solar[h] > 0:
                        # same conversion as battery_level_flow_prediction()
                        available[h] = kwh_to_percentage(min(solar[h], max_kwh_charging)) * efficiency
                        prices[h] = average(sell_by_hour[h]) if sell_by_hour.get(h) else float(predicted_prices[h])
                candidates = [h for h in available if h not in busy]
                if not candidates:
                    return

                blocked_hours, flow, charged, original_charged = _optimize_solar_blocking(
                    hours, delta, available, prices, candidates, float(original[hours[0]][0]), minimum, maximum
                )
                if not blocked_hours:
                    return

                gain = percentage_to_kwh(sum((original_charged[h] - charged[h]) * prices[h] for h in available)) / efficiency
                moved_text = "".join(
                    f"{r('at_time', time=f'{h:02d}:00')}: {percentage_to_kwh(charged[h] - original_charged[h]) / efficiency:.2f} kWh ({prices[h]:.2f} {valuta_kwh})<br>"
                    for h in sorted(available)
                    if percentage_to_kwh(charged[h] - original_charged[h]) / efficiency >= 0.01
                )
                moved_to = [h for h in sorted(available) if percentage_to_kwh(charged[h] - original_charged[h]) / efficiency >= 0.01]
                charged_at = f" ({moved_to[0]:02d}:00)" if moved_to else ""
                for h in sorted(blocked_hours):
                    ts = start.replace(hour=h, minute=0, second=0, microsecond=0)
                    blocked[ts] = {
                        "reason": (
                            f"<details><summary>{emoji_parse({'blocked_solar_charge': True})}{r('title.blocked_solar')}{charged_at}</summary>"
                            f"<b>{r('before_blocking')}:</b> {sum(original[h]):.1f}%<br>"
                            f"<b>{r('after_blocking')}:</b> {sum(flow[h]):.1f}%<br>"
                            f"<b>{r('solar_sold')}:</b> {percentage_to_kwh(available[h]) / efficiency:.2f} kWh<br>"
                            f"<b>{r('sell_price')}:</b> {prices[h]:.2f} {valuta_kwh}<br>"
                            f"<b>{r('expected_gain_day')}:</b> {gain:.2f} {valuta}<br><br>"
                            f"<b>{r('charged_instead')}:</b><br>{moved_text}</details>"
                        )
                    }
                    if ts in data["discharge_timestamps"]:
                        data["discharge_timestamps"].remove(ts)
                if blocked:
                    _LOGGER.debug("Optimize solar charging: day %s blocking solar charging at %s", day, ", ".join(ts.strftime("%H:%M") for ts in sorted(blocked)))

            def negative_price_planner(day):
                """Fill the battery when the buy price is so low that charging pays even if the kWh are never used.

                Solar first is soft here: the solar surplus pushed out is sold for more than the grid kWh cost.
                """
                nonlocal total_cost, total_kwh
                for timestamp, price in sorted_by_cheapest_price:
                    kwh_profit = charge_kwh_profit(price, 0.0)
                    if kwh_profit < max(get_min_profit_per_kwh(), 0.01):
                        break
                    what_day = days_between(current_hour, timestamp)
                    if what_day != day or timestamp < current_hour:
                        continue
                    hour_in_charge_hours, kwh_available = kwh_available_in_hour(timestamp)
                    kwh_available = round(min(kwh_available, grid_headroom_kwh(timestamp, price)), 1)
                    if kwh_available <= 0.0 or not is_profitable(kwh_profit, kwh_available):
                        continue
                    _, total_cost, total_kwh, battery_level_added, cost_added = add_to_charge_hours(
                        kwh_available, total_cost, total_kwh, timestamp, price, kwh_available, rules=["negative_price_planner"],
                    )
                    if timestamp in charge_hours and battery_level_added:
                        charging_plan[day]["total_cost"] += cost_added
                        reason = (
                            f"<details><summary>{emoji_parse({'negative_price_planner': True})}{r('title.negative_price')} ({price}/{battery_level_added:.0f}%)</summary>"
                            f"{r('current_price')}: **{price} {valuta_kwh}**<br>"
                            f"{r('profit_without_use')}: **{kwh_profit:.2f} {valuta_kwh}**<br>"
                            f"{r('level_added')}: **{battery_level_added:.2f}%**<br>"
                            "</details>"
                        )
                        add_charging_session_to_day(timestamp, what_day, reason=reason)
                        add_charging_to_days(timestamp)

            def room_until(from_timestamp, to_timestamp):
                """Grid kWh that fit in the battery from from_timestamp until (excl.) to_timestamp."""
                highest = config["solar"]["powerwall_battery_level_min"]
                loop_timestamp = reset_time_to_hour(from_timestamp)
                while loop_timestamp < to_timestamp:
                    data = charging_plan.get(days_between(current_hour, loop_timestamp))
                    flow = data["battery_level_flow"].get(loop_timestamp.hour) if data else None
                    if flow:
                        highest = max(highest, sum(flow), flow[0])
                    loop_timestamp += datetime.timedelta(hours=1)
                return percentage_to_kwh(max(config["solar"]["powerwall_battery_level_max"] - highest, 0.0)) / efficiency

            def grid_charge_to_sell(day):
                """Charge from the grid only to sell the kWh again in the most expensive sell periods.

                A sale pairs with the cheapest earlier buy hour: sell price minus buy price incl. loss and
                wear has to beat min_profit_per_kwh, the sale min_sell_kwh and min_total_profit. The kWh
                have to fit in the battery until the sale, and an hour either charges or sells.
                """
                nonlocal total_cost, total_kwh
                min_gain = max(get_min_profit_per_kwh(), 0.01)
                exclude_hours = self.get_exclude_sell_hours()
                sell_prices, sell_price, using_grid_sell_price = sell_prices_for_window(
                    max(charging_plan[day]["start_of_day"], current_sell_price_timestamp), charging_plan[day]["end_of_day"]
                )
                for sell_timestamp, price in sorted(sell_prices.items(), key=lambda kv: (kv[1], kv[0]), reverse=True):
                    hour_timestamp = reset_time_to_hour(sell_timestamp)
                    data = charging_plan[day]
                    if sell_timestamp.hour in exclude_hours or sell_timestamp in data["force_discharge_timestamps"]:
                        continue
                    if hour_in(sell_timestamp, data["charging_sessions"]) or solar_surplus_in_hour(sell_timestamp) > 0.0:
                        continue
                    periods_in_hour = self.price_provider.sell_periods_in_hour(sell_timestamp)
                    min_sell_kwh = self.number("min_sell_kwh") / periods_in_hour
                    slot_kwh = abs(max_kwh_discharging) / periods_in_hour
                    if hour_timestamp in data["discharge_timestamps"]:
                        # the house uses the same discharge rate
                        slot_kwh -= data["hour_cost_prediction"][forecast_type].get(hour_timestamp.hour, {}).get("kwh", 0.0) / periods_in_hour
                    if slot_kwh < min_sell_kwh:
                        continue

                    for buy_timestamp, buy_price in sorted_by_cheapest_price:
                        kwh_profit = charge_kwh_profit(buy_price, price)
                        if kwh_profit < min_gain:
                            break
                        buy_day = days_between(current_hour, buy_timestamp)
                        if buy_timestamp < current_hour or buy_timestamp >= hour_timestamp or buy_day not in charging_plan:
                            continue
                        _, kwh_available = kwh_available_in_hour(buy_timestamp)
                        grid_kwh = round(min(kwh_available, room_until(buy_timestamp, hour_timestamp), slot_kwh / efficiency**2), 2)
                        delivered_kwh = grid_kwh * efficiency**2
                        if delivered_kwh < min_sell_kwh or not is_profitable(kwh_profit, delivered_kwh, periods_in_hour):
                            continue
                        charged_before = charge_hours.get(buy_timestamp, {}).get("kWh", 0.0)
                        _, total_cost, total_kwh, battery_level_added, cost_added = add_to_charge_hours(
                            grid_kwh, total_cost, total_kwh, buy_timestamp, buy_price, grid_kwh, rules=["grid_charge_to_sell"],
                        )
                        if not battery_level_added:
                            continue
                        delivered_kwh = (charge_hours[buy_timestamp]["kWh"] - charged_before) * efficiency**2
                        charging_plan[buy_day]["total_cost"] += cost_added
                        fixed_price_calc_text = (
                            f"{sell_price:.2f} - {grid_sell_prices.get(sell_timestamp, 0.0):.2f} =" if not using_grid_sell_price else ""
                        )
                        data["force_discharge_timestamps"][sell_timestamp] = {
                            "title": r("title.sell_bought_cheap"),
                            "emoji": emoji_parse({"grid_charge_to_sell": True}),
                            "kwh": delivered_kwh,
                            "profit": kwh_profit * delivered_kwh,
                            "reason": (
                                f"<details><summary>{emoji_parse({'grid_charge_to_sell': True})}{r('title.sell_bought_cheap')}<br>"
                                f"({delivered_kwh:.1f} kWh / +{kwh_profit * delivered_kwh:.2f} {valuta})</summary>"
                                f"{r('bought_at', time=buy_timestamp.strftime('%d/%m %H:%M'), price=f'{buy_price:.2f} {valuta_kwh}')}<br>"
                                f"{r('grid_sell_price')}: **{fixed_price_calc_text}{price:.2f} {valuta_kwh}**<br>"
                                f"{r('loss_wear_per_kwh')}: **{calc_battery_loss_cost(buy_price) + abs(config['solar']['powerwall_wear_cost_per_kwh']):.2f} {valuta_kwh}**<br>"
                                f"{r('profit_per_kwh')}: **{kwh_profit:.2f} {valuta_kwh}**<br>"
                                "</details>"
                            ),
                        }
                        reason = (
                            f"<details><summary>{emoji_parse({'grid_charge_to_sell': True})}{r('title.buy_to_sell')} ({buy_price}/{battery_level_added:.0f}%)"
                            f"{other_day_text(day, buy_day)}</summary>"
                            f"{r('sold_at', time=sell_timestamp.strftime('%d/%m %H:%M'), price=f'{price:.2f} {valuta_kwh}')}<br>"
                            f"{r('profit_per_kwh')}: **{kwh_profit:.2f} {valuta_kwh}**<br>"
                            f"{r('level_added')}: **{battery_level_added:.2f}%**<br>"
                            "</details>"
                        )
                        add_charging_session_to_day(buy_timestamp, buy_day, reason=reason)
                        add_charging_to_days(buy_timestamp)
                        break

            def discharge_before_solar(day):
                """Use/sell battery energy before the sun when the battery gets full of solar anyway.

                Every kWh emptied before the sun is refilled with solar surplus that would otherwise be
                sold, so a kWh used (buy price) or sold (sell price) has to beat that solar sell price
                incl. loss and wear by min_profit_per_kwh (sales also min_sell_kwh / min_total_profit).
                """
                minimum = config["solar"]["powerwall_battery_level_min"]
                maximum = config["solar"]["powerwall_battery_level_max"]
                data = charging_plan[day]
                start = data["start_of_day"]
                wear = abs(config["solar"]["powerwall_wear_cost_per_kwh"])
                hours = [hour for hour in range(now.hour if day == 0 else 0, 24) if data["battery_level_flow"].get(hour)]
                solar_hours = [hour for hour in hours if solar_surplus_in_hour(start.replace(hour=hour)) > 0.0]
                if not solar_hours:
                    return

                # solar surplus pushed out of the full battery (battery %) and what it is sold for
                pushed_out, pushed_out_value = 0.0, 0.0
                for hour in solar_hours:
                    timestamp = start.replace(hour=hour)
                    if hour_in(timestamp, data["blocked_solar_charge_timestamps"]) or hour_in(timestamp, data["force_discharge_timestamps"]):
                        continue
                    flow = data["battery_level_flow"][hour]
                    stored = kwh_to_percentage(solar_kwh_in_hour(timestamp)) * efficiency
                    grid = data["charging_sessions"].get(timestamp, {}).get("battery_level", 0.0)
                    pushed = max(stored + grid - (sum(flow) - flow[0]), 0.0) if sum(flow) >= maximum - 0.01 else 0.0
                    pushed_out += pushed
                    pushed_out_value += pushed * sell_price_in_hour(timestamp)
                if pushed_out <= 0.01:
                    return
                solar_sell_price = pushed_out_value / pushed_out
                # 1 kWh out of the battery takes 1/efficiency, refilling that takes 1/efficiency² of solar
                refill_cost = solar_sell_price / efficiency**2 + wear

                # the hours before the sun since the battery was last full or grid charged
                window = []
                for hour in hours:
                    if hour >= solar_hours[0]:
                        break
                    timestamp = start.replace(hour=hour)
                    flow = data["battery_level_flow"][hour]
                    if hour_in(timestamp, data["charging_sessions"]) or sum(flow) >= maximum - 0.01:
                        window = []
                        continue
                    window.append(hour)
                if not window:
                    return
                budget_kwh = min(
                    percentage_to_kwh(pushed_out),
                    min(percentage_to_kwh(max(sum(data["battery_level_flow"][hour]) - minimum, 0.0)) for hour in window),
                ) * efficiency
                usage_minimum = max(minimum, self.number("usage_reserve"))
                sell_budget_kwh = min(
                    percentage_to_kwh(pushed_out),
                    min(percentage_to_kwh(max(sum(data["battery_level_flow"][hour]) - usage_minimum, 0.0)) for hour in window),
                ) * efficiency

                candidates = []
                for hour in window:
                    timestamp = start.replace(hour=hour)
                    prediction = data["hour_cost_prediction"][forecast_type].get(hour)
                    price = grid_prices.get(timestamp)
                    if timestamp not in data["discharge_timestamps"] and prediction and prediction["kwh"] > 0.0 and price is not None:
                        candidates.append((price, timestamp, "use", prediction["kwh"], 1))
                exclude_hours = self.get_exclude_sell_hours()
                sell_prices, sell_price, using_grid_sell_price = sell_prices_for_window(
                    max(start.replace(hour=window[0]), current_sell_price_timestamp), start.replace(hour=window[-1]) + datetime.timedelta(hours=1)
                )
                for timestamp, price in sell_prices.items():
                    if timestamp.hour in exclude_hours or timestamp in data["force_discharge_timestamps"]:
                        continue
                    periods_in_hour = self.price_provider.sell_periods_in_hour(timestamp)
                    slot_kwh = abs(max_kwh_discharging) / periods_in_hour
                    if reset_time_to_hour(timestamp) in data["discharge_timestamps"]:
                        slot_kwh -= data["hour_cost_prediction"][forecast_type].get(timestamp.hour, {}).get("kwh", 0.0) / periods_in_hour
                    candidates.append((price, timestamp, "sell", slot_kwh, periods_in_hour))

                for price, timestamp, kind, kwh, periods_in_hour in sorted(candidates, key=lambda item: (item[0], item[1]), reverse=True):
                    gain = price - refill_cost
                    if budget_kwh <= 0.01 or gain < max(get_min_profit_per_kwh(), 0.01):
                        break
                    if kind == "use":
                        # discharge_allowed covers the whole hour, so the budget has to cover all of it
                        if kwh > budget_kwh or not is_profitable(gain, kwh):
                            continue
                        allow_discharge(timestamp)
                        data.setdefault("discharge_before_solar_timestamps", {})[timestamp] = gain * kwh
                        _LOGGER.debug("Discharge before solar: using %.2f kWh at %s (%.2f %s better than selling the solar)", kwh, timestamp, gain * kwh, valuta)
                    else:
                        kwh = min(kwh, budget_kwh, sell_budget_kwh)
                        if kwh < self.number("min_sell_kwh") / periods_in_hour or not is_profitable(gain, kwh, periods_in_hour):
                            continue
                        fixed_price_calc_text = (
                            f"{sell_price:.2f} - {grid_sell_prices.get(timestamp, 0.0):.2f} =" if not using_grid_sell_price else ""
                        )
                        data["force_discharge_timestamps"][timestamp] = {
                            "title": r("title.sell_before_solar"),
                            "emoji": emoji_parse({"discharge_before_solar": True}),
                            "kwh": kwh,
                            "profit": gain * kwh,
                            "reason": (
                                f"<details><summary>{emoji_parse({'discharge_before_solar': True})}{r('title.sell_before_solar')}<br>"
                                f"({kwh:.1f} kWh / +{gain * kwh:.2f} {valuta})</summary>"
                                f"{r('grid_sell_price')}: **{fixed_price_calc_text}{price:.2f} {valuta_kwh}**<br>"
                                f"{r('solar_sold_otherwise')}: **{solar_sell_price:.2f} {valuta_kwh}**<br>"
                                f"{r('refill_cost')}: **{refill_cost:.2f} {valuta_kwh}**<br>"
                                f"{r('solar_not_fitting')}: **{percentage_to_kwh(pushed_out) / efficiency:.2f} kWh**<br>"
                                f"{r('gain')}: **{gain * kwh:.2f} {valuta}**<br>"
                                "</details>"
                            ),
                        }
                    budget_kwh -= kwh
                    sell_budget_kwh = max(sell_budget_kwh - kwh, 0.0)

            def shadow_plan(days):
                """Whole horizon optimisation next to the rules (compared in the overview).

                Returns (hours, optimal result, explanations) for apply_optimizer_plan(), None without prices.
                """
                buy_by_hour = defaultdict(list)
                for timestamp, price in grid_prices.items():
                    buy_by_hour[reset_time_to_hour(timestamp)].append(price)
                # the planned kWh per charge hour - charge_if_needed() stops when they are in the battery
                charge_hour_kwh = defaultdict(float)
                for ts, session in charge_hours.items():
                    if isinstance(ts, datetime.datetime):
                        charge_hour_kwh[reset_time_to_hour(ts)] += session["kWh"]
                exclude_hours = self.get_exclude_sell_hours()
                hours, rule_actions, rule_limits = [], [], []
                for day in days:
                    data = charging_plan[day]
                    # the planned kWh per sell hour - force discharge only runs in the planned sell periods
                    force_hours = defaultdict(float)
                    for ts, sale in data["force_discharge_timestamps"].items():
                        force_hours[reset_time_to_hour(ts)] += sale["kwh"]
                    blocked_solar_hours = {ts.hour for ts in data["blocked_solar_charge_timestamps"]}
                    for hour in sorted(data["battery_level_flow"]):
                        timestamp = data["start_of_day"].replace(hour=hour)
                        prediction = data["hour_cost_prediction"][forecast_type].get(hour)
                        if prediction is None or not buy_by_hour.get(timestamp):
                            continue
                        solar = solar_surplus_in_hour(timestamp)
                        # the current hour only has the minutes left (the solar prediction is already scaled)
                        fraction = max(1.0 - now.minute / 60.0, 0.05) if timestamp == current_hour else 1.0
                        hours.append(OptimizerHour(
                            timestamp, average(buy_by_hour[timestamp]), sell_price_in_hour(timestamp),
                            0.0 if solar > 0.0 else prediction["kwh"] * fraction, solar,
                            can_sell=hour not in exclude_hours, fraction=fraction,
                        ))
                        # same priority as charge_if_needed()
                        limit = None
                        if timestamp in charge_hour_kwh:
                            action, limit = "grid_charging", charge_hour_kwh[timestamp]
                        elif timestamp in force_hours:
                            action, limit = "force_discharge", force_hours[timestamp]
                        elif timestamp in data["discharge_timestamps"] or timestamp in data["force_powerwall_max_hours_old_discharge_timestamps"]:
                            action = "discharge_allowed"
                        elif hour in blocked_solar_hours:
                            action = "blocked_solar_charge"
                        else:
                            action = "force_solar_only_charging"
                        rule_actions.append(action)
                        rule_limits.append(limit)
                if not hours:
                    return None
                wear = abs(config["solar"]["powerwall_wear_cost_per_kwh"])
                optimizer_config = OptimizerConfig(
                    battery_size=config["solar"]["powerwall_battery_size"],
                    level_min=config["solar"]["powerwall_battery_level_min"],
                    level_max=config["solar"]["powerwall_battery_level_max"],
                    max_charge=max_kwh_charging,
                    max_discharge=abs(max_kwh_discharging),
                    efficiency=efficiency,
                    wear=wear,
                    charge_efficiency_curve=self.charging_efficiency_curve(),
                    discharge_efficiency_curve=self.discharging_efficiency_curve(),
                    curve_fallback_efficiency=self._fallback_efficiency(),
                    min_profit_per_kwh=get_min_profit_per_kwh(),
                    min_sell_kwh=self.number("min_sell_kwh"),
                    min_total_profit=self.get_min_total_profit(),
                    usage_reserve=self.number("usage_reserve"),
                )
                # a kWh left in the battery at the end saves the average buy price later (after wear and
                # the min. profit, else buying just to hold the energy at the end would pay)
                end_value = efficiency * max(average([hour.buy for hour in hours]) - wear - get_min_profit_per_kwh(), 0.0)
                start_level = get_battery_level()
                # the rule switches decide which actions the optimiser may use;
                # use_midnight_battery_level: every day is optimised on its own until midnight
                optimize = optimize_per_day if self.enabled("use_midnight_battery_level") else optimize_with_rules
                optimal, explanations = optimize(hours, start_level, optimizer_config, end_value, self.enabled)
                rules = simulate(hours, start_level, optimizer_config, rule_actions, end_value, rule_limits)
                self_consumption = simulate(hours, start_level, optimizer_config, ["discharge_allowed"] * len(hours), end_value)
                optimal_limits = [
                    grid if action == "grid_charging" else sold if action == "force_discharge" else None
                    for action, grid, sold in zip(optimal.actions, optimal.grid_kwh, optimal.sold_kwh)
                ]
                self.optimizer_shadow = {
                    # totals of the plan for the overview (the one that controls the battery is shown)
                    "summary_rules": breakdown(hours, start_level, optimizer_config, rule_actions, end_value, rule_limits),
                    "summary_optimal": breakdown(hours, start_level, optimizer_config, optimal.actions, end_value, optimal_limits),
                    "rules": round(rules.total, 2),
                    "optimal": round(optimal.total, 2),
                    "self_consumption": round(self_consumption.total, 2),
                    "hours": [
                        {
                            "hour": hour.timestamp,
                            "action": action,
                            "battery_level": level,
                            "rules_action": rule_action,
                            "rules_battery_level": rule_level,
                            "grid_kwh": grid_kwh,
                            "sold_kwh": sold_kwh,
                            "rules_grid_kwh": rule_kwh,
                            "buy": round(hour.buy, 2),
                            "sell": round(hour.sell, 2),
                        }
                        for hour, action, level, grid_kwh, sold_kwh, rule_action, rule_level, rule_kwh in zip(
                            hours, optimal.actions, optimal.levels, optimal.grid_kwh, optimal.sold_kwh, rules.actions, rules.levels, rules.grid_kwh
                        )
                    ],
                }
                _LOGGER.debug(
                    "Whole period optimisation over %s hours: optimal %.2f, rules %.2f, self consumption %.2f",
                    len(hours), optimal.total, rules.total, self_consumption.total,
                )
                return hours, optimal, explanations

            def apply_optimizer_plan(days, hours, optimal, explanations):
                """The optimiser controls the battery: its action per hour replaces the rule based plan.

                Every action is explained as the rule it corresponds to (same titles/emojis as the
                rules) with the hour it is for. Everything is built first and swapped in at the end,
                so a failure leaves the rule plan. Partial charge hours stop at their kWh (replanned
                every cron interval), and a sale uses the most expensive sell periods of the hour.
                """
                charge_titles = {
                    "cheapest_hour_fill_planner": r("title.cheapest_hours"),
                    "most_expensive_planner": r("title.most_expensive_hours"),
                    "needed_before_max_level_planner": r("title.needed"),
                    "negative_price_planner": r("title.negative_price"),
                    "grid_charge_to_sell": r("title.buy_to_sell"),
                }
                sale_titles = {
                    "grid_charge_to_sell": r("title.sell_bought_cheap"),
                    "discharge_before_solar": r("title.sell_before_solar"),
                    "most_profit": r("title.sell_instead_of_use"),
                }

                def when(index):
                    return hours[index].timestamp.strftime("%d/%m %H:%M")

                new_charge_hours = {}
                new_plan = {
                    day: {
                        "charging_sessions": {},
                        "discharge_timestamps": [],
                        "force_discharge_timestamps": {},
                        "blocked_discharge_timestamps": {},
                        "blocked_solar_charge_timestamps": {},
                        "force_powerwall_max_hours_old_discharge_timestamps": {},
                    }
                    for day in days
                }
                new_total_cost = new_total_kwh = 0.0
                rows = zip(hours, optimal.actions, optimal.levels, optimal.grid_kwh, optimal.sold_kwh, explanations)
                for hour, action, level, grid_kwh, sold_kwh, explanation in rows:
                    timestamp = hour.timestamp
                    plan_day = new_plan[days_between(current_hour, timestamp)]
                    rule = explanation["rule"] if explanation else None
                    target = explanation["target"] if explanation else None
                    level_text = f"{r('level_after_hour')}: **{level:.0f}%**<br>"
                    if action == "grid_charging" and grid_kwh >= 0.1:  # a few Wh is not worth a charge hour
                        percentage = kwh_to_percentage(grid_kwh) * efficiency
                        session = {
                            "Cost": round(grid_kwh * hour.buy, 2),
                            "kWh": round(grid_kwh, 2),
                            "battery_level": round(percentage, 2),
                            "Price": round(hour.buy, 2),
                            rule: True,
                        }
                        if target is None:
                            target_text = ""
                        elif rule == "grid_charge_to_sell":
                            target_text = (
                                f"{r('sold_at', time=when(target), price=f'{hours[target].sell:.2f} {valuta_kwh}')}<br>"
                                f"{r('profit_per_kwh')}: **{charge_kwh_profit(hour.buy, hours[target].sell):.2f} {valuta_kwh}**<br>"
                            )
                        else:
                            target_text = (
                                f"{r('used_at', time=when(target), price=f'{hours[target].buy:.2f} {valuta_kwh}')}<br>"
                                f"{r('profit_per_kwh')}: **{charge_kwh_profit(hour.buy, hours[target].buy):.2f} {valuta_kwh}**<br>"
                            )
                        new_charge_hours[timestamp] = session
                        plan_day["charging_sessions"][timestamp] = {
                            **session,
                            "reason": (
                                f"<details><summary>{emoji_parse({rule: True})}{charge_titles[rule]} ({hour.buy:.2f}/{percentage:.0f}%)</summary>"
                                f"{r('charging_at', kwh=f'{grid_kwh:.2f} kWh', price=f'{hour.buy:.2f} {valuta_kwh}')}<br>"
                                f"{target_text}{level_text}"
                                "</details>"
                            ),
                        }
                        new_total_cost += grid_kwh * hour.buy
                        new_total_kwh += grid_kwh
                    elif action == "force_discharge" and sold_kwh > 0.01:
                        title = sale_titles[rule]
                        if rule == "grid_charge_to_sell" and explanation.get("kind") == "recharged":
                            title = r("title.sell_before_cheap_charge")
                            detail = f"{r('recharged_at', time=when(target), price=f'{hours[target].buy:.2f} {valuta_kwh}')}<br>"
                        elif rule == "grid_charge_to_sell" and target is not None:
                            detail = f"{r('bought_at', time=when(target), price=f'{hours[target].buy:.2f} {valuta_kwh}')}<br>"
                        elif rule == "discharge_before_solar" and target is not None:
                            detail = f"{r('solar_refills_at', time=when(target), price=f'{hours[target].sell:.2f} {valuta_kwh}')}<br>"
                        else:
                            detail = f"{r('worth_more_sold_now')}<br>"
                        emoji = emoji_parse({rule: True})
                        periods, _, _ = sell_prices_for_window(timestamp, timestamp + datetime.timedelta(hours=1))
                        periods = periods or {timestamp: hour.sell}
                        period_kwh = abs(max_kwh_discharging) / len(periods)
                        count = min(len(periods), max(1, math.ceil(sold_kwh / period_kwh - 0.01)))
                        for period, price in sorted(periods.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)[:count]:
                            plan_day["force_discharge_timestamps"][period] = {
                                "title": title,
                                "emoji": emoji,
                                "kwh": sold_kwh / count,
                                "profit": sold_kwh / count * price,
                                "reason": (
                                    f"<details><summary>{emoji}{title}<br>({sold_kwh / count:.1f} kWh / {price:.2f} {valuta_kwh})</summary>"
                                    f"{r('sell_price')}: **{price:.2f} {valuta_kwh}**<br>{detail}{level_text}"
                                    "</details>"
                                ),
                            }
                        # the battery also covers the house in the rest of the hour
                        plan_day["discharge_timestamps"].append(timestamp)
                    elif action == "discharge_allowed":
                        plan_day["discharge_timestamps"].append(timestamp)
                    elif action == "blocked_solar_charge":
                        later = f"{r('charged_instead_at', time=when(target))}<br>" if target is not None else ""
                        charged_at = f" ({hours[target].timestamp.strftime('%H:%M')})" if target is not None else ""
                        plan_day["blocked_solar_charge_timestamps"][timestamp] = {
                            "reason": (
                                f"<details><summary>{emoji_parse({'blocked_solar_charge': True})}{r('title.blocked_solar')}{charged_at}</summary>"
                                f"{r('solar_sold_for')}: **{hour.sell:.2f} {valuta_kwh}**<br>{later}{level_text}"
                                "</details>"
                            )
                        }
                    elif rule == "prioritize_discharge_hours_by_energy_cost":
                        later = (
                            f"{r('used_instead_at', time=when(target), price=f'{hours[target].buy:.2f} {valuta_kwh}')}<br>"
                            if target is not None else f"{r('worth_more_later')}<br>"
                        )
                        title = r("title.most_expensive_prioritized") + (f" ({hours[target].timestamp.strftime('%H:%M')})" if target is not None else "")
                        plan_day["blocked_discharge_timestamps"][timestamp] = (
                            f"<details><summary>{emoji_parse({'prioritize_discharge_hours_by_energy_cost': True})}{title}</summary>"
                            f"{r('price_now')}: **{hour.buy:.2f} {valuta_kwh}**<br>{later}{level_text}"
                            "</details>"
                        )

                charge_hours.clear()
                charge_hours.update(new_charge_hours)
                for day in days:
                    charging_plan[day].update(new_plan[day])
                    charging_plan[day]["total_cost"] = sum(
                        session["Cost"] for ts, session in new_charge_hours.items() if days_between(current_hour, ts) == day
                    )
                return round(new_total_cost, 2), round(new_total_kwh, 2)

            charging_rules = [
                ("only_discharge_on_profit", self.enabled("only_discharge_on_profit"), only_discharge_on_profit),
                ("negative_price_planner", self.enabled("negative_price_planner"), negative_price_planner),
                ("cheapest_hour_fill_planner", self.enabled("cheapest_hour_fill_planner"), cheapest_hour_fill_planner),
                ("most_expensive_planner", self.enabled("most_expensive_planner"), most_expensive_planner),
                ("needed_before_max_level_planner", self.enabled("needed_before_max_level_planner"), needed_before_max_level_planner),
                ("prioritize_discharge_hours_by_energy_cost", self.enabled("prioritize_discharge_hours_by_energy_cost"), prioritize_discharge_hours_by_energy_cost),
            ]
            sell_rules = [
                ("discharge_before_solar", self.enabled("discharge_before_solar"), discharge_before_solar),
                ("most_profit", self.enabled("most_profit"), most_profit),
                ("sell_excess_kwh_available", self.enabled("sell_excess_kwh_available"), sell_excess_kwh_available),
                ("grid_charge_to_sell", self.enabled("grid_charge_to_sell"), grid_charge_to_sell),
                ("force_powerwall_max_hours_old_discharge", self.enabled("force_powerwall_max_hours_old_discharge"), force_powerwall_max_hours_old_discharge),
            ]
            days = sorted(key for key in charging_plan if isinstance(key, int))

            for rules in (charging_rules, sell_rules):
                for day in days:
                    for rule_name, enabled, func in rules:
                        if not enabled:
                            continue
                        try:
                            func(day)
                        except Exception as err:  # noqa: BLE001
                            _LOGGER.exception("Error in %s day:%s", rule_name, day)
                            self.notify(
                                f"Error in {rule_name} day:{day}: {err} {type(err)}",
                                title=f"{self.title} error",
                                notification_id=f"{name}_{rule_name}_error_{day}",
                            )
                        battery_level_flow_prediction_recalc()

            if self.enabled("optimize_solar_charging"):
                for day in days:
                    optimize_solar_charging(day)

            battery_level_flow_prediction_recalc(final_recalc=True)
            for day in days:
                discharge_amount(day)

            # always calculated: it controls the battery, or it is compared with the rules
            self.optimizer_shadow = None
            try:
                result = shadow_plan(days)
                if self.enabled("optimizer_control") and result is not None:
                    total_cost, total_kwh = apply_optimizer_plan(days, *result)
                    self.optimizer_shadow["controls"] = True
                    battery_level_flow_prediction_recalc(final_recalc=True)
                    for day in days:
                        discharge_amount(day)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Whole period optimisation failed, the rules control the battery")
            return [total_cost, total_kwh]

        # ------------------------------------------------------------------ #
        # Build the base plan
        # ------------------------------------------------------------------ #
        diff_procent = []
        for hour in range(48):
            dt = current_hour - datetime.timedelta(hours=hour + 1)
            consumption = self.power_values(from_timestamp=dt, to_timestamp=dt + datetime.timedelta(hours=1))[
                "power_consumption_without_all_exclusion"
            ]
            if consumption <= 0.0:
                continue
            forecast = self.get_forecast_value(self.power_values_db.get(dt.hour, {}).get("power_consumption_without_all_exclusion", [0.0]))
            diff_procent.append(consumption / forecast if forecast > 0.0 else 0.0)
        correction_factor = sum(diff_procent) / len(diff_procent) if diff_procent else 1.0

        for day in range(amount_of_days):
            start_of_day_datetime = start_of_day(now) + datetime.timedelta(days=day)
            end_of_day_datetime = end_of_day(now) + datetime.timedelta(days=day)
            from_hour = now.hour if day == 0 else 0

            charging_plan[day] = {
                "start_of_day": start_of_day_datetime,
                "end_of_day": end_of_day_datetime,
                "day_text": day_of_week_text(end_of_day_datetime),
                "hour_cost_prediction": {"ema": {}, "trend": {}, "average": {}},
                "sorted_hour_cost_prediction": {"ema": [], "trend": [], "average": []},
                "grouped_hour_cost_prediction": {"ema": {}, "trend": {}, "average": {}},
                "grouped_sorted_hour_cost_prediction": {"ema": [], "trend": [], "average": []},
                "charging_sessions": {},
                "solar_kwh_prediction": [],
                "solar_cost_prediction": [],
                "solar_prediction_corrected": False,
                "battery_level_start_of_day": [],
                "battery_level_end_of_day": [],
                "battery_level_flow": {},
                "battery_level_flow_without_solar": {},
                "total_cost": 0.0,
                "discharge_kwh": 0.0,
                "discharge_timestamps": [],
                "force_discharge_timestamps": {},
                "blocked_discharge_timestamps": {},
                # dict (the PyScript version started with a list, which broke when
                # optimize_solar_charging was disabled)
                "blocked_solar_charge_timestamps": {},
                "force_powerwall_max_hours_old_discharge_timestamps": {},
            }
            charging_plan[day]["hour_cost_prediction"]["correction_factor"] = correction_factor

            for hour in range(from_hour, 24):
                dt = start_of_day_datetime + datetime.timedelta(hours=hour)
                values = reverse_list(get_list_values(
                    self.power_values_db.get(hour, {}).get("power_consumption_without_all_exclusion", [0.0])
                ))
                ema = calculate_ema(values) / 1000 * correction_factor
                trend = calculate_trend(values) / 1000 * correction_factor if values else 0.0
                avg = average(values) / 1000 * correction_factor
                price = grid_prices.get(dt, 0.0)
                for key, value in (("ema", ema), ("trend", trend), ("average", avg)):
                    charging_plan[day]["hour_cost_prediction"][key][hour] = {
                        "percentage": kwh_to_percentage(value),
                        "kwh": value,
                        "price": price,
                        "cost": price * value,
                    }
                charging_plan[day]["discharge_timestamps"].append(start_of_day_datetime.replace(hour=hour))
                charging_plan[day]["battery_level_flow"][hour] = []
                charging_plan[day]["battery_level_flow_without_solar"][hour] = []

            for kind in ("ema", "trend", "average"):
                dict_sorted = sorted(
                    charging_plan[day]["hour_cost_prediction"][kind].items(),
                    key=lambda kv: (kv[1]["cost"], kv[0]),
                    reverse=True,
                )
                charging_plan[day]["sorted_hour_cost_prediction"][kind] = [s[0] for s in dict_sorted]

                grouped_dict: dict = {}
                last_hour = -2
                dict_key = 0
                for sorted_hour in charging_plan[day]["sorted_hour_cost_prediction"][kind]:
                    if sorted_hour != last_hour + 1:
                        dict_key = sorted_hour
                        grouped_dict[dict_key] = {"hours": [], "kwh": [], "price": [], "cost": [], "avg_price": 0.0, "total_cost": 0.0, "total_kwh": 0.0}
                    prediction = charging_plan[day]["hour_cost_prediction"][kind][sorted_hour]
                    grouped_dict[dict_key]["hours"].append(sorted_hour)
                    grouped_dict[dict_key]["kwh"].append(prediction["kwh"])
                    grouped_dict[dict_key]["price"].append(prediction["price"])
                    grouped_dict[dict_key]["cost"].append(prediction["cost"])
                    last_hour = sorted_hour
                for group in grouped_dict.values():
                    group["avg_price"] = average(group["price"])
                    group["total_cost"] = sum(group["cost"])
                    group["total_kwh"] = sum(group["kwh"])
                charging_plan[day]["grouped_hour_cost_prediction"][kind] = grouped_dict
                charging_plan[day]["grouped_sorted_hour_cost_prediction"][kind] = [
                    s[0] for s in sorted(grouped_dict.items(), key=lambda kv: (kv[1]["total_cost"], kv[0]), reverse=True)
                ]

            if self.enabled("solar_charging"):
                prediction = self.solar_prediction_db.get(day, {})
                charging_plan[day]["solar_kwh_prediction"] = prediction.get("total", [])
                charging_plan[day]["solar_cost_prediction"] = prediction.get("total_sell", [])

            solar_kwh_sum = sum(charging_plan[day]["solar_kwh_prediction"])
            max_charging_kwh = percentage_to_kwh(
                config["solar"]["powerwall_battery_level_max"] - config["solar"]["powerwall_battery_level_min"]
            )
            if solar_kwh_sum > max_charging_kwh:
                charging_plan[day]["solar_prediction_corrected"] = True
                solar_kwh_sum = min(solar_kwh_sum, max_charging_kwh)

            solar_percentage_sum = kwh_to_percentage(solar_kwh_sum) if charging_plan[day]["solar_kwh_prediction"] else 0.0
            solar_percentage_sum = min(
                solar_percentage_sum, config["solar"]["powerwall_battery_level_max"] - config["solar"]["powerwall_battery_level_min"]
            )

            sun_events = self.get_sun_events(charging_plan[day]["start_of_day"])
            sunrise_text = f"{emoji_parse({'sunrise': True})}{date_to_string(sun_events['sunrise'], '%H:%M')}"
            sunset_text = f"{emoji_parse({'sunset': True})}{date_to_string(sun_events['sunset'], '%H:%M')}"
            solar_over_production[day] = {
                "day": t(f"ui.calendar.weekday_names.{day_of_week_text(charging_plan[day]['start_of_day'])}"),
                "date": date_to_string(charging_plan[day]["start_of_day"], "%d/%m"),
                "when": f"{sunrise_text}-{sunset_text}",
                "emoji": emoji_parse({"solar": True}),
                "percentage": solar_percentage_sum,
                "kWh": solar_kwh_sum,
                "corrected": charging_plan[day]["solar_prediction_corrected"],
            }

            battery_level_flow_prediction(day, from_hour)

        sorted_by_cheapest_price = [
            (timestamp, price) for timestamp, price in sorted_by_cheapest_price if days_between(current_hour, timestamp) < amount_of_days
        ]

        total_cost, total_kwh = future_charging(total_cost, total_kwh)

        charge_hours["total_cost"] = total_cost
        charge_hours["total_kwh"] = total_kwh
        charge_hours["total_procent"] = round(kwh_to_percentage(total_kwh), 2)

        # ------------------------------------------------------------------ #
        # Publish attributes
        # ------------------------------------------------------------------ #
        battery_level_flow_sum = []
        for day in sorted(k for k in charging_plan if isinstance(k, int)):
            for hour in range(24):
                if hour not in charging_plan[day]["battery_level_flow"]:
                    continue
                timestamp = charging_plan[day]["start_of_day"] + datetime.timedelta(hours=hour)
                battery_level = round(sum(charging_plan[day]["battery_level_flow"][hour]), 1)
                battery_level_flow_sum.append({"hour": timestamp, "battery_level": battery_level})
        self.set_attr("powerwall_action", "battery_level_flow", battery_level_flow_sum)
        shadow = self.optimizer_shadow or {}
        self.set_attr("powerwall_action", "optimizer_shadow_summary", {key: value for key, value in shadow.items() if not key.startswith(("hours", "summary"))})

        # timestamps per action for automations (attributes on sensor.<prefix>_powerwall_action)
        actions = {
            "charge_timestamps": "charging_sessions",
            "discharge_timestamps": "discharge_timestamps",
            "force_discharge_timestamps": "force_discharge_timestamps",
            "blocked_solar_charge_timestamps": "blocked_solar_charge_timestamps",
            "force_powerwall_max_hours_old_discharge_timestamps": "force_powerwall_max_hours_old_discharge_timestamps",
        }
        timestamps: dict[str, set] = {attribute: set() for attribute in actions}
        planned_hours: list = []
        for day in sorted(k for k in charging_plan if isinstance(k, int)):
            for attribute, plan_key in actions.items():
                timestamps[attribute].update(charging_plan[day][plan_key])
            planned_hours += [charging_plan[day]["start_of_day"].replace(hour=hour) for hour in charging_plan[day]["battery_level_flow"]]
        with_action = set().union(*timestamps.values())
        timestamps["force_solar_only_charging_timestamps"] = {ts for ts in planned_hours if ts not in with_action}
        for attribute, values in timestamps.items():
            self.set_attr("powerwall_action", attribute, sorted(values))

        prediction_dicts = {key: {} for key in ("avg", "ema", "trend", "sum_avg", "sum_ema", "sum_trend")}
        for day in sorted(k for k in charging_plan if isinstance(k, int)):
            hour_cost_prediction = charging_plan[day]["hour_cost_prediction"]
            sums = {"avg": 0.0, "ema": 0.0, "trend": 0.0}
            for hour in hour_cost_prediction["average"].keys():
                timestamp = charging_plan[day]["start_of_day"] + datetime.timedelta(hours=hour)
                values = {
                    "avg": hour_cost_prediction["average"][hour]["kwh"],
                    "ema": hour_cost_prediction["ema"][hour]["kwh"],
                    "trend": hour_cost_prediction["trend"][hour]["kwh"],
                }
                for key, value in values.items():
                    sums[key] += value
                    prediction_dicts[key][timestamp] = round(value, 2)
                    prediction_dicts[f"sum_{key}"][timestamp] = round(sums[key], 2)
        for key, value in prediction_dicts.items():
            self.set_attr("powerwall_action", f"hour_cost_prediction_{key}", value)

        old_charge_hours = self.charge_hours
        self.plan = charging_plan
        self.charge_hours = charge_hours
        self._log_plan_summary(charging_plan, charge_hours)

        if old_charge_hours and check_next_24_hours_diff(old_charge_hours, charge_hours, now):
            self.append_overview_output("Charging plan changed, next 24 hours updated")

        overview = self._build_overview(charging_plan, charge_hours, battery_expenses, solar_over_production)
        overview_text = "\n".join(overview) if overview else self.t("ui.reason.no_data")
        self.last_overview = overview_text
        self.set_attr("overview", "overview", overview_text)

        self.last_plan_hash = inputs_hash
        return charge_hours

    def _log_plan_summary(self, charging_plan: dict, charge_hours: dict) -> None:
        """One INFO line per new plan + the details at DEBUG."""
        days = [day for day in charging_plan if isinstance(day, int)]
        charges = sorted(ts for ts in charge_hours if isinstance(ts, datetime.datetime))
        sales = sorted(ts for day in days for ts in charging_plan[day]["force_discharge_timestamps"])
        blocked = sum(len(charging_plan[day]["blocked_discharge_timestamps"]) for day in days)
        blocked_solar = sum(len(charging_plan[day]["blocked_solar_charge_timestamps"]) for day in days)
        _LOGGER.info(
            "New plan: %s charge hours (%.2f kWh, %.2f), %s sell periods, %s blocked discharge hours, %s blocked solar hours",
            len(charges), charge_hours.get("total_kwh", 0.0), charge_hours.get("total_cost", 0.0),
            len(sales), blocked, blocked_solar,
        )
        for ts in charges:
            rules = [key for key, value in charge_hours[ts].items() if value is True]
            _LOGGER.debug("  charge %s: %.2f kWh at %.2f/kWh (%s)", ts, charge_hours[ts]["kWh"], charge_hours[ts]["Price"], ", ".join(rules))
        for day in days:
            for ts, data in sorted(charging_plan[day]["force_discharge_timestamps"].items()):
                _LOGGER.debug("  sell %s: %.2f kWh, profit %.2f (%s)", ts, data["kwh"], data["profit"], data["title"])

    # ------------------------------------------------------------------ #
    # Overview markdown
    # ------------------------------------------------------------------ #

    def _plan_overview(self, overview: list[str], charging_plan: dict, days: list[int]) -> None:
        """📅 The charge/discharge plan per day (hours with the same rule joined) + the totals of the plan."""
        t = self.t
        valuta = t("ui.common.valuta")
        valuta_kwh = t("ui.common.valuta_kwh")
        try:
            overview.append("<center>\n")
            overview.append(f"## 📅 {t('ui.cheap_grid_charge_hours.plan_title')} ##")
            any_rows = False
            for day in days:
                data = charging_plan[day]
                joined = {}
                joined.update(data["force_powerwall_max_hours_old_discharge_timestamps"])
                joined.update(data["blocked_discharge_timestamps"])
                joined.update(data["blocked_solar_charge_timestamps"])
                joined.update(
                    _join_timestamps_by_hour(
                        data["force_discharge_timestamps"],
                        valuta,
                        t("ui.common.total"),
                        lambda timestamp: self.price_provider.sell_periods_in_hour(timestamp) if self.price_provider else 1,
                    )
                )
                joined.update(data["charging_sessions"])
                if not joined:
                    continue
                any_rows = True
                weekday = t(f"ui.calendar.weekday_names.{day_of_week_text(data['start_of_day'])}")
                overview.append(f"#### {weekday} {data['start_of_day'].strftime('%d/%m')}\n")
                overview.append(f"| {t('ui.common.time')} | {t('ui.common.reason')} |")
                overview.append("|:---|:---|")
                rows = [
                    (timestamp, value["reason"] if isinstance(value, dict) and "reason" in value else value)
                    for timestamp, value in sorted(joined.items(), key=lambda kv: kv[0])
                ]
                for group in _group_hour_rules(rows):
                    first, last = group[0][0], group[-1][0]
                    if len(group) == 1:
                        end = joined.get(first, {}).get("period_end") if isinstance(joined.get(first), dict) else None
                        time_text = f"{first.strftime('%H:%M')}-{end.strftime('%H:%M')}" if end else first.strftime("%H:%M")
                        overview.append(f"| **{time_text}** | {group[0][1]} |")
                    else:
                        until = reset_time_to_hour(last).replace(minute=59).strftime("%H:%M")  # the last hour included
                        overview.append(f"| **{first.strftime('%H:%M')}-{until}** | {_joined_reason(group)} |")
                overview.append("\n")
            if not any_rows:
                overview.append(f"**{t('ui.cheap_grid_charge_hours.no_upcoming_charging_planned')}**\n")
            overview.extend(self._plan_summary_lines(valuta, valuta_kwh))

            # all prices offline, or hours the provider has no price for, filled from the price history
            if self.grid_price_cache.get("missing_hours"):
                n_pairs = 4
                overview.append(f"\n\n<details><summary><b>{t('ui.cheap_grid_charge_hours.offline_prices')}!!!</b></summary>\n")
                by_day = defaultdict(list)
                for ts, price in sorted(self.grid_price_cache["missing_hours"].items()):
                    by_day[ts.date()].append((ts.strftime("%H:%M"), f"{price:.2f}{valuta}"))
                for day_date in sorted(by_day):
                    rows = by_day[day_date]
                    overview.append(f"\n<b>{day_date.strftime('%d/%m')}</b>\n")
                    heads = [t("ui.common.time"), t("ui.common.price")] * n_pairs
                    overview.append("| " + " | ".join(heads) + " |\n| " + " | ".join([":---:", "---:"] * n_pairs) + " |")
                    for i in range(0, len(rows), n_pairs):
                        slice_pairs = rows[i:i + n_pairs]
                        while len(slice_pairs) < n_pairs:
                            slice_pairs.append(("&nbsp;", "&nbsp;"))
                        cells = []
                        for time_text, price_text in slice_pairs:
                            cells += [f"**{time_text}**", f"*{price_text}*"]
                        overview.append("| " + " | ".join(cells) + " |")
                    overview.append("")
                overview.append("\n</details>\n\n")
            overview.append("</center>\n")
            overview.append("***")
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Failed to create the charge/discharge plan overview: %s", err)

    def _plan_summary_lines(self, valuta: str, valuta_kwh: str) -> list[str]:
        """The bill of the period without and with the battery (the plan that controls it), item by item."""
        shadow = self.optimizer_shadow or {}
        controls = shadow.get("controls", False)
        summary = shadow.get("summary_optimal" if controls else "summary_rules")
        if not summary:
            return []
        t = self.t
        prefix = "ui.cheap_grid_charge_hours.plan_summary"
        without, with_battery, profit = summary["without"], summary["with"], summary["profit"]

        def money(value: float, kwh: float | None = None, note: str = "") -> str:
            text = f"{value:.2f} {valuta}"
            if kwh is not None:
                text += f" ({kwh:.1f} kWh)"
            return f"{text}<br>({note})" if note else text

        def row(label: str, without_value, with_value, bold: bool = False, difference: float | None = None) -> str:
            """(value, kWh, note) per column, None = not there (–); the difference is with minus without."""
            if difference is None:
                difference = (with_value[0] if with_value else 0.0) - (without_value[0] if without_value else 0.0)
            cells = [
                money(*without_value) if without_value else "–",
                money(*with_value) if with_value else "–",
                f"{difference:+.2f}",
            ]
            if bold:
                label, cells = f"**{label}**", [f"**{cell}**" for cell in cells]
            return f"| {label} | {' | '.join(cells)} |"

        start, end = summary["start"], summary["end"]
        days = ((end - start).total_seconds() / 3600 + 1) / 24 if start and end else 0.0
        period = (
            f"{start.strftime('%d/%m %H:%M')} - {end.replace(minute=59).strftime('%d/%m %H:%M')} ({days:.1f} {t(f'{prefix}.days')})"
            if start and end else "-"
        )
        energy_without = (without["energy_value"], None, t(f"{prefix}.kept")) if abs(without["energy_value"]) >= 0.005 else None
        energy_with = (with_battery["energy_value"], None, t(f"{prefix}.left")) if abs(with_battery["energy_value"]) >= 0.005 else None
        return [
            f"\n<details><summary><b>💰 {t(f'{prefix}.total')}: {profit:+.2f} {valuta} ({summary['profit_per_kwh']:.2f} {valuta_kwh})</b><br>"
            f"📈 {summary['charged_grid_kwh']:.1f} kWh · 📤 {with_battery['sold_kwh']:.1f} kWh · ☀️ {summary['charged_solar_kwh']:.1f} kWh</summary>\n",
            f"|  | {t(f'{prefix}.without')} | {t(f'{prefix}.with')} | {t(f'{prefix}.difference')} |",
            "|:---|---:|---:|---:|",
            row(f"💸 {t(f'{prefix}.paid')}", (without["paid"],), (with_battery["paid"],), bold=True),
            row(f"↳ 🏠 {t(f'{prefix}.to_house')}", (without["house_cost"], without["house_kwh"]), (with_battery["house_cost"], with_battery["house_kwh"])),
            row(f"↳ 📈 {t(f'{prefix}.to_battery')}", None, (with_battery["battery_cost"], with_battery["battery_kwh"])),
            row(f"💵 {t(f'{prefix}.earned')}", (without["earned"],), (with_battery["earned"],), bold=True),
            row(f"↳ ☀️ {t(f'{prefix}.solar_surplus')}", (without["solar_income"], without["solar_kwh"]), (with_battery["solar_income"], with_battery["solar_kwh"])),
            row(f"↳ 📤 {t(f'{prefix}.from_battery')}", None, (with_battery["sold_income"], with_battery["sold_kwh"])),
            row(f"🛠️ {t(f'{prefix}.wear')}", None, (with_battery["wear"],)),
            row(f"🔋 {t(f'{prefix}.energy')}", energy_without, energy_with),
            row(
                f"= {t(f'{prefix}.bill')}",
                (without["bill"],),
                (with_battery["bill"],),
                bold=True,
                difference=-profit,  # the same as the title (not rounded twice)
            ),
            "",
            "|  |  |",
            "|:---|---:|",
            f"| **🔋 {t(f'{prefix}.delivers')}** | {summary['delivered_house_kwh'] + with_battery['sold_kwh']:.1f} kWh "
            f"({summary['delivered_house_kwh']:.1f} {t(f'{prefix}.to_house_short')}, {with_battery['sold_kwh']:.1f} {t(f'{prefix}.sold_short')}) |",
            f"| **🔋 {t(f'{prefix}.charged')}** | {summary['charged_grid_kwh'] + summary['charged_solar_kwh']:.1f} kWh "
            f"({summary['charged_grid_kwh']:.1f} {t(f'{prefix}.from_grid')}, {summary['charged_solar_kwh']:.1f} {t(f'{prefix}.from_solar')}) |",
            f"| **🔋 {t(f'{prefix}.end_level')}** | {summary['end_level']:.0f}% |",
            f"| **📆 {t(f'{prefix}.period')}** | {period} |",
            f"| **🧠 {t(f'{prefix}.controlled_by')}** | {t(f'{prefix}.optimizer') if controls else t(f'{prefix}.rules')} |",
            f"\n*{t(f'{prefix}.note')}*",
            "</details>\n",
        ]

    def _build_overview(self, charging_plan, charge_hours, battery_expenses, solar_over_production) -> list[str]:  # noqa: C901
        t = self.t
        valuta = t("ui.common.valuta")
        valuta_kwh = t("ui.common.valuta_kwh")
        overview: list[str] = []
        days = sorted(k for k in charging_plan if isinstance(k, int))

        try:
            percentage = round(battery_expenses.get("percentage", 0.0), 0)
            solar_percentage = round(battery_expenses.get("solar_percentage", 0.0), 0)
            kwh = round(battery_expenses.get("kwh", 0.0), 1)
            solar_kwh = round(self.percentage_to_kwh(battery_expenses.get("solar_percentage", 0.0)), 1)
            cost_loss = round(battery_expenses.get("cost_loss", 0.0), 2)
            cost_wear = round(battery_expenses.get("cost_wear", 0.0), 2)
            cost = round(battery_expenses.get("cost", 0.0), 2)
            unit = round(battery_expenses.get("unit") or 0.0, 2)
            unit_percentage = round(self.kwh_to_percentage(self.percentage_to_kwh(battery_expenses.get("unit") or 0.0)), 2)
            cost_with_loss = round(battery_expenses.get("cost_with_loss_and_wear", 0.0), 2)
            unit_with_loss = round(battery_expenses.get("unit_with_loss_and_wear") or 0.0, 2)
            unit_percentage_with_loss = round(
                self.kwh_to_percentage(self.percentage_to_kwh(battery_expenses.get("unit_with_loss_and_wear") or 0.0)), 2
            )
            unit_kwh_with_loss_text = f"**{unit_with_loss:.2f} {valuta_kwh}**"
            unit_pct_with_loss_text = (
                f"<br>**{unit_percentage_with_loss:.2f} {t('ui.common.valuta_percentage')}**" if unit_with_loss != unit_percentage_with_loss else ""
            )
            unit_kwh_text = f"<br>(**{unit:.2f} {valuta_kwh}**)"
            unit_pct_text = f"<br>(**{unit_percentage:.2f} {t('ui.common.valuta_percentage')}**)" if unit != unit_percentage else ""
            cost_loss_kwh = round(cost_loss / kwh, 2) if kwh else 0.0
            cost_wear_kwh = round(cost_wear / kwh, 2) if kwh else 0.0
            loss_percentage = battery_expenses.get("loss_percentage", 0.0) * 100

            if kwh > 0.0:
                prefix = "ui.cheap_grid_charge_hours.battery_level_expenses"
                overview.append("<center>\n")
                overview.append(f"## 🔎 {t(f'{prefix}.title')} ##")
                overview.append("|  |  |")
                overview.append("|:---|---:|")
                overview.append(f"| **🔋 {t(f'{prefix}.current_battery_level')}** | **{percentage:.0f}% {kwh:.1f} kWh** |")
                if solar_percentage > 0.0:
                    overview.append(f"| **☀️ {t(f'{prefix}.solar_share')}** | **{solar_percentage:.0f}% {solar_kwh:.1f} kWh** |")
                overview.append(f"| **💰 {t(f'{prefix}.expense')}** | **{cost_with_loss:.2f} {valuta}**<br>(**{cost:.2f} {valuta}**) |")
                overview.append(
                    f"| **📊 {t(f'{prefix}.estimated_loss')}**<br>({t(f'{prefix}.charging_loss')} {loss_percentage:.1f}%) | "
                    f"**{cost_loss:.2f} {valuta}**<br>(**{cost_loss_kwh:.2f} {valuta_kwh}**) |"
                )
                overview.append(
                    f"| **🛠️ {t(f'{prefix}.estimated_wear_cost')}** | **{cost_wear:.2f} {valuta}**<br>(**{cost_wear_kwh:.2f} {valuta_kwh}**) |"
                )
                overview.append(
                    f"| **🧮 {t(f'{prefix}.unit_price')}** | {unit_kwh_with_loss_text}{unit_pct_with_loss_text}{unit_kwh_text}{unit_pct_text} |"
                )
                overview.append("</center>\n")
                overview.append("***")
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to calculate battery level cost: %s", err)

        self._plan_overview(overview, charging_plan, days)

        try:
            if self.is_solar_configured():
                overview.append("<center>\n")
                if solar_over_production:
                    overview.append(f"## 🌞 {t('ui.cheap_grid_charge_hours.solar_production_title')} ##")
                    overview.append(f"| {t('ui.common.time')} |  |  |  | % |  | kWh |")
                    overview.append("|---|---:|:---|---:|---:|---|---:|")
                    for d in solar_over_production.values():
                        day_text = f"**{d['day']}**" if d["day"] else ""
                        date_text = f"**{d['date']}**" if d["date"] else ""
                        when_text = f"**{d['when']}**" if d["when"] else ""
                        emoji = emoji_parse({"solar_corrected": True}) if d["corrected"] else (f"**{emoji_text_format(d['emoji'])}**" if d["emoji"] else "")
                        pct = f"**{round(d['percentage'], 1)}**" if d["percentage"] else "**0**"
                        kwh_text = f"**{round(d['kWh'], 1)}**" if d["kWh"] else "**0.0**"
                        overview.append(f"| {day_text} | {date_text} | {when_text} | {emoji} | {pct} |  | {kwh_text} |")
                else:
                    overview.append(f"### 🌞 {t('ui.cheap_grid_charge_hours.no_solar_production')} ##")
                overview.append("</center>\n")
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to create solar over production overview: %s", err)

        '''
        color = {"ema": "red", "trend": "yellow", "average": "green"}
        label = {"ema": "EMA", "trend": "Trend", "average": "Avg"}
        legend = "### 🔎 Prediction type <font color=red>EMA</font>/<font color=yellow>Trend</font>/<font color=green>Avg</font> ###"

        try:
            overview.append("<center>\n")
            overview.append("## 🔎 Group sorted Hour cost prediction ##")
            overview.append(legend)
            for day in days:
                overview.append("<details>\n")
                overview.append(f"<summary>🔎 Day {day}</summary>\n")
                overview.append("|  |  |")
                overview.append("|:---|---:|")
                for key in ("ema", "trend", "average"):
                    hours = ", ".join(f"{h:02d}" for h in charging_plan[day]["grouped_sorted_hour_cost_prediction"][key])
                    overview.append(f"| <font color={color[key]}>{label[key]}</font> | <font color={color[key]}>{hours}</font> |")
                overview.append("\n</details>\n")
            overview.append("</center>\n")
            overview.append("***")
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Group sorted Hour cost prediction failed: %s", err)

        try:
            overview.append("<center>\n")
            overview.append("## 🔎 Group hour cost prediction ##")
            overview.append(legend)
            for day in days:
                overview.append("<details>\n")
                overview.append(f"<summary>🔎 Day {day} (discharge kwh: <b>{charging_plan[day]['discharge_kwh']:.1f}kWh</b>)</summary>\n")
                overview.append("| First | Hours | kwh<br>needed | price | cost |")
                overview.append("|:---:|:---:|:---:|:---:|:---:|")
                for key, col in ((self.forecast_type, "red"), ("trend", "yellow"), ("average", "green")):
                    for hour in charging_plan[day]["grouped_sorted_hour_cost_prediction"][key]:
                        group = charging_plan[day]["grouped_hour_cost_prediction"][key][hour]
                        hours_string = f"<font color={col}>" + ", ".join(f"{h:02d}" for h in group["hours"]) + "</font>"
                        kwh_string = f"<font color={col}>{group['total_kwh']:.1f}</font>" if group["total_kwh"] > 0.0 else ""
                        overview.append(
                            f"| **<font color={col}>{hour:02d}</font>** | {hours_string} | {kwh_string} | {group['avg_price']:.2f} | "
                            f"<font color={col}>{group['total_cost']:.2f}</font> |"
                        )
                overview.append("</details>\n")
            overview.append("</center>\n")
            overview.append("***")
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Group hour cost prediction failed: %s", err)

        try:
            overview.append("<center>\n")
            overview.append("## 🔎 Sorted Hour cost prediction ##")
            overview.append(legend)
            for day in days:
                overview.append("<details>\n")
                overview.append(f"<summary>🔎 Day {day}</summary>\n")
                overview.append("|  |  |")
                overview.append("|:---|---:|")
                for key in ("ema", "trend", "average"):
                    hours = ", ".join(f"{h:02d}" for h in charging_plan[day]["sorted_hour_cost_prediction"][key])
                    overview.append(f"| <font color={color[key]}>{label[key]}</font> | <font color={color[key]}>{hours}</font> |")
                overview.append("\n</details>\n")
            overview.append("</center>\n")
            overview.append("***")
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Sorted Hour cost prediction failed: %s", err)

        try:
            overview.append("<center>\n")
            overview.append("## 🔎 Hour cost prediction ##")
            overview.append(legend)
            last_battery_level = self.get_battery_level()
            for day in days:
                overview.append("<details>\n")
                overview.append(f"<summary>🔎 Day {day} (discharge kwh: **{charging_plan[day]['discharge_kwh']:.1f}kWh**)</summary>\n")
                overview.append("| Hour | kwh<br>needed | price | cost | 🔋 |")
                overview.append("|:---:|:---:|:---:|:---:|:---:|")
                prediction = charging_plan[day]["hour_cost_prediction"]
                for hour in prediction["ema"].keys():
                    ema = prediction["ema"][hour]
                    trend = prediction["trend"][hour]
                    avg = prediction["average"][hour]
                    battery_level = round(sum(charging_plan[day]["battery_level_flow"].get(hour, [0.0])), 1)
                    kwh_string = (
                        f"<font color=red>{ema['kwh']:.1f}</font> <br> <font color=yellow>{trend['kwh']:.1f}</font> / "
                        f"<font color=green>{avg['kwh']:.1f}</font>"
                        if ema["kwh"] > 0.0 or trend["kwh"] > 0.0 or avg["kwh"] > 0.0 else ""
                    )
                    cost_string = (
                        f"<font color=red>{ema['cost']:.2f}</font> <br> <font color=yellow>{trend['cost']:.2f}</font> / "
                        f"<font color=green>{avg['cost']:.2f}</font>"
                    )
                    battery_level_string = f"<font color=yellow>{battery_level:.1f}%</font>"
                    if battery_level < last_battery_level:
                        battery_level_string = f"<font color=red>{battery_level:.1f}%</font> 📉"
                    elif battery_level > last_battery_level:
                        battery_level_string = f"{emoji_parse({'charging': True})}<font color=green>{battery_level:.1f}%</font>"
                    last_battery_level = battery_level
                    overview.append(f"| **{hour:02d}** | {kwh_string} | {ema['price']:.2f} | {cost_string} | **{battery_level_string}** |")
                overview.append("</details>\n")
            overview.append("</center>\n")
            overview.append("***")
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Hour cost prediction failed: %s", err)
        '''
        
        if overview:
            overview.append("<center>\n")
            overview.append(f"##### {t('ui.cheap_grid_charge_hours.last_scheduled')} {self.now()} #####")
            overview.append("</center>\n")
        return overview


# ---------------------------------------------------------------------- #
# Hour rules overview: consecutive hours with the same rule in one row
# ---------------------------------------------------------------------- #

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_TIME = re.compile(r"\d{2}:\d{2}")
_OTHER_DAY = re.compile(r"<br><center>.*?</center>", re.S)


def _summary(reason: str) -> tuple[str, list[str], str, str] | None:
    """(title without the values, the values in (), other day text, body) of a <details> reason."""
    match = re.search(r"<summary>(.*?)</summary>(.*)</details>\s*$", str(reason), re.S)
    if not match:
        return None
    summary, body = match.groups()
    other_day = "".join(_OTHER_DAY.findall(summary))
    summary = _OTHER_DAY.sub("", summary)
    values = re.findall(r"\(([^()]*)\)", summary)
    title = re.sub(r"\s+", " ", re.sub(r"\([^()]*\)|<br>", " ", summary)).strip()
    return title, values, other_day, body


def _group_hour_rules(rows: list[tuple[datetime.datetime, str]]) -> list[list[tuple[datetime.datetime, str]]]:
    """Consecutive hours with the same rule (title of the reason) grouped."""
    groups: list[list[tuple[datetime.datetime, str]]] = []
    for timestamp, reason in rows:
        parts = _summary(reason)
        if groups and parts:
            last_timestamp, last_reason = groups[-1][-1]
            last_parts = _summary(last_reason)
            consecutive = reset_time_to_hour(timestamp) - reset_time_to_hour(last_timestamp) == datetime.timedelta(hours=1)
            if consecutive and last_parts and last_parts[0] == parts[0] and last_parts[2] == parts[2]:
                groups[-1].append((timestamp, reason))
                continue
        groups.append([(timestamp, reason)])
    return groups


def _average_values(values: list[list[str]]) -> list[str]:
    """Average every number of the () values when all hours have the same layout, else the first hour's."""
    templates = {tuple(_NUMBER.sub("#", value) for value in hour) for hour in values}
    if len(templates) != 1:
        return values[0]
    averaged = []
    for position, text in enumerate(values[0]):
        column = [hour[position] for hour in values]
        if _TIME.search(text):
            # clock times are not averaged: the same time, else first-last
            averaged.append(text if len(set(column)) == 1 else f"{column[0]}-{column[-1]}")
            continue
        numbers = [[float(n) for n in _NUMBER.findall(hour[position])] for hour in values]
        decimals = [len(n.split(".")[1]) if "." in n else 0 for n in _NUMBER.findall(text)]
        means = iter(f"{sum(column) / len(column):.{decimal}f}" for column, decimal in zip(zip(*numbers), decimals))
        averaged.append(_NUMBER.sub(lambda _match: next(means), text))
    return averaged


def _joined_reason(group: list[tuple[datetime.datetime, str]]) -> str:
    """One reason for the hours of a group: the title with the average values, each hour inside."""
    parts = [_summary(reason) for _, reason in group]
    title, _, other_day, _ = parts[0]
    values = "".join(f" ({value})" for value in _average_values([part[1] for part in parts]))
    hours = "".join(
        f"<details><summary>{timestamp.strftime('%H:%M')}{''.join(f' ({value})' for value in part[1])}</summary>{part[3]}</details>"
        for (timestamp, _), part in zip(group, parts)
    )
    return f"<details><summary>{emoji_parse({'bundle': True})}{title}{values}{other_day}</summary>{hours}</details>"


def _join_timestamps_by_hour(timestamps: dict, valuta: str, total_label: str, periods_in_hour=None) -> dict:
    """Sell periods of the same hour and title in one reason (📦)."""
    grouped: dict = {}
    for timestamp, data in timestamps.items():
        timestamp_dt = datetime.datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else timestamp
        timestamp_dt = timestamp_dt.replace(tzinfo=None)
        title = data.get("title", "")
        grouped.setdefault((timestamp_dt.replace(minute=0, second=0, microsecond=0), title), []).append({
            "timestamp": timestamp_dt,
            "title": title,
            "emoji": data.get("emoji", ""),
            "kwh": data.get("kwh", 0.0),
            "profit": data.get("profit", 0.0),
            "reason": data.get("reason", ""),
        })
    joined = {}
    for periods in grouped.values():
        periods = sorted(periods, key=lambda p: p["timestamp"])
        first = periods[0]
        period_count = max(int(periods_in_hour(first["timestamp"])) if periods_in_hour else 1, 1)
        period_end = periods[-1]["timestamp"] + datetime.timedelta(minutes=60 / period_count)
        if len(periods) == 1:
            joined[first["timestamp"]] = {
                **{k: first[k] for k in ("title", "emoji", "kwh", "profit", "reason")},
                "period_end": period_end,
            }
            continue
        total_kwh = 0.0
        total_profit = 0.0
        period_details = []
        for period in periods:
            total_kwh += period["kwh"]
            total_profit += period["profit"]
            detail = period["reason"]
            summary_end = detail.find("</summary>")
            if summary_end != -1:
                detail = detail[summary_end + len("</summary>"):]
            if detail.endswith("</details>"):
                detail = detail[: -len("</details>")]
            period_details.append(
                f"<details><summary>{first['emoji']}**{period['timestamp'].strftime('%H:%M')}:** "
                f"{period['kwh']:.2f} kWh / {period['profit']:.2f} {valuta}</summary>{detail}</details>"
            )
        joined[first["timestamp"]] = {
            "title": first["title"],
            "kwh": total_kwh,
            "profit": total_profit,
            "period_end": period_end,
            "reason": (
                f"<details><summary>{emoji_parse({'bundle': True})}{first['emoji']}{first['title']}<br>"
                f"({total_kwh:.1f} kWh / {total_profit:.2f} {valuta})</summary>{''.join(period_details)}"
                f"<br>**{total_label}:** {total_kwh:.2f} kWh / {total_profit:.2f} {valuta}</details>"
            ),
        }
    return joined


def _running_battery_price(start_kwh: float, start_price: float, steps: list[tuple[float, float, float, float]]) -> float:
    """kWh price of the energy in the battery after the steps (weighted average cost, like a stock).

    steps: (change of the content in kWh, grid kWh charged, grid price, solar price) per hour. What comes
    in costs the grid price (the grid part) or the solar price (the rest); what goes out leaves at the
    average price, so the price of what is left doesn't change.
    """
    kwh, cost = start_kwh, start_kwh * start_price
    for change, grid_kwh, grid_price, solar_price in steps:
        if change < 0.0:
            removed = min(-change, kwh)
            if kwh > 0.0:
                cost -= cost / kwh * removed
            kwh -= removed
        elif change > 0.0:
            from_grid = min(grid_kwh, change)
            cost += from_grid * grid_price + (change - from_grid) * solar_price
            kwh += change
    return cost / kwh if kwh > 0.0 else start_price
