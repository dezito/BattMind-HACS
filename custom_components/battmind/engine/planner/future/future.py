"""The future charging plan: all rules that add charge and discharge hours."""
from __future__ import annotations

import logging
from functools import partial

from .charge_rules import FutureChargeRules
from .context import FutureContext
from .discharge_rules import FutureDischargeRules
from .optimizer import FutureOptimizer
from .selling import FutureSelling
from .solar import FutureSolar
from .support import FutureSupport

_LOGGER = logging.getLogger(__name__)


class FutureCharging(FutureSupport, FutureChargeRules, FutureDischargeRules, FutureSelling, FutureSolar, FutureOptimizer):
    """The future charging plan: all rules that add charge and discharge hours."""

    def future_charging(self, amount_of_days, battery_expenses, battery_expenses_keys, calc_battery_loss_cost, charge_hours, charging_plan, config, current_hour, current_sell_price_timestamp, efficiency, forecast_type, get_battery_level, get_min_profit_per_kwh, grid_prices, grid_sell_prices, kwh_to_percentage, max_kwh_charging, max_kwh_discharging, name, now, percentage_to_kwh, sorted_by_cheapest_price, t, valuta, valuta_kwh, total_cost, total_kwh):
        totals = [total_cost, total_kwh]  # the running totals of the rules (updated by them)
        ctx = FutureContext(
            amount_of_days=amount_of_days,
            battery_expenses=battery_expenses,
            battery_expenses_keys=battery_expenses_keys,
            calc_battery_loss_cost=calc_battery_loss_cost,
            charge_hours=charge_hours,
            charging_plan=charging_plan,
            config=config,
            current_hour=current_hour,
            current_sell_price_timestamp=current_sell_price_timestamp,
            efficiency=efficiency,
            forecast_type=forecast_type,
            get_battery_level=get_battery_level,
            get_min_profit_per_kwh=get_min_profit_per_kwh,
            grid_prices=grid_prices,
            grid_sell_prices=grid_sell_prices,
            kwh_to_percentage=kwh_to_percentage,
            max_kwh_charging=max_kwh_charging,
            max_kwh_discharging=max_kwh_discharging,
            name=name,
            now=now,
            percentage_to_kwh=percentage_to_kwh,
            sorted_by_cheapest_price=sorted_by_cheapest_price,
            t=t,
            totals=totals,
            valuta=valuta,
            valuta_kwh=valuta_kwh,
        )

        charging_rules = [
            ("only_discharge_on_profit", self.enabled("only_discharge_on_profit"), partial(self.only_discharge_on_profit, ctx)),
            ("negative_price_planner", self.enabled("negative_price_planner"), partial(self.negative_price_planner, ctx)),
            ("cheapest_hour_fill_planner", self.enabled("cheapest_hour_fill_planner"), partial(self.cheapest_hour_fill_planner, ctx)),
            ("most_expensive_planner", self.enabled("most_expensive_planner"), partial(self.most_expensive_planner, ctx)),
            ("needed_before_max_level_planner", self.enabled("needed_before_max_level_planner"), partial(self.needed_before_max_level_planner, ctx)),
            ("prioritize_discharge_hours_by_energy_cost", self.enabled("prioritize_discharge_hours_by_energy_cost"), partial(self.prioritize_discharge_hours_by_energy_cost, ctx)),
        ]
        sell_rules = [
            ("discharge_before_solar", self.enabled("discharge_before_solar"), partial(self.discharge_before_solar, ctx)),
            ("most_profit", self.enabled("most_profit"), partial(self.most_profit, ctx)),
            ("sell_excess_kwh_available", self.enabled("sell_excess_kwh_available"), partial(self.sell_excess_kwh_available, ctx)),
            ("grid_charge_to_sell", self.enabled("grid_charge_to_sell"), partial(self.grid_charge_to_sell, ctx)),
            ("force_powerwall_max_hours_old_discharge", self.enabled("force_powerwall_max_hours_old_discharge"), partial(self.force_powerwall_max_hours_old_discharge, ctx)),
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
                    self.battery_level_flow_prediction_recalc(amount_of_days, charging_plan, config, current_hour, efficiency, forecast_type, get_battery_level, kwh_to_percentage, max_kwh_charging, max_kwh_discharging, now)

        if self.enabled("optimize_solar_charging"):
            for day in days:
                self.optimize_solar_charging(ctx, day)

        self.battery_level_flow_prediction_recalc(amount_of_days, charging_plan, config, current_hour, efficiency, forecast_type, get_battery_level, kwh_to_percentage, max_kwh_charging, max_kwh_discharging, now, final_recalc=True)
        for day in days:
            self.discharge_amount(ctx, day)

        # always calculated: it controls the battery, or it is compared with the rules
        self.optimizer_shadow = None
        self.shadow_plan_actions = None
        try:
            result = self.shadow_plan(ctx, days)
            if self.enabled("optimizer_control") and result is not None:
                total_cost, total_kwh = self.apply_optimizer_plan(ctx, days, *result)
                totals[:] = total_cost, total_kwh
                self.optimizer_shadow["controls"] = True
                self.battery_level_flow_prediction_recalc(amount_of_days, charging_plan, config, current_hour, efficiency, forecast_type, get_battery_level, kwh_to_percentage, max_kwh_charging, max_kwh_discharging, now, final_recalc=True)
                for day in days:
                    self.discharge_amount(ctx, day)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Whole period optimisation failed, the rules control the battery")
        return list(totals)
