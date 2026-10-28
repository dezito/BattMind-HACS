"""Charging history queries and the cost of the energy in the battery."""
from __future__ import annotations

import logging

from ...utils.helpers import reset_time_to_hour

_LOGGER = logging.getLogger(__name__)


class HistoryQueries:
    """Charging history queries and the cost of the energy in the battery."""

    def charging_history(self, timestamp=None, save_db=True) -> dict:
        if not self.battery_expenses or save_db:
            self.current_battery_level_expenses()

        start = reset_time_to_hour(timestamp if timestamp is not None else self.now())
        end = start.replace(minute=59, second=59, microsecond=0)
        _LOGGER.debug("Calculating charging history for period: %s-%s", start, end.strftime("%H:%M:%S"))

        data = self.power_values(start, end)
        home_consumption_kwh = data["power_consumption"] / 1000.0
        charge_kwh = data["powerwall_charging_consumption"] / 1000.0
        discharge_kwh = data["powerwall_discharging_consumption"] / 1000.0
        solar_production_kwh = data["solar_production"] / 1000.0

        home_consumption_without_solar_kwh = max(home_consumption_kwh - solar_production_kwh, 0.0)
        grid_consumption_kwh = home_consumption_kwh - (discharge_kwh + solar_production_kwh)

        charge_grid_share_pct = max(grid_consumption_kwh / charge_kwh if charge_kwh > 0.0 else 0.0, 0.0)
        solar_charge_share_pct = max(solar_production_kwh / charge_kwh if charge_kwh > 0.0 else 0.0, 0.0)
        discharge_grid_share_pct = max(grid_consumption_kwh / discharge_kwh if discharge_kwh > 0.0 else 0.0, 0.0)
        solar_discharge_share_pct = max(
            solar_production_kwh / (discharge_kwh + home_consumption_kwh) if discharge_kwh > 0.0 else 0.0, 0.0
        )
        powerwall_discharge_share_pct = max(
            discharge_kwh / home_consumption_without_solar_kwh if home_consumption_without_solar_kwh > 0.0 else 0.0, 0.0
        )

        charge_share_pct_sum = charge_grid_share_pct + solar_charge_share_pct
        discharge_share_pct_sum = discharge_grid_share_pct + solar_discharge_share_pct + powerwall_discharge_share_pct
        charge_normalization_factor = charge_share_pct_sum if charge_share_pct_sum > 0.0 else 1.0
        charge_grid_share_pct /= charge_normalization_factor
        solar_charge_share_pct /= charge_normalization_factor
        discharge_normalization_factor = discharge_share_pct_sum if discharge_share_pct_sum > 0.0 else 1.0
        discharge_grid_share_pct /= discharge_normalization_factor
        solar_discharge_share_pct /= discharge_normalization_factor
        powerwall_discharge_share_pct /= discharge_normalization_factor

        buy_price = self.get_grid_prices().get(start)
        sell_price = self.get_sell_prices().get(start)
        powerwall_kwh_price = self.battery_expenses.get("unit_with_loss_and_wear")

        if buy_price is None or sell_price is None:
            raise ValueError(f"Buy or sell price not found for timestamp {start}. buy_price: {buy_price}, sell_price: {sell_price}")
        sell_price = self.sell_value(sell_price)
        if not isinstance(powerwall_kwh_price, (int, float)):
            powerwall_kwh_price = self.get_powerwall_kwh_price()

        charge_price = buy_price * charge_grid_share_pct + sell_price * solar_charge_share_pct
        discharge_price = (
            sell_price * discharge_grid_share_pct
            + sell_price * solar_discharge_share_pct
            + powerwall_kwh_price * powerwall_discharge_share_pct
        )
        charge_cost = charge_price * charge_kwh
        discharge_cost = discharge_price * discharge_kwh
        home_consumption_cost = home_consumption_without_solar_kwh * buy_price if discharge_kwh > 0.0 else 0.0
        sell_consumption_revenue = (discharge_kwh - home_consumption_without_solar_kwh) * sell_price if discharge_kwh > 0.0 else 0.0
        savings = (home_consumption_cost + sell_consumption_revenue) - (discharge_kwh * discharge_price)
        kwh_savings = savings / discharge_kwh if discharge_kwh != 0 else None

        kwh_from_local_energy = 0.0
        if charge_kwh > 0.0:
            try:
                _, kwh_from_local_energy = self.calc_local_energy_kwh(start, end, charge_kwh)
                kwh_from_local_energy = min(kwh_from_local_energy, charge_kwh)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Error calculating local energy kWh: %s", err)
                self.notify(
                    f"Charging history failed to calculate local energy kWh or price: {err} {type(err)}",
                    f"{self.title} failed",
                    f"{self.name}_charging_history_calc_local_energy_kwh_failed",
                )

        def _r(value, decimals=3):
            return round(value, decimals) if isinstance(value, (int, float)) else None

        session_dict = {
            "rules": dict(self.session_rules),
            "energy_summary": {
                "home_consumption_kwh": _r(home_consumption_kwh),
                "solar_production_kwh": _r(solar_production_kwh),
                "home_consumption_without_solar_kwh": _r(home_consumption_without_solar_kwh),
                "grid_consumption_kwh": _r(grid_consumption_kwh),
            },
            "prices": {
                "buy_price": _r(buy_price),
                "sell_price": _r(sell_price),
                "powerwall_kwh_price": _r(powerwall_kwh_price),
                "kwh_savings": _r(kwh_savings),
                "charge_price": _r(charge_price),
                "discharge_price": _r(discharge_price),
            },
            "charged": {
                "percentage": round(self.kwh_to_percentage(charge_kwh), 1),
                "kWh": round(charge_kwh, 3),
                "kWh_from_grid": round(max(charge_kwh - kwh_from_local_energy, 0.0), 3),
                "kWh_from_local_energy": round(kwh_from_local_energy, 3),
                "cost": _r(charge_cost),
                "price": _r(charge_price),
            },
            "discharged": {
                "percentage": round(self.kwh_to_percentage(discharge_kwh), 1),
                "kWh": round(discharge_kwh, 3),
                "cost": _r(discharge_cost),
                "price": _r(discharge_price),
                "savings": _r(savings),
            },
        }

        if charge_kwh > 0.0 or discharge_kwh > 0.0:
            _LOGGER.info(
                "Charging history %s: charged %.3f kWh, discharged %.3f kWh, charge cost %.2f, discharge cost %.2f",
                start, charge_kwh, discharge_kwh, charge_cost, discharge_cost,
            )
            self.charging_history_db[start] = session_dict
            self.mark_dirty("charging_history")

        if not save_db:
            return session_dict

        self.session_rules = {}
        try:
            self.charging_history_combine_and_set()
            self.save_charging_history()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error combining and setting charging history: %s", err)
            self.notify(
                f"Charging history failed to combine and set after adding new session: {err} {type(err)}",
                f"{self.title} failed",
                f"{self.name}_charging_history_combine_and_set_failed",
            )
        return session_dict

    def current_battery_level_expenses(self) -> dict:
        cfg = self.config
        self.battery_expenses = expenses = {
            "kwh": 0.0,
            "percentage": 0.0,
            "solar_percentage": 0.0,
            "cost": 0.0,
            "unit": None,
            "cost_loss": 0.0,
            "cost_wear": 0.0,
            "cost_with_loss_and_wear": 0.0,
            "unit_with_loss_and_wear": None,
            "loss_percentage": self.effective_round_trip_loss(),
        }
        try:
            current_battery_level = self.get_battery_level()
            if self.charging_history_db:
                for key in sorted(self.charging_history_db.keys(), reverse=True):
                    if round(expenses["percentage"]) >= round(current_battery_level):
                        break
                    charged = self.charging_history_db[key].get("charged", {})
                    if any(k not in charged for k in ("price", "cost", "kWh", "kWh_from_local_energy", "percentage")):
                        continue
                    if not isinstance(charged["price"], (int, float)):
                        continue
                    cost = charged["cost"] or 0.0
                    kwh = charged["kWh"]
                    percentage = charged["percentage"]
                    solar_percentage = self.kwh_to_percentage(charged["kWh_from_local_energy"])
                    cost_loss = self.calc_battery_loss_cost(cost)
                    total_cost = (cost + cost_loss) + abs(cfg["solar"]["powerwall_wear_cost_per_kwh"])

                    new_battery_level = percentage + expenses["percentage"]
                    if new_battery_level > current_battery_level and percentage > 0.0:
                        diff = (percentage - (new_battery_level - current_battery_level)) / percentage
                        cost *= diff
                        cost_loss *= diff
                        total_cost *= diff
                        kwh *= diff
                        percentage *= diff
                        solar_percentage *= diff

                    expenses[key] = charged
                    expenses["kwh"] += kwh
                    expenses["percentage"] += percentage
                    expenses["solar_percentage"] += solar_percentage
                    expenses["cost"] += cost
                    expenses["cost_loss"] += cost_loss
                    expenses["cost_wear"] += abs(cfg["solar"]["powerwall_wear_cost_per_kwh"]) * kwh
                    expenses["cost_with_loss_and_wear"] += total_cost

                expenses["kwh"] = round(expenses["kwh"], 3)
                expenses["percentage"] = round(expenses["percentage"], 1)
                expenses["solar_percentage"] = round(expenses["solar_percentage"], 1)
                expenses["cost"] = round(expenses["cost"], 3)
                expenses["cost_with_loss_and_wear"] = round(expenses["cost_with_loss_and_wear"], 3)
                if expenses["kwh"] > 0.0:
                    expenses["unit"] = round(expenses["cost"] / expenses["kwh"], 3)
                    expenses["unit_with_loss_and_wear"] = round(expenses["cost_with_loss_and_wear"] / expenses["kwh"], 3)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Error in battery level cost calculation: %s", err)
        return expenses
