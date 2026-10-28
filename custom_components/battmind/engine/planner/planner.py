"""Charging planner (engine part) - port of cheap_grid_charge_hours()."""
from __future__ import annotations

import datetime
import logging

from ...utils.formatting import emoji_parse, get_list_values
from ...utils.helpers import (
    average,
    calculate_ema,
    calculate_trend,
    check_next_24_hours_diff,
    date_to_string,
    day_of_week_text,
    days_between,
    end_of_day,
    reset_time_to_hour,
    reverse_list,
    start_of_day,
)
from .charging import ChargingHours
from .charging_flow import ChargingFlow
from .overview import Overview
from .what_if import WhatIf
from .future import FutureCharging

_LOGGER = logging.getLogger(__name__)
FUNC_NAME = "cheap_grid_charge_hours"


class Planner(WhatIf, Overview, ChargingHours, ChargingFlow, FutureCharging):
    """The charging / discharging planner."""

    def consumption_forecast_values(self, timestamp: datetime.datetime) -> list[float]:
        """W of the hour of the day (oldest first) the consumption forecast is made from."""
        return reverse_list(get_list_values(
            self.power_values_db.get(timestamp.hour, {}).get("power_consumption_without_all_exclusion", [0.0])
        ))

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
                values = self.consumption_forecast_values(dt)
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

            self.battery_level_flow_prediction(charging_plan, config, efficiency, forecast_type, get_battery_level, kwh_to_percentage, max_kwh_charging, max_kwh_discharging, now, day, from_hour)

        sorted_by_cheapest_price = [
            (timestamp, price) for timestamp, price in sorted_by_cheapest_price if days_between(current_hour, timestamp) < amount_of_days
        ]

        total_cost, total_kwh = self.future_charging(amount_of_days, battery_expenses, battery_expenses_keys, calc_battery_loss_cost, charge_hours, charging_plan, config, current_hour, current_sell_price_timestamp, efficiency, forecast_type, get_battery_level, get_min_profit_per_kwh, grid_prices, grid_sell_prices, kwh_to_percentage, max_kwh_charging, max_kwh_discharging, name, now, percentage_to_kwh, sorted_by_cheapest_price, t, valuta, valuta_kwh, total_cost, total_kwh)

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

    # ------------------------------------------------------------------ #
    # Overview markdown
    # ------------------------------------------------------------------ #


# ---------------------------------------------------------------------- #
# Hour rules overview: consecutive hours with the same rule in one row
# ---------------------------------------------------------------------- #
