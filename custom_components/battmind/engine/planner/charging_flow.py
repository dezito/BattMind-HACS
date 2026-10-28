"""Prediction of the battery level flow."""
from __future__ import annotations

import datetime
import logging

from ...utils.helpers import days_between, hour_in, hours_between, start_of_day

_LOGGER = logging.getLogger(__name__)


class ChargingFlow:
    """Prediction of the battery level flow."""

    def battery_level_flow_prediction(self, charging_plan, config, efficiency, forecast_type, get_battery_level, kwh_to_percentage, max_kwh_charging, max_kwh_discharging, now, day, from_hour, reset_hour=False):
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

    def battery_level_flow_prediction_recalc(self, amount_of_days, charging_plan, config, current_hour, efficiency, forecast_type, get_battery_level, kwh_to_percentage, max_kwh_charging, max_kwh_discharging, now, final_recalc=False):
        for day in range(amount_of_days):
            self.battery_level_flow_prediction(charging_plan, config, efficiency, forecast_type, get_battery_level, kwh_to_percentage, max_kwh_charging, max_kwh_discharging, now, day, now.hour if day == 0 else 0, reset_hour=True)

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
