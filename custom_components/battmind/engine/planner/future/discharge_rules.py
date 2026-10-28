"""The rules that choose when the battery discharges."""
from __future__ import annotations

import datetime
import logging

from ....utils.formatting import emoji_parse
from ....utils.helpers import average, hour_in, reset_time_to_hour

_LOGGER = logging.getLogger(__name__)


class FutureDischargeRules:
    """Discharge amount, discharge on profit, energy cost priority, powerwall max and discharge before solar."""

    def discharge_amount(self, ctx, day):
        discharge_kwh = 0.0
        last_battery_level = 0.0
        for hour in ctx.charging_plan[day]["battery_level_flow"]:
            if ctx.charging_plan[day]["battery_level_flow"][hour]:
                level = sum(ctx.charging_plan[day]["battery_level_flow"][hour])
                if level < last_battery_level:
                    discharge_kwh += ctx.percentage_to_kwh(last_battery_level - level)
                last_battery_level = level
        ctx.charging_plan[day]["discharge_kwh"] = discharge_kwh

    def only_discharge_on_profit(self, ctx, day):
        for hour in range(ctx.current_hour.hour if day == 0 else 0, 24):
            if hour not in ctx.charging_plan[day]["battery_level_flow"]:
                continue
            if hour not in ctx.charging_plan[day]["hour_cost_prediction"][ctx.forecast_type]:
                continue
            timestamp = ctx.charging_plan[day]["start_of_day"] + datetime.timedelta(hours=hour)
            if timestamp not in ctx.charging_plan[day]["discharge_timestamps"]:
                continue
            price = ctx.grid_prices.get(timestamp)
            if price is None:
                continue
            raw, loss_cost, battery_kwh_cost = self._get_predicted_battery_cost(ctx, day, hour)
            min_profit_per_kwh = ctx.get_min_profit_per_kwh()
            profit = price - battery_kwh_cost
            # per kWh only: min_total_profit is for trades (a charge hour / a sale) - not using the
            # battery for the house earns nothing, so it must not block small hours
            if profit < min_profit_per_kwh:
                _LOGGER.debug(
                    "Only discharge on profit: blocking %s (price %.2f - battery cost %.2f = %.2f < %.2f)",
                    timestamp, price, battery_kwh_cost, profit, min_profit_per_kwh,
                )
                ctx.charging_plan[day]["discharge_timestamps"].remove(timestamp)
                if sum(ctx.charging_plan[day]["battery_level_flow"][hour]) <= ctx.config["solar"]["powerwall_battery_level_min"]:
                    continue
                ctx.charging_plan[day]["battery_level_flow"][hour].append(
                    ctx.charging_plan[day]["hour_cost_prediction"][ctx.forecast_type][hour]["percentage"]
                )
                ctx.charging_plan[day]["blocked_discharge_timestamps"][timestamp] = (
                    f"<details><summary>{emoji_parse({'only_discharge_on_profit': True})}{ctx.r('title.no_profit')} ({profit:.2f})</summary>"
                    f"{ctx.r('battery_level')}: **{sum(ctx.charging_plan[day]['battery_level_flow'][hour]):.1f}%**<br>"
                    f"{ctx.r('battery_cost_basis')}: **{raw:.2f} {ctx.valuta_kwh}**<br>"
                    f"{ctx.r('loss_cost')}: **{loss_cost:.2f} {ctx.valuta_kwh}**<br>"
                    f"{ctx.r('wear_cost')}: **{abs(ctx.config['solar']['powerwall_wear_cost_per_kwh']):.2f} {ctx.valuta_kwh}**<br>"
                    f"**{ctx.r('battery_cost_total')}**: **{battery_kwh_cost:.2f} {ctx.valuta_kwh}**<br>"
                    f"{ctx.r('current_price')}: **{price} {ctx.valuta_kwh}**<br>"
                    f"{ctx.r('min_profit_per_kwh')}: **{min_profit_per_kwh:.2f} {ctx.valuta_kwh}**<br>"
                    f"{ctx.r('profit_per_kwh')}: **{profit:.2f} {ctx.valuta_kwh}**<br>"
                    "</details>"
                )

    def prioritize_discharge_hours_by_energy_cost(self, ctx, day):
        powerwall_kwh_min = ctx.percentage_to_kwh(ctx.config["solar"]["powerwall_battery_level_min"])
        highest_battery_level = ctx.get_battery_level() if day == 0 else ctx.config["solar"]["powerwall_battery_level_min"]
        highest_battery_level_timestamp = ctx.now if day == 0 else ctx.charging_plan[day]["start_of_day"]

        for hour in ctx.charging_plan[day]["hour_cost_prediction"][ctx.forecast_type].keys():
            battery_level = sum(ctx.charging_plan[day]["battery_level_flow"].get(hour, [0.0]))
            if battery_level > highest_battery_level:
                highest_battery_level = battery_level
                highest_battery_level_timestamp = ctx.current_hour.replace(hour=hour) + datetime.timedelta(days=day)

        # the battery only has to last until it is charged again (solar or grid)
        recharge_timestamp = ctx.charging_plan[day]["end_of_day"]
        for hour in range(highest_battery_level_timestamp.hour + 1, 24):
            flow = ctx.charging_plan[day]["battery_level_flow"].get(hour)
            if flow and sum(flow) > flow[0] + 0.01:
                recharge_timestamp = ctx.charging_plan[day]["start_of_day"].replace(hour=hour)
                break

        highest_battery_kwh = ctx.percentage_to_kwh(highest_battery_level)
        battery_kwh = highest_battery_kwh
        prioritized_hours = []
        prioritized_kwh = []

        for hour in ctx.charging_plan[day]["sorted_hour_cost_prediction"][ctx.forecast_type]:
            timestamp = ctx.current_hour.replace(hour=hour) + datetime.timedelta(days=day)
            if timestamp not in ctx.charging_plan[day]["discharge_timestamps"]:
                continue
            if timestamp < highest_battery_level_timestamp or timestamp >= recharge_timestamp:
                continue
            if self.solar_surplus_in_hour(ctx.charging_plan, ctx.current_hour, timestamp) > 0.0:
                continue
            kwh_needed = ctx.charging_plan[day]["hour_cost_prediction"][ctx.forecast_type][hour]["kwh"] / ctx.efficiency
            battery_kwh = max(battery_kwh - kwh_needed, powerwall_kwh_min)

            if battery_kwh <= powerwall_kwh_min:
                if prioritized_hours and hour > max(prioritized_hours):
                    continue
                ctx.charging_plan[day]["discharge_timestamps"].remove(timestamp)
                prioritized_hours_string = ", ".join(f"{h:02d}:00" for h in prioritized_hours)
                prioritized_kwh_string = (
                    f"{', '.join(f'{ctx.kwh_to_percentage(k):.0f}%' for k in prioritized_kwh)} "
                    f"({ctx.kwh_to_percentage(sum(prioritized_kwh)):.1f}%)"
                )
                ctx.charging_plan[day]["blocked_discharge_timestamps"][timestamp] = (
                    f"<details><summary>{emoji_parse({'prioritize_discharge_hours_by_energy_cost': True})}{ctx.r('title.most_expensive_prioritized')} "
                    f"({prioritized_hours_string.replace(' ', '')})</summary>"
                    f"{ctx.r('reaches_minimum')}<br>"
                    f"{ctx.r('prioritized_hours')}: **{prioritized_hours_string}**<br>"
                    f"{ctx.r('prioritized_kwh')}: **{prioritized_kwh_string}**<br>"
                    f"{ctx.r('highest_level_time')}: **{highest_battery_level_timestamp}**<br>"
                    f"{ctx.r('highest_level')}: **{ctx.kwh_to_percentage(highest_battery_kwh):.1f}%**<br>"
                    f"{ctx.r('current_level')}: **{ctx.kwh_to_percentage(battery_kwh):.1f}%**<br>"
                    f"{ctx.r('lowest_allowed_level')}: **{ctx.kwh_to_percentage(powerwall_kwh_min):.1f}%**<br>"
                    f"{ctx.r('needed_for_discharge')}: **{ctx.kwh_to_percentage(kwh_needed):.1f}%**<br>"
                    "</details>"
                )
            else:
                prioritized_hours.append(hour)
                prioritized_kwh.append(kwh_needed)

    def force_powerwall_max_hours_old_discharge(self, ctx, day):
        """Energy older than powerwall_max_hours_old is used even without profit.

        When selling it in the most expensive sell periods pays more than using it (by
        min_profit_per_kwh / min_total_profit, at least min_sell_kwh), it is sold instead.
        """
        max_hours_old = self.number("powerwall_max_hours_old")
        max_days_old = max(int(round(max_hours_old / 24)), 0)
        if day > max_days_old:
            _LOGGER.debug("Old energy: day %s exceeds the max age (%s days), skipping", day, max_days_old)
            return ctx.battery_expenses_keys
        minimum = ctx.config["solar"]["powerwall_battery_level_min"]
        usage_minimum = max(minimum, self.number("usage_reserve"))
        from_timestamp = ctx.charging_plan[day]["start_of_day"].replace(hour=ctx.current_hour.hour if day == 0 else 0)
        old_keys = [
            key for key in ctx.battery_expenses_keys
            if reset_time_to_hour(key) + datetime.timedelta(hours=max_hours_old) <= ctx.charging_plan[day]["end_of_day"]
        ]
        if not old_keys:
            return ctx.battery_expenses_keys
        old_from = max(min(reset_time_to_hour(key) + datetime.timedelta(hours=max_hours_old) for key in old_keys), from_timestamp)
        old_kwh_total = sum(ctx.battery_expenses[key]["kWh"] for key in old_keys)
        old_price = sum(ctx.battery_expenses[key]["price"] * ctx.battery_expenses[key]["kWh"] for key in old_keys) / old_kwh_total if old_kwh_total > 0.0 else 0.0
        old_flow = ctx.charging_plan[day]["battery_level_flow"].get(old_from.hour)
        old_kwh = min(old_kwh_total, ctx.percentage_to_kwh(max((old_flow or [minimum])[0] - minimum, 0.0)) * ctx.efficiency)
        battery_loss_cost = ctx.calc_battery_loss_cost(old_price)
        battery_kwh_cost = old_price + battery_loss_cost + abs(ctx.config["solar"]["powerwall_wear_cost_per_kwh"])

        # use it: allow discharging in every hour from when the energy is too old
        usage_prices = []
        for hour in range(old_from.hour, 24):
            loop_hour = ctx.charging_plan[day]["start_of_day"].replace(hour=hour)
            if hour_in(loop_hour, ctx.charging_plan[day]["charging_sessions"]):
                continue
            if hour_in(loop_hour, ctx.charging_plan[day]["force_discharge_timestamps"]):
                continue
            price = ctx.grid_prices.get(loop_hour, old_price)
            usage_prices.append(price)
            ctx.charging_plan[day]["blocked_discharge_timestamps"].pop(loop_hour, None)
            if loop_hour in ctx.charging_plan[day]["discharge_timestamps"]:
                continue
            _LOGGER.debug("Old energy: allowing discharge at %s (energy older than %s hours)", loop_hour, max_hours_old)
            ctx.charging_plan[day]["discharge_timestamps"].append(loop_hour)
            profit = price - old_price
            ctx.charging_plan[day]["force_powerwall_max_hours_old_discharge_timestamps"][loop_hour] = {
                "kwh": old_kwh,
                "profit": profit,
                "reason": (
                    f"<details><summary>{emoji_parse({'force_powerwall_max_hours_old_discharge': True})}{ctx.r('title.old_energy_used')} ({profit:.2f})</summary>"
                    f"{ctx.r('battery_cost_basis')}: **{old_price:.2f} {ctx.valuta_kwh}**<br>"
                    f"{ctx.r('loss_cost')}: **{battery_loss_cost:.2f} {ctx.valuta_kwh}**<br>"
                    f"{ctx.r('wear_cost')}: **{abs(ctx.config['solar']['powerwall_wear_cost_per_kwh']):.2f} {ctx.valuta_kwh}**<br>"
                    f"**{ctx.r('battery_cost_total')}**: **{battery_kwh_cost:.2f} {ctx.valuta_kwh}**<br>"
                    f"{ctx.r('grid_buy_price')}: **{price:.2f} {ctx.valuta_kwh}**<br>"
                    f"{ctx.r('profit_using_battery')}: **{profit:.2f} {ctx.valuta}**<br>"
                    "</details>"
                ),
            }

        # or sell it in the most expensive periods when that pays more than using it
        usage_value = average(usage_prices) if usage_prices else 0.0
        exclude_hours = self.get_exclude_sell_hours()
        sell_prices, sell_price, using_grid_sell_price = self.sell_prices_for_window(ctx, 
            max(old_from, ctx.current_sell_price_timestamp), ctx.charging_plan[day]["end_of_day"]
        )
        for timestamp, price in sorted(sell_prices.items(), key=lambda kv: (kv[1], kv[0]), reverse=True):
            gain_per_kwh = price - usage_value
            if old_kwh <= 0.0 or gain_per_kwh < max(ctx.get_min_profit_per_kwh(), 0.01):
                break
            if timestamp.hour in exclude_hours or timestamp in ctx.charging_plan[day]["force_discharge_timestamps"]:
                continue
            if hour_in(timestamp, ctx.charging_plan[day]["charging_sessions"]):
                continue
            flow = ctx.charging_plan[day]["battery_level_flow"].get(timestamp.hour)
            if not flow:
                continue
            periods_in_hour = self.price_provider.sell_periods_in_hour(timestamp)
            kwh = min(old_kwh, abs(ctx.max_kwh_discharging) / periods_in_hour, ctx.percentage_to_kwh(max(sum(flow) - usage_minimum, 0.0)) * ctx.efficiency)
            if kwh < self.number("min_sell_kwh") / periods_in_hour or gain_per_kwh * kwh < self.get_min_total_profit() / periods_in_hour:
                continue
            old_kwh -= kwh
            _LOGGER.debug("Old energy: selling %.2f kWh at %s (%.2f %s more than using it)", kwh, timestamp, gain_per_kwh * kwh, ctx.valuta)
            fixed_price_calc_text = (
                f"{sell_price:.2f} - {ctx.grid_sell_prices.get(timestamp, 0.0):.2f} =" if not using_grid_sell_price else ""
            )
            ctx.charging_plan[day]["force_discharge_timestamps"][timestamp] = {
                "title": ctx.r("title.old_energy_sold"),
                "emoji": emoji_parse({"force_powerwall_max_hours_old_discharge": True}),
                "kwh": kwh,
                "profit": (price - battery_kwh_cost) * kwh,
                "reason": (
                    f"<details><summary>{emoji_parse({'force_powerwall_max_hours_old_discharge': True})}{ctx.r('title.old_energy_sold')}<br>"
                    f"({kwh:.1f} kWh / +{gain_per_kwh * kwh:.2f} {ctx.valuta})</summary>"
                    f"**{ctx.r('battery_cost_total')}**: **{battery_kwh_cost:.2f} {ctx.valuta_kwh}**<br>"
                    f"{ctx.r('grid_sell_price')}: **{fixed_price_calc_text}{price:.2f} {ctx.valuta_kwh}**<br>"
                    f"{ctx.r('avg_price_use')}: **{usage_value:.2f} {ctx.valuta_kwh}**<br>"
                    f"{ctx.r('kwh_sold')}: **{kwh:.2f} kWh**<br>"
                    f"{ctx.r('gain_vs_use')}: **{gain_per_kwh * kwh:.2f} {ctx.valuta}**<br>"
                    "</details>"
                ),
            }

        for key in old_keys:
            ctx.battery_expenses_keys.remove(key)
        return ctx.battery_expenses_keys

    def discharge_before_solar(self, ctx, day):
        """Use/sell battery energy before the sun when the battery gets full of solar anyway.

        Every kWh emptied before the sun is refilled with solar surplus that would otherwise be
        sold, so a kWh used (buy price) or sold (sell price) has to beat that solar sell price
        incl. loss and wear by min_profit_per_kwh (sales also min_sell_kwh / min_total_profit).
        """
        minimum = ctx.config["solar"]["powerwall_battery_level_min"]
        maximum = ctx.config["solar"]["powerwall_battery_level_max"]
        data = ctx.charging_plan[day]
        start = data["start_of_day"]
        wear = abs(ctx.config["solar"]["powerwall_wear_cost_per_kwh"])
        hours = [hour for hour in range(ctx.now.hour if day == 0 else 0, 24) if data["battery_level_flow"].get(hour)]
        solar_hours = [hour for hour in hours if self.solar_surplus_in_hour(ctx.charging_plan, ctx.current_hour, start.replace(hour=hour)) > 0.0]
        if not solar_hours:
            return

        # solar surplus pushed out of the full battery (battery %) and what it is sold for
        pushed_out, pushed_out_value = 0.0, 0.0
        for hour in solar_hours:
            timestamp = start.replace(hour=hour)
            if hour_in(timestamp, data["blocked_solar_charge_timestamps"]) or hour_in(timestamp, data["force_discharge_timestamps"]):
                continue
            flow = data["battery_level_flow"][hour]
            stored = ctx.kwh_to_percentage(self.solar_kwh_in_hour(ctx.charging_plan, ctx.current_hour, ctx.max_kwh_charging, timestamp)) * ctx.efficiency
            grid = data["charging_sessions"].get(timestamp, {}).get("battery_level", 0.0)
            pushed = max(stored + grid - (sum(flow) - flow[0]), 0.0) if sum(flow) >= maximum - 0.01 else 0.0
            pushed_out += pushed
            pushed_out_value += pushed * self.sell_price_in_hour(ctx, timestamp)
        if pushed_out <= 0.01:
            return
        solar_sell_price = pushed_out_value / pushed_out
        # 1 kWh out of the battery takes 1/efficiency, refilling that takes 1/efficiencyÂ² of solar
        refill_cost = solar_sell_price / ctx.efficiency**2 + wear

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
            ctx.percentage_to_kwh(pushed_out),
            min(ctx.percentage_to_kwh(max(sum(data["battery_level_flow"][hour]) - minimum, 0.0)) for hour in window),
        ) * ctx.efficiency
        usage_minimum = max(minimum, self.number("usage_reserve"))
        sell_budget_kwh = min(
            ctx.percentage_to_kwh(pushed_out),
            min(ctx.percentage_to_kwh(max(sum(data["battery_level_flow"][hour]) - usage_minimum, 0.0)) for hour in window),
        ) * ctx.efficiency

        candidates = []
        for hour in window:
            timestamp = start.replace(hour=hour)
            prediction = data["hour_cost_prediction"][ctx.forecast_type].get(hour)
            price = ctx.grid_prices.get(timestamp)
            if timestamp not in data["discharge_timestamps"] and prediction and prediction["kwh"] > 0.0 and price is not None:
                candidates.append((price, timestamp, "use", prediction["kwh"], 1))
        exclude_hours = self.get_exclude_sell_hours()
        sell_prices, sell_price, using_grid_sell_price = self.sell_prices_for_window(ctx, 
            max(start.replace(hour=window[0]), ctx.current_sell_price_timestamp), start.replace(hour=window[-1]) + datetime.timedelta(hours=1)
        )
        for timestamp, price in sell_prices.items():
            if timestamp.hour in exclude_hours or timestamp in data["force_discharge_timestamps"]:
                continue
            periods_in_hour = self.price_provider.sell_periods_in_hour(timestamp)
            slot_kwh = abs(ctx.max_kwh_discharging) / periods_in_hour
            if reset_time_to_hour(timestamp) in data["discharge_timestamps"]:
                slot_kwh -= data["hour_cost_prediction"][ctx.forecast_type].get(timestamp.hour, {}).get("kwh", 0.0) / periods_in_hour
            candidates.append((price, timestamp, "sell", slot_kwh, periods_in_hour))

        for price, timestamp, kind, kwh, periods_in_hour in sorted(candidates, key=lambda item: (item[0], item[1]), reverse=True):
            gain = price - refill_cost
            if budget_kwh <= 0.01 or gain < max(ctx.get_min_profit_per_kwh(), 0.01):
                break
            if kind == "use":
                # discharge_allowed covers the whole hour, so the budget has to cover all of it
                if kwh > budget_kwh or not self.is_profitable(ctx, gain, kwh):
                    continue
                self.allow_discharge(ctx, timestamp)
                data.setdefault("discharge_before_solar_timestamps", {})[timestamp] = gain * kwh
                _LOGGER.debug("Discharge before solar: using %.2f kWh at %s (%.2f %s better than selling the solar)", kwh, timestamp, gain * kwh, ctx.valuta)
            else:
                kwh = min(kwh, budget_kwh, sell_budget_kwh)
                if kwh < self.number("min_sell_kwh") / periods_in_hour or not self.is_profitable(ctx, gain, kwh, periods_in_hour):
                    continue
                fixed_price_calc_text = (
                    f"{sell_price:.2f} - {ctx.grid_sell_prices.get(timestamp, 0.0):.2f} =" if not using_grid_sell_price else ""
                )
                data["force_discharge_timestamps"][timestamp] = {
                    "title": ctx.r("title.sell_before_solar"),
                    "emoji": emoji_parse({"discharge_before_solar": True}),
                    "kwh": kwh,
                    "profit": gain * kwh,
                    "reason": (
                        f"<details><summary>{emoji_parse({'discharge_before_solar': True})}{ctx.r('title.sell_before_solar')}<br>"
                        f"({kwh:.1f} kWh / +{gain * kwh:.2f} {ctx.valuta})</summary>"
                        f"{ctx.r('grid_sell_price')}: **{fixed_price_calc_text}{price:.2f} {ctx.valuta_kwh}**<br>"
                        f"{ctx.r('solar_sold_otherwise')}: **{solar_sell_price:.2f} {ctx.valuta_kwh}**<br>"
                        f"{ctx.r('refill_cost')}: **{refill_cost:.2f} {ctx.valuta_kwh}**<br>"
                        f"{ctx.r('solar_not_fitting')}: **{ctx.percentage_to_kwh(pushed_out) / ctx.efficiency:.2f} kWh**<br>"
                        f"{ctx.r('gain')}: **{gain * kwh:.2f} {ctx.valuta}**<br>"
                        "</details>"
                    ),
                }
            budget_kwh -= kwh
            sell_budget_kwh = max(sell_budget_kwh - kwh, 0.0)
