"""The shadow plan and the optimizer plan."""
from __future__ import annotations

import datetime
import logging
import math
from collections import defaultdict

from ...optimizer import (
    OptimizerConfig,
    OptimizerHour,
    breakdown,
    optimize_per_day,
    optimize_with_rules,
    simulate,
)
from ....utils.formatting import emoji_parse
from ....utils.helpers import average, days_between, reset_time_to_hour

_LOGGER = logging.getLogger(__name__)


class FutureOptimizer:
    """Runs the optimizer next to the rules and applies its plan."""

    def shadow_plan(self, ctx, days):
        """Whole horizon optimisation next to the rules (compared in the overview).

        Returns (hours, optimal result, explanations) for apply_optimizer_plan(), None without prices.
        """
        buy_by_hour = defaultdict(list)
        for timestamp, price in ctx.grid_prices.items():
            buy_by_hour[reset_time_to_hour(timestamp)].append(price)
        # the planned kWh per charge hour - charge_if_needed() stops when they are in the battery
        charge_hour_kwh = defaultdict(float)
        for ts, session in ctx.charge_hours.items():
            if isinstance(ts, datetime.datetime):
                charge_hour_kwh[reset_time_to_hour(ts)] += session["kWh"]
        exclude_hours = self.get_exclude_sell_hours()
        hours, rule_actions, rule_limits = [], [], []
        for day in days:
            data = ctx.charging_plan[day]
            # the planned kWh per sell hour - force discharge only runs in the planned sell periods
            force_hours = defaultdict(float)
            for ts, sale in data["force_discharge_timestamps"].items():
                force_hours[reset_time_to_hour(ts)] += sale["kwh"]
            blocked_solar_hours = {ts.hour for ts in data["blocked_solar_charge_timestamps"]}
            for hour in sorted(data["battery_level_flow"]):
                timestamp = data["start_of_day"].replace(hour=hour)
                prediction = data["hour_cost_prediction"][ctx.forecast_type].get(hour)
                if prediction is None or not buy_by_hour.get(timestamp):
                    continue
                solar = self.solar_surplus_in_hour(ctx.charging_plan, ctx.current_hour, timestamp)
                # the current hour only has the minutes left (the solar prediction is already scaled)
                fraction = max(1.0 - ctx.now.minute / 60.0, 0.05) if timestamp == ctx.current_hour else 1.0
                hours.append(OptimizerHour(
                    timestamp, average(buy_by_hour[timestamp]), self.sell_price_in_hour(ctx, timestamp),
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
        wear = abs(ctx.config["solar"]["powerwall_wear_cost_per_kwh"])
        optimizer_config = OptimizerConfig(
            battery_size=ctx.config["solar"]["powerwall_battery_size"],
            level_min=ctx.config["solar"]["powerwall_battery_level_min"],
            level_max=ctx.config["solar"]["powerwall_battery_level_max"],
            max_charge=ctx.max_kwh_charging,
            max_discharge=abs(ctx.max_kwh_discharging),
            efficiency=ctx.efficiency,
            wear=wear,
            charge_efficiency_curve=self.charging_efficiency_curve(),
            discharge_efficiency_curve=self.discharging_efficiency_curve(),
            curve_fallback_efficiency=self._fallback_efficiency(),
            min_profit_per_kwh=ctx.get_min_profit_per_kwh(),
            min_sell_kwh=self.number("min_sell_kwh"),
            min_total_profit=self.get_min_total_profit(),
            usage_reserve=self.number("usage_reserve"),
        )
        # a kWh left in the battery at the end saves the average buy price later (after wear and
        # the min. profit, else buying just to hold the energy at the end would pay)
        end_value = ctx.efficiency * max(average([hour.buy for hour in hours]) - wear - ctx.get_min_profit_per_kwh(), 0.0)
        start_level = ctx.get_battery_level()
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

    def apply_optimizer_plan(self, ctx, days, hours, optimal, explanations):
        """The optimiser controls the battery: its action per hour replaces the rule based plan.

        Every action is explained as the rule it corresponds to (same titles/emojis as the
        rules) with the hour it is for. Everything is built first and swapped in at the end,
        so a failure leaves the rule plan. Partial charge hours stop at their kWh (replanned
        every cron interval), and a sale uses the most expensive sell periods of the hour.
        """
        charge_titles = {
            "cheapest_hour_fill_planner": ctx.r("title.cheapest_hours"),
            "most_expensive_planner": ctx.r("title.most_expensive_hours"),
            "needed_before_max_level_planner": ctx.r("title.needed"),
            "negative_price_planner": ctx.r("title.negative_price"),
            "grid_charge_to_sell": ctx.r("title.buy_to_sell"),
        }
        sale_titles = {
            "grid_charge_to_sell": ctx.r("title.sell_bought_cheap"),
            "discharge_before_solar": ctx.r("title.sell_before_solar"),
            "most_profit": ctx.r("title.sell_instead_of_use"),
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
            plan_day = new_plan[days_between(ctx.current_hour, timestamp)]
            rule = explanation["rule"] if explanation else None
            target = explanation["target"] if explanation else None
            level_text = f"{ctx.r('level_after_hour')}: **{level:.0f}%**<br>"
            if action == "grid_charging" and grid_kwh >= 0.1:  # a few Wh is not worth a charge hour
                percentage = ctx.kwh_to_percentage(grid_kwh) * ctx.efficiency
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
                        f"{ctx.r('sold_at', time=when(target), price=f'{hours[target].sell:.2f} {ctx.valuta_kwh}')}<br>"
                        f"{ctx.r('profit_per_kwh')}: **{self.charge_kwh_profit(ctx, hour.buy, hours[target].sell):.2f} {ctx.valuta_kwh}**<br>"
                    )
                else:
                    target_text = (
                        f"{ctx.r('used_at', time=when(target), price=f'{hours[target].buy:.2f} {ctx.valuta_kwh}')}<br>"
                        f"{ctx.r('profit_per_kwh')}: **{self.charge_kwh_profit(ctx, hour.buy, hours[target].buy):.2f} {ctx.valuta_kwh}**<br>"
                    )
                new_charge_hours[timestamp] = session
                plan_day["charging_sessions"][timestamp] = {
                    **session,
                    "reason": (
                        f"<details><summary>{emoji_parse({rule: True})}{charge_titles[rule]} ({hour.buy:.2f}/{percentage:.0f}%)</summary>"
                        f"{ctx.r('charging_at', kwh=f'{grid_kwh:.2f} kWh', price=f'{hour.buy:.2f} {ctx.valuta_kwh}')}<br>"
                        f"{target_text}{level_text}"
                        "</details>"
                    ),
                }
                new_total_cost += grid_kwh * hour.buy
                new_total_kwh += grid_kwh
            elif action == "force_discharge" and sold_kwh > 0.01:
                title = sale_titles[rule]
                if rule == "grid_charge_to_sell" and explanation.get("kind") == "recharged":
                    title = ctx.r("title.sell_before_cheap_charge")
                    detail = f"{ctx.r('recharged_at', time=when(target), price=f'{hours[target].buy:.2f} {ctx.valuta_kwh}')}<br>"
                elif rule == "grid_charge_to_sell" and target is not None:
                    detail = f"{ctx.r('bought_at', time=when(target), price=f'{hours[target].buy:.2f} {ctx.valuta_kwh}')}<br>"
                elif rule == "discharge_before_solar" and target is not None:
                    detail = f"{ctx.r('solar_refills_at', time=when(target), price=f'{hours[target].sell:.2f} {ctx.valuta_kwh}')}<br>"
                else:
                    detail = f"{ctx.r('worth_more_sold_now')}<br>"
                emoji = emoji_parse({rule: True})
                periods, _, _ = self.sell_prices_for_window(ctx, timestamp, timestamp + datetime.timedelta(hours=1))
                periods = periods or {timestamp: hour.sell}
                period_kwh = abs(ctx.max_kwh_discharging) / len(periods)
                count = min(len(periods), max(1, math.ceil(sold_kwh / period_kwh - 0.01)))
                for period, price in sorted(periods.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)[:count]:
                    plan_day["force_discharge_timestamps"][period] = {
                        "title": title,
                        "emoji": emoji,
                        "kwh": sold_kwh / count,
                        "profit": sold_kwh / count * price,
                        "reason": (
                            f"<details><summary>{emoji}{title}<br>({sold_kwh / count:.1f} kWh / {price:.2f} {ctx.valuta_kwh})</summary>"
                            f"{ctx.r('sell_price')}: **{price:.2f} {ctx.valuta_kwh}**<br>{detail}{level_text}"
                            "</details>"
                        ),
                    }
                # the battery also covers the house in the rest of the hour
                plan_day["discharge_timestamps"].append(timestamp)
            elif action == "discharge_allowed":
                plan_day["discharge_timestamps"].append(timestamp)
            elif action == "blocked_solar_charge":
                later = f"{ctx.r('charged_instead_at', time=when(target))}<br>" if target is not None else ""
                charged_at = f" ({hours[target].timestamp.strftime('%H:%M')})" if target is not None else ""
                plan_day["blocked_solar_charge_timestamps"][timestamp] = {
                    "reason": (
                        f"<details><summary>{emoji_parse({'blocked_solar_charge': True})}{ctx.r('title.blocked_solar')}{charged_at}</summary>"
                        f"{ctx.r('solar_sold_for')}: **{hour.sell:.2f} {ctx.valuta_kwh}**<br>{later}{level_text}"
                        "</details>"
                    )
                }
            elif rule == "prioritize_discharge_hours_by_energy_cost":
                later = (
                    f"{ctx.r('used_instead_at', time=when(target), price=f'{hours[target].buy:.2f} {ctx.valuta_kwh}')}<br>"
                    if target is not None else f"{ctx.r('worth_more_later')}<br>"
                )
                title = ctx.r("title.most_expensive_prioritized") + (f" ({hours[target].timestamp.strftime('%H:%M')})" if target is not None else "")
                plan_day["blocked_discharge_timestamps"][timestamp] = (
                    f"<details><summary>{emoji_parse({'prioritize_discharge_hours_by_energy_cost': True})}{title}</summary>"
                    f"{ctx.r('price_now')}: **{hour.buy:.2f} {ctx.valuta_kwh}**<br>{later}{level_text}"
                    "</details>"
                )

        ctx.charge_hours.clear()
        ctx.charge_hours.update(new_charge_hours)
        for day in days:
            ctx.charging_plan[day].update(new_plan[day])
            ctx.charging_plan[day]["total_cost"] = sum(
                session["Cost"] for ts, session in new_charge_hours.items() if days_between(ctx.current_hour, ts) == day
            )
        return round(new_total_cost, 2), round(new_total_kwh, 2)
