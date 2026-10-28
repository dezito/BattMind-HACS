"""Helpers of the charging plan: solar, available kWh and adding charge hours."""
from __future__ import annotations

import logging

from ...utils.helpers import days_between, hour_in

_LOGGER = logging.getLogger(__name__)


class ChargingHours:
    """Helpers of the charging plan: solar, available kWh and adding charge hours."""

    def solar_surplus_in_hour(self, charging_plan, current_hour, timestamp):
        """Predicted solar surplus (after the house consumption) in this hour, also when it is sold."""
        if not self.enabled("solar_charging"):
            return 0.0
        data = charging_plan.get(days_between(current_hour, timestamp))
        if data is None:
            return 0.0
        solar = data["solar_kwh_prediction"]
        return solar[timestamp.hour] if timestamp.hour < len(solar) else 0.0

    def solar_kwh_in_hour(self, charging_plan, current_hour, max_kwh_charging, timestamp):
        """Predicted solar surplus charging the battery in this hour (0 when blocked/disabled)."""
        data = charging_plan.get(days_between(current_hour, timestamp))
        if data is None or hour_in(timestamp, data["blocked_solar_charge_timestamps"]):
            return 0.0
        return min(self.solar_surplus_in_hour(charging_plan, current_hour, timestamp), max_kwh_charging)

    def kwh_available_in_hour(self, charge_hours, charging_plan, current_hour, max_kwh_charging, hour):
        """Grid charging capacity left in the hour - solar surplus charges first."""
        hour_in_charge_hours = False
        kwh_available = max_kwh_charging
        try:
            kwh = max_kwh_charging
            kwh -= kwh * (hour.minute / 60)
            kwh_available = max(kwh - self.solar_kwh_in_hour(charging_plan, current_hour, max_kwh_charging, hour), 0.0)
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

    def add_to_charge_hours(self, charge_hours, config, efficiency, kwh_to_percentage, max_kwh_charging, name, percentage_to_kwh, valuta_kwh, kwh_needed, total_cost, total_kwh, hour, price, kwh_available, battery_level=None, rules=None):
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
