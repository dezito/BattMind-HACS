"""Charging planner (engine mixin) - port of cheap_grid_charge_hours()."""
from __future__ import annotations

import datetime
import logging
from collections import defaultdict

from .formatting import emoji_parse, emoji_text_format, get_list_values, join_unique_emojis
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


class PlannerMixin:
    """The charging / discharging planner."""

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
        valuta_kwh = t("ui.common.valuta_kwh")
        valuta = t("ui.common.valuta")

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

        def kwh_available_in_hour(hour):
            hour_in_charge_hours = False
            kwh_available = max_kwh_charging
            try:
                kwh = max_kwh_charging
                kwh -= kwh * (hour.minute / 60)
                if hour in charge_hours:
                    hour_in_charge_hours = True
                    kwh_available = kwh - charge_hours[hour]["kWh"]
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
                        config["solar"]["powerwall_battery_level_max"] - (battery_level + kwh_to_percentage(kwh)), 2
                    )
                    kwh_diff = round(percentage_to_kwh(battery_level_diff), 2)
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
                    battery_level_added = kwh_to_percentage(kwh)

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

                if self.enabled("solar_charging"):
                    if not hour_in(timestamp, charging_plan[day]["blocked_solar_charge_timestamps"]):
                        try:
                            solar_added += kwh_to_percentage(charging_plan[day]["solar_kwh_prediction"][hour])
                            solar_added = min(solar_added, kwh_to_percentage(max_kwh_charging))
                        except Exception:  # noqa: BLE001
                            pass

                if hour not in charging_plan[day]["hour_cost_prediction"][forecast_type]:
                    continue

                percentage_added = grid_added + solar_added
                percentage_sold = 0.0
                percentage_used = charging_plan[day]["hour_cost_prediction"][forecast_type][hour]["percentage"] * -1

                if charging_plan[day]["force_discharge_timestamps"]:
                    discharge_limit_percentage = kwh_to_percentage(max_kwh_discharging)
                    for ts in charging_plan[day]["force_discharge_timestamps"]:
                        if ts.hour != timestamp.hour:
                            continue
                        percentage_sold += kwh_to_percentage(charging_plan[day]["force_discharge_timestamps"][ts]["kwh"]) * -1
                    if abs(percentage_sold) + percentage_added > discharge_limit_percentage:
                        diff = abs(percentage_sold) + percentage_added - discharge_limit_percentage
                        percentage_sold -= diff * -1
                    grid_added = 0.0
                    solar_added = 0.0

                if not hour_in(timestamp, charging_plan[day]["discharge_timestamps"]):
                    percentage_used = 0.0

                power_balance = percentage_used + percentage_added + percentage_sold
                power_balance_without_solar = percentage_used + grid_added + percentage_sold

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

            def _get_predicted_battery_cost(day, hour=None, predicted_battery_level=None):
                total_grid_solar_kwh = []
                total_grid_cost_prediction = []
                battery_level = predicted_battery_level
                if battery_level is None:
                    battery_level = get_battery_level() if day == 0 else sum(charging_plan[day]["battery_level_end_of_day"])
                if hour is None:
                    hour = 23

                powerwall_kwh = [percentage_to_kwh(battery_level)]
                powerwall_kwh_price = battery_expenses.get("unit")
                solar_index = len(charging_plan[day]["solar_kwh_prediction"])
                min_battery_level = percentage_to_kwh(config["solar"]["powerwall_battery_level_min"])

                for h in range(0, hour + 1):
                    timestamp = current_hour.replace(hour=h) + datetime.timedelta(days=day)
                    if timestamp < current_hour:
                        continue
                    if h < solar_index and charging_plan[day]["solar_kwh_prediction"][h] > 0.0:
                        after_solar_added = sum(powerwall_kwh) - charging_plan[day]["solar_kwh_prediction"][h]
                        if after_solar_added >= min_battery_level:
                            powerwall_kwh.append(charging_plan[day]["solar_kwh_prediction"][h] * -1)
                            battery_level -= kwh_to_percentage(charging_plan[day]["solar_kwh_prediction"][h])
                            total_grid_solar_kwh.append(charging_plan[day]["solar_kwh_prediction"][h])
                            if h < len(charging_plan[day]["solar_cost_prediction"]):
                                total_grid_cost_prediction.append(charging_plan[day]["solar_cost_prediction"][h])
                    if hour_in(timestamp, charging_plan[day]["charging_sessions"]):
                        session = charging_plan[day]["charging_sessions"].get(timestamp)
                        if session is not None:
                            total_grid_solar_kwh.append(session["kWh"])
                            total_grid_cost_prediction.append(session["Cost"])

                if not isinstance(powerwall_kwh_price, (int, float)):
                    powerwall_kwh_price = self.get_powerwall_kwh_price()

                total_grid_solar_kwh.append(sum(powerwall_kwh))
                total_grid_cost_prediction.append(powerwall_kwh_price * sum(powerwall_kwh))

                raw = sum(total_grid_cost_prediction) / sum(total_grid_solar_kwh) if sum(total_grid_solar_kwh) > 0.0 else 0.0
                loss_cost = calc_battery_loss_cost(raw)
                return raw, loss_cost, raw + loss_cost + abs(config["solar"]["powerwall_wear_cost_per_kwh"])

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

                kwh_needed = self.kwh_needed_for_charging(
                    min(sum(percentage_needed_today), config["solar"]["powerwall_battery_level_max"]), highest_battery_level
                )
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

                        kwh_with_profit = []
                        for hour in range(timestamp.hour, 24):
                            loop_price = grid_prices.get(timestamp.replace(hour=hour))
                            if loop_price is None:
                                continue
                            profit = loop_price - (price + calc_battery_loss_cost(price) + abs(config["solar"]["powerwall_wear_cost_per_kwh"]))
                            if profit < get_min_profit_per_kwh():
                                continue
                            prediction = charging_plan[day]["hour_cost_prediction"][forecast_type].get(hour)
                            if prediction is not None:
                                kwh_with_profit.append(prediction["kwh"])

                        hour_in_charge_hours, kwh_available = kwh_available_in_hour(timestamp)
                        timestamp_battery_level = charging_plan[what_day]["battery_level_flow"].get(timestamp.hour, [0.0])
                        battery_level_to_use = max(sum(timestamp_battery_level), highest_battery_level)
                        kwh_from_battery_level_used = percentage_to_kwh(
                            max(config["solar"]["powerwall_battery_level_max"] - battery_level_to_use, 0.0)
                        )
                        full_charge_kwh_needed = percentage_to_kwh(
                            config["solar"]["powerwall_battery_level_max"] - config["solar"]["powerwall_battery_level_min"]
                        )
                        kwh_available = min(sum(kwh_with_profit), kwh_from_battery_level_used, full_charge_kwh_needed)
                        if hour_in_charge_hours and kwh_available <= 0.0:
                            continue

                        if round(kwh_needed, 1) > 0.0 and kwh_to_percentage(kwh_needed) > 0.0:
                            kwh_needed, total_cost, total_kwh, battery_level_added, cost_added = add_to_charge_hours(
                                kwh_needed, total_cost, total_kwh, timestamp, price, kwh_available, highest_battery_level,
                                rules=["cheapest_hour_fill_planner"],
                            )
                            if timestamp in charge_hours and battery_level_added:
                                charging_plan[day]["total_cost"] += cost_added
                                reason = (
                                    f"<details><summary>{emoji_parse({'charging': True})}Billigste timer ({price}/{battery_level_added:.0f}%)"
                                    f"{other_day_text(day, what_day)}</summary>"
                                    f"Højeste batteriniveau tidspunkt: **{highest_battery_level_timestamp.strftime('%H:%M')}**<br>"
                                    f"Højeste batteriniveau: **{highest_battery_level:.1f}%**<br>"
                                    f"Aktuel elpris: **{price}** {valuta_kwh}<br>"
                                    f"Batteriniveau tilføjet: **{battery_level_added:.2f}%**<br>"
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
                    sorted_kwh_needed = charging_plan[day]["hour_cost_prediction"][forecast_type][sorted_hour]["kwh"]
                    sorted_percentage_needed = kwh_to_percentage(sorted_kwh_needed)
                    sorted_price = charging_plan[day]["hour_cost_prediction"][forecast_type][sorted_hour]["price"]
                    if sorted_timestamp <= current_hour:
                        continue
                    sorted_battery_level = sum(charging_plan.get(day, {}).get("battery_level_flow", {}).get(sorted_timestamp.hour, [0.0]))
                    if sorted_percentage_needed <= sorted_battery_level:
                        continue

                    remove_list = []
                    highest_battery_level = sorted_battery_level
                    from_timestamp, to_timestamp = charging_window(day)

                    for timestamp, price in sorted_by_cheapest_price:
                        if not in_between(timestamp, from_timestamp, to_timestamp) or timestamp < current_hour:
                            continue
                        if timestamp > sorted_timestamp:
                            continue
                        kwh_profit = sorted_price - (price + calc_battery_loss_cost(price) + abs(config["solar"]["powerwall_wear_cost_per_kwh"]))
                        if kwh_profit < get_min_profit_per_kwh():
                            continue
                        hour_in_charge_hours, kwh_available = kwh_available_in_hour(timestamp)
                        if hour_in_charge_hours and kwh_available <= 0.0:
                            remove_list.append(timestamp)
                            continue

                        highest_battery_level_timestamp = timestamp
                        what_day = days_between(current_hour, timestamp)
                        if what_day not in charging_plan:
                            continue
                        highest_battery_level = max(
                            sum(charging_plan[what_day]["battery_level_flow"].get(timestamp.hour, [0.0])), highest_battery_level
                        )
                        for i in range(1, hours_between(timestamp, sorted_timestamp) + 1):
                            loop_timestamp = sorted_timestamp - datetime.timedelta(hours=i)
                            loop_day = days_between(current_hour, loop_timestamp)
                            loop_battery_level = sum(
                                charging_plan.get(loop_day, {}).get("battery_level_flow", {}).get(loop_timestamp.hour, [0.0])
                            )
                            if loop_battery_level > highest_battery_level:
                                highest_battery_level_timestamp = loop_timestamp
                                highest_battery_level = loop_battery_level

                        available_battery_level = config["solar"]["powerwall_battery_level_max"] - max(
                            highest_battery_level, config["solar"]["powerwall_battery_level_min"]
                        )
                        available_kwh = percentage_to_kwh(available_battery_level)
                        if available_kwh <= 0.0:
                            remove_list.append(timestamp)
                            continue
                        kwh_needed = min(sorted_kwh_needed, available_kwh)
                        if kwh_needed <= 0.0:
                            remove_list.append(timestamp)
                            continue
                        kwh_available = round(min(available_kwh, kwh_available), 1)
                        if kwh_available <= 0.0:
                            remove_list.append(timestamp)
                            continue

                        what_day = days_between(now, timestamp)
                        if what_day < day - 1 or what_day not in charging_plan:
                            continue

                        if round(kwh_needed, 1) > 0.0 and kwh_to_percentage(kwh_needed) > 0.0:
                            sorted_kwh_needed, total_cost, total_kwh, battery_level_added, cost_added = add_to_charge_hours(
                                kwh_needed, total_cost, total_kwh, timestamp, price, kwh_available, highest_battery_level,
                                rules=["most_expensive_planner"],
                            )
                            if timestamp in charge_hours and battery_level_added:
                                charging_plan[day]["total_cost"] += cost_added
                                reason = (
                                    f"<details><summary>{emoji_parse({'charging': True})}Dyreste timer ({price}/{battery_level_added:.0f}%)"
                                    f"{other_day_text(day, what_day)}</summary>"
                                    f"Prioriteret time: **{sorted_timestamp}**<br>"
                                    f"Prioriteret batteriniveau: **{sorted_battery_level:.1f}%**<br>"
                                    f"Prioriteret elpris: **{sorted_price}** {valuta_kwh}<br>"
                                    f"Højeste batteriniveau tidspunkt: **{highest_battery_level_timestamp.strftime('%H:%M')}**<br>"
                                    f"Højeste batteriniveau: **{highest_battery_level:.1f}%**<br>"
                                    f"Aktuel elpris: **{price} {valuta_kwh}**<br>"
                                    f"Profit ved at lade til denne time: **{kwh_profit:.2f} {valuta_kwh}**<br>"
                                    f"Tilgængeligt batteriniveau: **{available_battery_level:.1f}%**<br>"
                                    f"Batteriniveau tilføjet: **{battery_level_added:.2f}%**<br>"
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
                nonlocal total_cost, total_kwh
                highest_battery_level = get_battery_level() if day == 0 else sum(charging_plan[day]["battery_level_start_of_day"])
                highest_battery_level_timestamp = current_hour if day == 0 else charging_plan[day]["start_of_day"]
                from_hour = now.hour if day == 0 else 0

                for hour in range(from_hour, 24):
                    battery_level = sum(charging_plan[day]["battery_level_flow"].get(hour, [0.0]))
                    if battery_level > highest_battery_level:
                        highest_battery_level = battery_level
                        highest_battery_level_timestamp = highest_battery_level_timestamp.replace(hour=hour)

                highest_battery_level_timestamp = current_hour if day == 0 else charging_plan[day]["end_of_day"]
                from_timestamp, to_timestamp = charging_window(day)

                for hour in range(from_hour, highest_battery_level_timestamp.hour + 1):
                    if hour not in charging_plan[day]["hour_cost_prediction"][forecast_type]:
                        continue
                    battery_level = sum(charging_plan[day]["battery_level_flow"].get(hour, [0.0])) - config["solar"]["powerwall_battery_level_min"]
                    kwh_needed = self.kwh_needed_for_charging(
                        charging_plan[day]["hour_cost_prediction"][forecast_type][hour]["percentage"], battery_level
                    )
                    if kwh_needed <= 0.0:
                        continue
                    _LOGGER.debug(
                        "Needed before max level: day %s hour %02d needs %.2f kWh (battery above min %.1f%%)",
                        day, hour, kwh_needed, battery_level,
                    )
                    needed_price = grid_prices.get(charging_plan[day]["start_of_day"].replace(hour=hour))
                    if needed_price is None:
                        continue

                    finished = False
                    for _ in range(int(round_up(kwh_needed / max_kwh_charging) + 1)):
                        if finished:
                            break
                        for timestamp, price in sorted_by_cheapest_price:
                            if not in_between(timestamp, from_timestamp, to_timestamp) or timestamp < current_hour:
                                continue
                            kwh_profit = needed_price - (price + calc_battery_loss_cost(price) + abs(config["solar"]["powerwall_wear_cost_per_kwh"]))
                            if kwh_profit < get_min_profit_per_kwh():
                                continue
                            hour_in_charge_hours, kwh_available = kwh_available_in_hour(timestamp)
                            if hour_in_charge_hours and kwh_available <= 0.0:
                                continue
                            what_day = days_between(current_hour, timestamp)
                            if what_day < day - 1 or what_day not in charging_plan:
                                continue

                            if round(kwh_needed, 1) > 0.0 and kwh_to_percentage(kwh_needed) > 0.0:
                                kwh_needed, total_cost, total_kwh, battery_level_added, cost_added = add_to_charge_hours(
                                    kwh_needed, total_cost, total_kwh, timestamp, price, kwh_available, highest_battery_level,
                                    rules=["needed_before_max_level_planner"],
                                )
                                if timestamp in charge_hours and battery_level_added:
                                    charging_plan[day]["total_cost"] += cost_added
                                    reason = (
                                        f"<details><summary>{emoji_parse({'needed_before_max_level_planner': True})}Brug for "
                                        f"({price}/{battery_level_added:.0f}%){other_day_text(day, what_day)}</summary>"
                                        f"Højeste batteriniveau tidspunkt: **{highest_battery_level_timestamp.strftime('%H:%M')}**<br>"
                                        f"Højeste batteriniveau: **{highest_battery_level:.1f}%**<br>"
                                        f"Aktuel elpris: **{price}** {valuta_kwh}<br>"
                                        f"Batteriniveau tilføjet: **{battery_level_added:.2f}%**<br>"
                                        "</details>"
                                    )
                                    add_charging_session_to_day(timestamp, what_day, reason=reason)
                                    add_charging_to_days(timestamp)
                        else:
                            finished = True

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
                    battery_level = charging_plan[day]["battery_level_flow"].get(hour)
                    raw, loss_cost, battery_kwh_cost = _get_predicted_battery_cost(
                        day, hour, predicted_battery_level=sum(battery_level) if battery_level is not None else None
                    )
                    min_profit_per_kwh = get_min_profit_per_kwh()
                    profit = price - battery_kwh_cost
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
                            f"<details><summary>{emoji_parse({'blocked': True})}Ingen fortjeneste ({profit:.2f})</summary>"
                            f"Battery level: **{sum(charging_plan[day]['battery_level_flow'][hour]):.1f}%**<br>"
                            f"Battery kWh cost (basis): **{raw:.2f} {valuta_kwh}**<br>"
                            f"Charge/Discharge loss: **{loss_cost:.2f} {valuta_kwh}**<br>"
                            f"Wear cost per kWh: **{abs(config['solar']['powerwall_wear_cost_per_kwh']):.2f} {valuta_kwh}**<br>"
                            f"**Samlet battery kWh cost**: **{battery_kwh_cost:.2f} {valuta_kwh}**<br>"
                            f"Aktuel elpris: **{price} {valuta_kwh}**<br>"
                            f"Minimum fortjeneste pr. kWh: **{min_profit_per_kwh:.2f} {valuta_kwh}**<br>"
                            f"Fortjeneste pr. kWh: **{profit:.2f} {valuta_kwh}**<br>"
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

                highest_battery_kwh = percentage_to_kwh(highest_battery_level)
                battery_kwh = highest_battery_kwh
                prioritized_hours = []
                prioritized_kwh = []

                for hour in charging_plan[day]["sorted_hour_cost_prediction"][forecast_type]:
                    timestamp = current_hour.replace(hour=hour) + datetime.timedelta(days=day)
                    if timestamp not in charging_plan[day]["discharge_timestamps"]:
                        continue
                    if timestamp < highest_battery_level_timestamp:
                        continue
                    kwh_needed = charging_plan[day]["hour_cost_prediction"][forecast_type][hour]["kwh"]
                    battery_kwh = max(battery_kwh - kwh_needed, powerwall_kwh_min)

                    if battery_kwh <= powerwall_kwh_min:
                        if prioritized_hours and hour > max(prioritized_hours):
                            continue
                        charging_plan[day]["discharge_timestamps"].remove(timestamp)
                        prioritized_hours_string = ", ".join(str(h) for h in prioritized_hours)
                        prioritized_kwh_string = (
                            f"{', '.join(f'{kwh_to_percentage(k):.0f}%' for k in prioritized_kwh)} "
                            f"({kwh_to_percentage(sum(prioritized_kwh)):.1f}%)"
                        )
                        charging_plan[day]["blocked_discharge_timestamps"][timestamp] = (
                            f"<details><summary>{emoji_parse({'blocked': True})}Dyreste timer prioriteret "
                            f"({prioritized_hours_string.replace(' ', '')})</summary>"
                            "(battery_kwh ≤ minimum threshold)<br>"
                            f"Prioriterede timer: **{prioritized_hours_string}**<br>"
                            f"Prioriterede kWh: **{prioritized_kwh_string}**<br>"
                            f"Highest battery level time: **{highest_battery_level_timestamp}**<br>"
                            f"Highest battery kWh: **{kwh_to_percentage(highest_battery_kwh):.1f}%**<br>"
                            f"Current battery kWh: **{kwh_to_percentage(battery_kwh):.1f}%**<br>"
                            f"Minimum allowed kWh: **{kwh_to_percentage(powerwall_kwh_min):.1f}%**<br>"
                            f"Kwh needed for discharge: **{kwh_to_percentage(kwh_needed):.1f}%**<br>"
                            "</details>"
                        )
                    else:
                        prioritized_hours.append(hour)
                        prioritized_kwh.append(kwh_needed)

            def lowest_battery_level_until_next_charge(day):
                lowest_timestamp = charging_plan[day]["end_of_day"]
                lowest_battery_level = sum(charging_plan[day]["battery_level_end_of_day"])
                using_next_day = False
                if not self.enabled("use_midnight_battery_level") and day < amount_of_days - 1:
                    for hour in range(24):
                        timestamp = charging_plan[day + 1]["start_of_day"] + datetime.timedelta(hours=hour)
                        what_day = days_between(charging_plan[0]["start_of_day"], timestamp)
                        loop_hour = timestamp.hour
                        if what_day not in charging_plan or loop_hour not in charging_plan[what_day]["battery_level_flow"]:
                            continue
                        battery_level = sum(charging_plan[what_day]["battery_level_flow"][loop_hour])
                        if battery_level < lowest_battery_level:
                            using_next_day = True
                            lowest_battery_level = battery_level
                            lowest_timestamp = timestamp
                        if lowest_battery_level == config["solar"]["powerwall_battery_level_min"]:
                            break
                return lowest_timestamp, lowest_battery_level, using_next_day

            def sell_prices_for_window(from_timestamp, to_timestamp):
                prices = {ts: price for ts, price in grid_sell_prices.items() if in_between(ts, from_timestamp, to_timestamp)}
                sell_price = config["solar"]["production_price"]
                using_grid_sell_price = True
                if sell_price != -1.0:
                    using_grid_sell_price = False
                    for ts in prices:
                        prices[ts] = max(round(sell_price - prices[ts], 2), 0.0)
                return prices, sell_price, using_grid_sell_price

            def most_profit(day):
                from_timestamp = max(charging_plan[day]["start_of_day"], current_sell_price_timestamp)
                lowest_timestamp, _, _ = lowest_battery_level_until_next_charge(day)
                grid_sell_prices_for_day, sell_price, using_grid_sell_price = sell_prices_for_window(
                    from_timestamp, lowest_timestamp + datetime.timedelta(hours=1)
                )
                powerwall_usage_profit_total = []
                hours = hours_between(current_hour, lowest_timestamp)
                exclude_hours = self.get_exclude_sell_hours()
                min_profit_per_kwh = get_min_profit_per_kwh()

                for i in range(hours + 1):
                    timestamp = current_hour + datetime.timedelta(hours=i)
                    what_day = days_between(charging_plan[0]["start_of_day"], timestamp)
                    hour = timestamp.hour
                    if what_day not in charging_plan or hour not in charging_plan[what_day]["hour_cost_prediction"][forecast_type]:
                        continue
                    prediction = charging_plan[what_day]["hour_cost_prediction"][forecast_type][hour]
                    _, _, battery_kwh_cost = _get_predicted_battery_cost(what_day, hour, predicted_battery_level=prediction["percentage"])
                    grid_price = grid_prices.get(timestamp)
                    if grid_price is None:
                        continue
                    powerwall_usage_profit = (grid_price - battery_kwh_cost if grid_price > 0.0 else 0.0) * prediction["kwh"]
                    if powerwall_usage_profit < min_profit_per_kwh:
                        exclude_hours.append(hour)
                        continue
                    powerwall_usage_profit_total.append(round(powerwall_usage_profit, 2))

                day_periods_in_hour = self.price_provider.sell_periods_in_hour(charging_plan[day]["start_of_day"])
                for i in range(hours * day_periods_in_hour + 1):
                    timestamp = current_hour + datetime.timedelta(minutes=(60 / day_periods_in_hour) * i)
                    what_day = days_between(charging_plan[0]["start_of_day"], timestamp)
                    hour = timestamp.hour
                    if timestamp not in grid_sell_prices_for_day or timestamp < from_timestamp or hour in exclude_hours:
                        continue
                    if what_day not in charging_plan or hour not in charging_plan[what_day]["battery_level_flow"]:
                        continue
                    periods_in_hour = self.price_provider.sell_periods_in_hour(timestamp)
                    period_max_kwh_discharging = abs(max_kwh_discharging) / periods_in_hour
                    battery_level = min(
                        max(sum(charging_plan[what_day]["battery_level_flow"][hour]) - config["solar"]["powerwall_battery_level_min"], 0.0),
                        kwh_to_percentage(period_max_kwh_discharging),
                    )
                    if battery_level <= 0.0:
                        continue
                    kwh = percentage_to_kwh(battery_level)
                    if kwh < self.number("min_sell_kwh") / periods_in_hour:
                        continue

                    raw, loss_cost, battery_kwh_cost = _get_predicted_battery_cost(what_day, hour, predicted_battery_level=battery_level)
                    grid_sell_price = grid_sell_prices_for_day[timestamp]
                    grid_sell_profit = round((grid_sell_price - battery_kwh_cost if grid_sell_price > 0.0 else 0.0) * kwh, 2)
                    grid_sell_price = round(grid_sell_price, 2)
                    battery_kwh_cost = round(battery_kwh_cost, 2)
                    sell_profit = grid_sell_profit - sum(powerwall_usage_profit_total)

                    if sell_profit > min_profit_per_kwh * kwh:
                        _LOGGER.debug(
                            "Most profit: selling %.2f kWh at %s (profit %.2f kr vs %.2f kr using the battery)",
                            kwh, timestamp, grid_sell_profit, sum(powerwall_usage_profit_total),
                        )
                        diff = grid_sell_profit - sum(powerwall_usage_profit_total)
                        fixed_price_calc_text = (
                            f"{sell_price:.2f} - {grid_sell_prices.get(timestamp, 0.0):.2f} =" if not using_grid_sell_price else ""
                        )
                        charging_plan[what_day]["force_discharge_timestamps"][timestamp] = {
                            "title": "Sælger resterende kWh i batteriet",
                            "emoji": emoji_parse({"discharging": True}),
                            "kwh": kwh,
                            "profit": grid_sell_profit,
                            "reason": (
                                f"<details><summary>{emoji_parse({'discharging': True})}Sælger resterende kWh i batteriet<br>"
                                f"({kwh:.1f} kWh / {grid_sell_profit:.2f} kr)</summary>"
                                f"Battery kWh cost (basis): **{raw:.2f} {valuta_kwh}**<br>"
                                f"Charge/Discharge loss: **{loss_cost:.2f} {valuta_kwh}**<br>"
                                f"Wear cost per kWh: **{abs(config['solar']['powerwall_wear_cost_per_kwh']):.2f} {valuta_kwh}**<br>"
                                f"**Samlet battery kWh cost**: **{battery_kwh_cost:.2f} {valuta_kwh}**<br>"
                                f"Grid sell price: **{fixed_price_calc_text}{grid_sell_price:.2f} {valuta_kwh}**<br>"
                                f"kWh sold: **{kwh:.2f} kWh**<br>"
                                f"Profit from selling: **{grid_sell_profit:.2f} {valuta}**<br>"
                                f"Profit from using battery: **{sum(powerwall_usage_profit_total):.2f} {valuta}**<br>"
                                f"Difference: **{diff:.2f} {valuta}**<br>"
                                "</details>"
                            ),
                        }

            def sell_excess_kwh_available(day):
                from_timestamp = max(charging_plan[day]["start_of_day"], current_sell_price_timestamp)
                day_periods_in_hour = self.price_provider.sell_periods_in_hour(charging_plan[day]["start_of_day"])
                lowest_timestamp, lowest_battery_level, using_next_day = lowest_battery_level_until_next_charge(day)

                lowest_battery_level = round(lowest_battery_level, 0) - config["solar"]["powerwall_battery_level_min"]
                excess_kwh_available = round(percentage_to_kwh(max(lowest_battery_level, 0.0)), 1)
                min_sell_kwh = self.number("min_sell_kwh") / day_periods_in_hour

                if excess_kwh_available < min_sell_kwh or excess_kwh_available <= 0.0:
                    charging_plan[day]["force_discharge_timestamps_empty"] = (
                        "<details><summary>⚠️ Ingen overskydende kWh tilgængelig at sælge</summary>"
                        f"Laveste batteriniveau timestamp: **{lowest_timestamp}**<br>"
                        f"Laveste batteriniveau: **{lowest_battery_level:.1f}%**<br>"
                        f"Batteri kWh tilgængelig at sælge: **{excess_kwh_available:.2f} kWh**<br>"
                        f"Minimum kWh at sælge: **{min_sell_kwh:.2f} kWh**<br>"
                        "</details>"
                    )
                    return

                if using_next_day:
                    _LOGGER.debug("Using lowest battery level of next day (%s) for excess kWh on day %s", lowest_timestamp, day)

                discharge_hours_needed = int(round_up(excess_kwh_available / (abs(max_kwh_discharging) / day_periods_in_hour)))
                grid_sell_prices_for_day, sell_price, using_grid_sell_price = sell_prices_for_window(
                    from_timestamp, charging_plan[day]["end_of_day"] + datetime.timedelta(hours=6)
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

                        battery_level = charging_plan[what_day]["battery_level_flow"].get(timestamp.hour)
                        loop_raw, loop_loss_cost, loop_battery_kwh_cost = _get_predicted_battery_cost(
                            day, timestamp.hour, predicted_battery_level=sum(battery_level) if battery_level is not None else None
                        )
                        periods_in_hour = self.price_provider.sell_periods_in_hour(timestamp)
                        excess_current = min(excess_kwh_available, abs(max_kwh_discharging) / periods_in_hour)
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
                                    "Sell excess: day %s %s profit too low (%.2f kr < %.2f kr)",
                                    day, timestamp, excess_profit, min_total_profit,
                                )
                                continue
                        _LOGGER.debug(
                            "Sell excess: day %s selling %.2f kWh at %s (profit %.2f kr, min %.2f kr)",
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
                            "title": "Sælger overskydende kWh",
                            "emoji": emoji_parse({"discharging": True}),
                            "kwh": excess_current,
                            "profit": excess_profit,
                            "reason": (
                                f"<details><summary>{emoji_parse({'discharging': True})}Sælger overskydende kWh<br>"
                                f"({excess_current:.1f} kWh / {excess_profit:.2f} kr){other_day}</summary>"
                                f"Battery kWh cost (basis): **{loop_raw:.2f} {valuta_kwh}**<br>"
                                f"Charge/Discharge loss: **{loop_loss_cost:.2f} {valuta_kwh}**<br>"
                                f"Wear cost per kWh: **{abs(config['solar']['powerwall_wear_cost_per_kwh']):.2f} {valuta_kwh}**<br>"
                                f"**Samlet battery kWh cost**: **{loop_battery_kwh_cost:.2f} {valuta_kwh}**<br>"
                                f"Grid price: **{fixed_price_calc_text}{price:.2f} {valuta_kwh}**<br>"
                                f"Excess kWh sold: **{excess_current:.2f} kWh**<br>"
                                f"Profit from selling: **{excess_profit:.2f} {valuta}**<br>"
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
                        hour_excluded = f"⚠️ Maks pris tidspunktet er i de ekskluderede timer: <b>{max_price_timestamp.hour}:00</b><br>"
                    charging_plan[day]["force_discharge_timestamps_empty"] = (
                        f"<details><summary>⚠️Ingen gode tidspunkter at sælge overskydende kWh på ({profit:.2f})</summary>"
                        f"{hour_excluded}"
                        f"Battery kWh cost (basis): <b>{raw:.2f} {valuta_kwh}</b><br>"
                        f"Charge/Discharge loss: <b>{loss_cost:.2f} {valuta_kwh}</b><br>"
                        f"Wear cost per kWh: <b>{abs(config['solar']['powerwall_wear_cost_per_kwh']):.2f} {valuta_kwh}</b><br>"
                        f"<b>Samlet battery kWh cost: {battery_kwh_cost:.2f} {valuta_kwh}</b><br>"
                        f"Maks pris tidspunkt i dag: <b>{max_price_timestamp if max_price_timestamp else 'N/A'}</b><br>"
                        f"Maks pris for at sælge i dag: <b>{max_price:.2f} {valuta_kwh}</b><br>"
                        f"Minimum fortjeneste pr. kWh: <b>{get_min_profit_per_kwh():.2f} {valuta_kwh}</b><br>"
                        f"Profit from selling: <b>{profit:.2f} {valuta_kwh}</b><br>"
                        "</details>"
                    )

            def force_powerwall_max_hours_old_discharge(day):
                max_hours_old = self.number("powerwall_max_hours_old")
                max_days_old = max(int(round(max_hours_old / 24)), 0)
                if day > max_days_old:
                    _LOGGER.debug("Old energy: day %s exceeds the max age (%s days), skipping", day, max_days_old)
                    return battery_expenses_keys
                remove_used_keys = []
                from_hour = current_hour.hour if day == 0 else 0
                for expense_timestamp in battery_expenses_keys:
                    for hour in range(from_hour, 24):
                        loop_hour = current_hour.replace(hour=hour) + datetime.timedelta(days=day)
                        if hours_between(expense_timestamp, loop_hour) < max_hours_old:
                            continue
                        if hour_in(loop_hour, charging_plan[day]["charging_sessions"]):
                            continue
                        if hour_in(loop_hour, charging_plan[day]["force_discharge_timestamps"]):
                            continue
                        charging_plan[day]["blocked_discharge_timestamps"].pop(loop_hour, None)

                        if loop_hour not in charging_plan[day]["discharge_timestamps"]:
                            _LOGGER.debug(
                                "Old energy: allowing discharge at %s (energy charged %s is older than %s hours)",
                                loop_hour, expense_timestamp, max_hours_old,
                            )
                            charging_plan[day]["discharge_timestamps"].append(loop_hour)
                            data = battery_expenses[expense_timestamp]
                            price = grid_prices.get(loop_hour, data["price"])
                            profit = price - data["price"]
                            battery_loss_cost = calc_battery_loss_cost(data["price"])
                            battery_kwh_cost = data["price"] + battery_loss_cost + abs(config["solar"]["powerwall_wear_cost_per_kwh"])
                            excess_kwh_from_history = sum(battery_expenses[key]["kWh"] for key in battery_expenses_keys)
                            kwh = data["kWh"] + (data["kWh"] - excess_kwh_from_history)
                            charging_plan[day]["force_powerwall_max_hours_old_discharge_timestamps"][loop_hour] = {
                                "kwh": kwh,
                                "profit": profit,
                                "reason": (
                                    f"<details><summary>{emoji_parse({'discharging': True})}Gammel strøm bruges ({profit:.2f})</summary>"
                                    f"Battery kWh cost (basis): **{data['price']:.2f} {valuta_kwh}**<br>"
                                    f"Charge/Discharge loss: **{battery_loss_cost:.2f} {valuta_kwh}**<br>"
                                    f"Wear cost per kWh: **{abs(config['solar']['powerwall_wear_cost_per_kwh']):.2f} {valuta_kwh}**<br>"
                                    f"**Samlet battery kWh cost**: **{battery_kwh_cost:.2f} {valuta_kwh}**<br>"
                                    f"Grid buy price: **{price:.2f} {valuta_kwh}**<br>"
                                    f"Profit from using battery: **{profit:.2f} {valuta}**<br>"
                                    "</details>"
                                ),
                            }
                            remove_used_keys.append(expense_timestamp)
                for key in remove_used_keys:
                    if key in battery_expenses_keys:
                        battery_expenses_keys.remove(key)
                return battery_expenses_keys

            def optimize_solar_charging(day):
                """Move solar charging to cheaper export hours without reducing final battery level."""
                cfg = config["solar"]
                data = charging_plan[day]
                start = data["start_of_day"]
                solar = data.get("solar_kwh_prediction", [])
                prices = data.get("solar_cost_prediction", [])
                base = data.get("battery_level_flow_without_solar", {})
                original = data.get("battery_level_flow", {})
                blocked = data["blocked_solar_charge_timestamps"] = {}

                size = cfg["powerwall_battery_size"]
                if not base or not original or size <= 0:
                    return
                factor = 100 * (1 - cfg["powerwall_charge_discharge_loss"]) / size
                if factor <= 0:
                    return

                minimum = cfg["powerwall_battery_level_min"]
                maximum = cfg["powerwall_battery_level_max"]
                eps = 0.000001
                from_hour = now.hour if day == 0 else 0

                hours = []
                grid = {ts.hour for ts in data.get("charging_sessions", {}) if ts.date() == start.date()}
                available = {}
                eligible = {}
                for h in sorted(base):
                    if h < from_hour or h not in original or not original[h]:
                        continue
                    hours.append(h)
                    if h < len(solar) and h < len(prices) and solar[h] > 0:
                        available[h] = float(solar[h]) * factor
                        if h not in grid:
                            eligible[h] = available[h]
                if not hours or not eligible:
                    return

                def simulate(excluded):
                    result, charged = {}, {}
                    level = float(original[hours[0]][0])
                    for h in hours:
                        before = level
                        vals = base[h]
                        level = max(minimum, min(100, level + (vals[1] if len(vals) > 1 else 0)))
                        charge = 0
                        if h in available and h not in excluded:
                            charge = min(available[h], max(0, maximum - level))
                            level += charge
                        charged[h] = charge
                        result[h] = [before, level - before]
                    return result, charged, level

                excluded: set = set()
                flow, charged, end_level = simulate(excluded)
                original_charged = charged.copy()

                while True:
                    best = None
                    for h in eligible:
                        if h in excluded or charged[h] <= eps or original_charged[h] <= eps:
                            continue
                        if sum(flow[h]) >= maximum - eps:
                            continue
                        test = excluded | {h}
                        new_flow, new_charged, new_end = simulate(test)
                        if new_end < end_level - eps:
                            continue
                        moved = 0
                        profit = 0
                        for t_hour in available:
                            diff = (charged[t_hour] - new_charged[t_hour]) / factor
                            profit += diff * float(prices[t_hour])
                            if t_hour > h and diff < -eps:
                                moved -= diff
                        removed = charged[h] / factor
                        if moved < removed - eps or profit <= eps:
                            continue
                        if best is None or profit > best[0] + eps or (abs(profit - best[0]) <= eps and h < best[1]):
                            best = (profit, h, new_flow, new_charged, new_end)
                    if best is None:
                        break
                    excluded.add(best[1])
                    flow, charged, end_level = best[2], best[3], best[4]

                allocation = {}
                pending = []
                for h in hours:
                    if h in excluded:
                        allocation[h] = []
                        pending.append([h, original_charged[h] / factor])
                    elif h in available:
                        remaining = max(0, (charged[h] - original_charged[h]) / factor)
                        for source in pending:
                            if remaining <= eps:
                                break
                            if source[1] <= eps:
                                continue
                            amount = min(remaining, source[1])
                            allocation[source[0]].append((h, amount))
                            source[1] -= amount
                            remaining -= amount

                for h in sorted(excluded):
                    ts = start.replace(hour=h, minute=0, second=0, microsecond=0)
                    later_text = ""
                    moved = 0
                    profit = 0
                    for t_hour, kwh in allocation[h]:
                        moved += kwh
                        profit += kwh * (float(prices[h]) - float(prices[t_hour]))
                        later_text += f"Kl. {t_hour:02d}:00: {kwh:.2f} kWh ({prices[t_hour]:.2f} kr/kWh)<br>"
                    blocked[ts] = {
                        "reason": (
                            f"<details><summary>{emoji_parse({'blocked_solar_charge': True})}Blokeret solcelle ladning</summary>"
                            f"<b>Før blokering:</b> {sum(original[h]):.1f}%<br>"
                            f"<b>Efter blokering:</b> {sum(flow[h]):.1f}%<br>"
                            f"<b>Solcelleoverskud:</b> {solar[h]:.2f} kWh<br>"
                            f"<b>Salgspris:</b> {prices[h]:.2f} kr/kWh<br>"
                            f"<b>Senere opladning:</b> {moved:.2f} kWh<br>"
                            f"<b>Forventet økonomisk gevinst:</b> {profit:.2f} kr<br><br>"
                            f"<b>Opladning flyttet til:</b><br>{later_text}</details>"
                        )
                    }
                    if ts in data["discharge_timestamps"]:
                        data["discharge_timestamps"].remove(ts)
                if blocked:
                    _LOGGER.debug("Optimize solar charging: day %s blocking solar charging at %s", day, ", ".join(ts.strftime("%H:%M") for ts in sorted(blocked)))

            charging_rules = [
                ("only_discharge_on_profit", self.enabled("only_discharge_on_profit"), only_discharge_on_profit),
                ("cheapest_hour_fill_planner", self.enabled("cheapest_hour_fill_planner"), cheapest_hour_fill_planner),
                ("most_expensive_planner", self.enabled("most_expensive_planner"), most_expensive_planner),
                ("needed_before_max_level_planner", self.enabled("needed_before_max_level_planner"), needed_before_max_level_planner),
                ("prioritize_discharge_hours_by_energy_cost", self.enabled("prioritize_discharge_hours_by_energy_cost"), prioritize_discharge_hours_by_energy_cost),
            ]
            sell_rules = [
                ("most_profit", self.enabled("most_profit"), most_profit),
                ("sell_excess_kwh_available", self.enabled("sell_excess_kwh_available"), sell_excess_kwh_available),
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
        overview_text = "\n".join(overview) if overview else "Ingen data"
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
            "New plan: %s charge hours (%.2f kWh, %.2f kr), %s sell periods, %s blocked discharge hours, %s blocked solar hours",
            len(charges), charge_hours.get("total_kwh", 0.0), charge_hours.get("total_cost", 0.0),
            len(sales), blocked, blocked_solar,
        )
        for ts in charges:
            rules = [key for key, value in charge_hours[ts].items() if value is True]
            _LOGGER.debug("  charge %s: %.2f kWh at %.2f kr/kWh (%s)", ts, charge_hours[ts]["kWh"], charge_hours[ts]["Price"], ", ".join(rules))
        for day in days:
            for ts, data in sorted(charging_plan[day]["force_discharge_timestamps"].items()):
                _LOGGER.debug("  sell %s: %.2f kWh, profit %.2f kr (%s)", ts, data["kwh"], data["profit"], data["title"])

    # ------------------------------------------------------------------ #
    # Overview markdown
    # ------------------------------------------------------------------ #

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

        try:
            sorted_charge_hours = sorted(
                ((k, v) for k, v in charge_hours.items() if isinstance(k, datetime.datetime)), key=lambda kv: kv[0]
            )
            merged_intervals = []
            has_combined = False
            current_interval = None
            for timestamp, value in sorted_charge_hours:
                if current_interval is not None and (
                    timestamp == current_interval["end"].replace(minute=0) + datetime.timedelta(hours=1)
                    and days_between(current_interval["start"], timestamp) == 0
                ):
                    has_combined = True
                    current_interval["end"] = timestamp
                    current_interval["type"] = join_unique_emojis(current_interval["type"], emoji_parse(value))
                    current_interval["percentage"] += value["battery_level"]
                    current_interval["kWh"] += round(value["kWh"], 2)
                    current_interval["cost"] += round(value["Cost"], 2)
                    continue
                if current_interval is not None:
                    merged_intervals.append(current_interval)
                current_interval = {
                    "start": timestamp,
                    "end": timestamp,
                    "type": emoji_parse(value),
                    "percentage": value["battery_level"],
                    "kWh": round(value["kWh"], 2),
                    "cost": round(value["Cost"], 2),
                    "unit": round(value["Price"], 2),
                }
            if current_interval:
                merged_intervals.append(current_interval)

            overview.append("<center>\n")
            overview.append(f"## 📖 {t('ui.cheap_grid_charge_hours.charging_overview_title')} ##")
            if merged_intervals:
                overview.append(f"|  | {t('ui.common.time')} | % | kWh | {valuta_kwh} | {t('ui.common.price')} |")
                overview.append("|---:|:---:|---:|---:|:---:|---:|")
                for d in merged_intervals:
                    if d["start"].strftime("%H") == d["end"].strftime("%H"):
                        time_range = d["start"].strftime("%d/%m %H:%M")
                    else:
                        time_range = f"{d['start'].strftime('%d/%m %H')}-{d['end'].strftime('%H:%M')}"
                    overview.append(
                        f"| {emoji_text_format(d['type'], group_size=3)} | **{time_range}** | **{int(round(d['percentage'], 0))}** | "
                        f"**{d['kWh']:.2f}** | **{d['unit']:.2f}** | **{d['cost']:.2f}** |"
                    )
                total_kwh = charge_hours["total_kwh"]
                unit_total = round(charge_hours["total_cost"] / total_kwh, 2) if total_kwh else 0.0
                total_text = (
                    f"{t('ui.common.total')} {int(round(charge_hours['total_procent'], 0))}% {total_kwh} kWh "
                    f"{charge_hours['total_cost']:.2f}{valuta} ({unit_total} {valuta_kwh})"
                )
                if has_combined:
                    overview.append(f"\n<details><summary><b>{total_text}</b></summary>")
                    overview.append(f"\n|  | {t('ui.common.time')} | % | kWh | {valuta_kwh} | {t('ui.common.price')} |")
                    overview.append("|---:|:---:|---:|---:|:---:|---:|")
                    for timestamp, value in sorted_charge_hours:
                        overview.append(
                            f"| {emoji_parse(value)} | **{timestamp.strftime('%d/%m %H:%M')}** | **{int(round(value['battery_level'], 0))}** | "
                            f"**{round(value['kWh'], 2):.2f}** | **{round(value['Price'], 2):.2f}** | **{round(value['Cost'], 2):.2f}** |"
                        )
                    overview.append("</details>\n")
                else:
                    overview.append(f"\n**{total_text}**")

                if self.grid_price_cache.get("using_offline_prices", False) and self.grid_price_cache.get("missing_hours"):
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
            else:
                overview.append(f"**{t('ui.cheap_grid_charge_hours.no_upcoming_charging_planned')}**")
            overview.append("</center>\n")
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Failed to create charging plan overview: %s", err)

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

        def join_timestamps_by_hour(timestamps: dict) -> dict:
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
                if len(periods) == 1:
                    joined[first["timestamp"]] = {k: first[k] for k in ("title", "emoji", "kwh", "profit", "reason")}
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
                        f"{period['kwh']:.2f} kWh / {period['profit']:.2f} kr</summary>{detail}</details>"
                    )
                joined[first["timestamp"]] = {
                    "title": first["title"],
                    "kwh": total_kwh,
                    "profit": total_profit,
                    "reason": (
                        f"<details><summary>{emoji_parse({'bundle': True})}{first['emoji']}{first['title']}<br>"
                        f"({total_kwh:.1f} kWh / {total_profit:.2f} kr)</summary>{''.join(period_details)}"
                        f"<br>**Total:** {total_kwh:.2f} kWh / {total_profit:.2f} kr</details>"
                    ),
                }
            return joined

        try:
            overview.append("<center>\n")
            overview.append("## 🔎 Hour rules ##")
            for day in days:
                joined = {}
                joined.update(charging_plan[day]["force_powerwall_max_hours_old_discharge_timestamps"])
                joined.update(charging_plan[day]["blocked_discharge_timestamps"])
                joined.update(charging_plan[day]["blocked_solar_charge_timestamps"])
                joined.update(join_timestamps_by_hour(charging_plan[day]["force_discharge_timestamps"]))
                joined.update(charging_plan[day]["charging_sessions"])

                overview.append(f"#### 🔎 Day {day} ({charging_plan[day]['start_of_day'].date().strftime('%d/%m')})\n")
                if not charging_plan[day]["charging_sessions"]:
                    overview.append(f"{t('ui.cheap_grid_charge_hours.no_charging_sessions')}<br>\n")
                if not charging_plan[day]["blocked_discharge_timestamps"]:
                    overview.append(f"{t('ui.cheap_grid_charge_hours.no_blocked_discharging_planned')}<br>\n")
                if self.enabled("sell_excess_kwh_available") and not charging_plan[day]["force_discharge_timestamps"]:
                    if "force_discharge_timestamps_empty" in charging_plan[day]:
                        overview.append(f"{charging_plan[day]['force_discharge_timestamps_empty']}<br>\n")
                if not joined:
                    continue
                overview.append(f"| {t('ui.common.time')} | {t('ui.common.reason')} |")
                overview.append("|:---|:---|")
                for timestamp, data in sorted(joined.items(), key=lambda kv: kv[0]):
                    reason = data["reason"] if isinstance(data, dict) and "reason" in data else data
                    overview.append(f"| **{timestamp.strftime('%H:%M')}** | {reason} |")
                overview.append("\n")
            overview.append("</center>\n")
            overview.append("***")
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Hour rules overview failed: %s", err)

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
