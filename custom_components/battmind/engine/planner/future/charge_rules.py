"""The rules that add charge hours."""
from __future__ import annotations

import datetime
import logging

from ....utils.formatting import emoji_parse
from ....utils.helpers import days_between, hour_in, in_between, reset_time_to_hour, reverse_list, round_up

_LOGGER = logging.getLogger(__name__)


class FutureChargeRules:
    """Cheapest hours, most expensive hours, needed before max level, negative prices and grid charge to sell."""

    def cheapest_hour_fill_planner(self, ctx, day):
        total_cost, total_kwh = ctx.totals
        finished = False
        day_prices = {}
        timestamps = []
        remove_list = []
        ignore_price_diff = self.number("cheap_price_period_rise_threshold")

        from_timestamp, to_timestamp = self.charging_window(ctx, day)
        for timestamp, price in ctx.grid_prices.items():
            if not in_between(timestamp, from_timestamp, to_timestamp) or timestamp < ctx.current_hour:
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
                loop_price = ctx.grid_prices.get(loop_timestamp)
                if loop_price is None:
                    continue
                if loop_price - price <= ignore_price_diff:
                    data.append(loop_timestamp)
                else:
                    last_timestamp = data.pop(-1) if data else timestamp
                    timestamp = last_timestamp
                    hour_in_charge_hours, kwh_available = self.kwh_available_in_hour(ctx.charge_hours, ctx.charging_plan, ctx.current_hour, ctx.max_kwh_charging, last_timestamp)
                    if hour_in_charge_hours and kwh_available <= 0.0:
                        if last_timestamp not in ctx.charge_hours:
                            remove_list.append(last_timestamp)
                        for item in reverse_list(data):
                            last_timestamp = item
                            hour_in_charge_hours, kwh_available = self.kwh_available_in_hour(ctx.charge_hours, ctx.charging_plan, ctx.current_hour, ctx.max_kwh_charging, last_timestamp)
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

        highest_battery_level = ctx.config["solar"]["powerwall_battery_level_min"]
        highest_battery_level_timestamp = ctx.charging_plan[day]["start_of_day"]
        for hour in range(0, 24):
            if hour not in ctx.charging_plan[day]["battery_level_flow"]:
                continue
            battery_level = sum(ctx.charging_plan[day]["battery_level_flow"].get(hour, [0.0]))
            if battery_level > highest_battery_level:
                highest_battery_level = battery_level
                highest_battery_level_timestamp = ctx.charging_plan[day]["start_of_day"].replace(hour=hour)

        if self.enabled("cheapest_hour_fill_up"):
            percentage_needed_today = [ctx.config["solar"]["powerwall_battery_level_max"]]
        else:
            percentage_needed_today = [
                ctx.charging_plan[day]["hour_cost_prediction"][ctx.forecast_type][hour]["percentage"]
                for hour in range(ctx.now.hour if day == 0 else 0, 24)
            ]

        # grid kWh incl. the charging loss
        kwh_needed = self.kwh_needed_for_charging(
            min(sum(percentage_needed_today), ctx.config["solar"]["powerwall_battery_level_max"]), highest_battery_level
        ) / ctx.efficiency
        if kwh_needed <= 0.0:
            return

        for cheap_timestamp in cheap_timestamps:
            for i in range(int(round_up(kwh_needed / ctx.max_kwh_charging) + 1)):
                if finished:
                    break
                timestamp = cheap_timestamp - datetime.timedelta(hours=i)
                price = ctx.grid_prices.get(timestamp)
                if price is None or timestamp < ctx.current_hour:
                    continue
                what_day = days_between(ctx.current_hour, timestamp)
                if what_day < day - 1 or what_day not in ctx.charging_plan:
                    continue
                if cheapest_price is not None and price - cheapest_price > cheapest_price_rise_threshold:
                    continue

                # the hours of the day (after the charge hour) where using the charged kWh pays off
                kwh_with_profit = []
                profit_with_kwh = []
                for hour in ctx.charging_plan[day]["hour_cost_prediction"][ctx.forecast_type]:
                    loop_timestamp = ctx.charging_plan[day]["start_of_day"].replace(hour=hour)
                    loop_price = ctx.grid_prices.get(loop_timestamp)
                    if loop_price is None or loop_timestamp <= timestamp or self.solar_surplus_in_hour(ctx.charging_plan, ctx.current_hour, loop_timestamp) > 0.0:
                        continue
                    profit = self.charge_kwh_profit(ctx, price, loop_price)
                    if profit < ctx.get_min_profit_per_kwh():
                        continue
                    kwh_with_profit.append(ctx.charging_plan[day]["hour_cost_prediction"][ctx.forecast_type][hour]["kwh"] / ctx.efficiency**2)
                    profit_with_kwh.append(profit * kwh_with_profit[-1])

                hour_in_charge_hours, kwh_available = self.kwh_available_in_hour(ctx.charge_hours, ctx.charging_plan, ctx.current_hour, ctx.max_kwh_charging, timestamp)
                if hour_in_charge_hours and kwh_available <= 0.0:
                    continue
                full_charge_kwh_needed = ctx.percentage_to_kwh(
                    ctx.config["solar"]["powerwall_battery_level_max"] - ctx.config["solar"]["powerwall_battery_level_min"]
                ) / ctx.efficiency
                kwh_available = min(sum(kwh_with_profit), self.grid_headroom_kwh(ctx, timestamp, price), full_charge_kwh_needed, kwh_available)
                if kwh_available <= 0.0:
                    continue
                average_profit = sum(profit_with_kwh) / sum(kwh_with_profit)
                if not self.is_profitable(ctx, average_profit, min(kwh_available, kwh_needed)):
                    _LOGGER.debug(
                        "Cheapest hour fill: skipping %s (%.2f %s x %.2f kWh below the minimum profit)",
                        timestamp, average_profit, ctx.valuta_kwh, min(kwh_available, kwh_needed),
                    )
                    continue

                if round(kwh_needed, 1) > 0.0 and ctx.kwh_to_percentage(kwh_needed) > 0.0:
                    kwh_needed, total_cost, total_kwh, battery_level_added, cost_added = self.add_to_charge_hours(ctx.charge_hours, ctx.config, ctx.efficiency, ctx.kwh_to_percentage, ctx.max_kwh_charging, ctx.name, ctx.percentage_to_kwh, ctx.valuta_kwh, 
                        kwh_needed, total_cost, total_kwh, timestamp, price, kwh_available,
                        rules=["cheapest_hour_fill_planner"],
                    )
                    ctx.totals[:] = total_cost, total_kwh
                    if timestamp in ctx.charge_hours and battery_level_added:
                        ctx.charging_plan[day]["total_cost"] += cost_added
                        reason = (
                            f"<details><summary>{emoji_parse({'charging': True})}{ctx.r('title.cheapest_hours')} ({price}/{battery_level_added:.0f}%)"
                            f"{self.other_day_text(ctx, day, what_day)}</summary>"
                            f"{ctx.r('highest_level_time')}: **{highest_battery_level_timestamp.strftime('%H:%M')}**<br>"
                            f"{ctx.r('highest_level')}: **{highest_battery_level:.1f}%**<br>"
                            f"{ctx.r('current_price')}: **{price}** {ctx.valuta_kwh}<br>"
                            f"{ctx.r('avg_profit_use')}: **{average_profit:.2f} {ctx.valuta_kwh}**<br>"
                            f"{ctx.r('level_added')}: **{battery_level_added:.2f}%**<br>"
                            "</details>"
                        )
                        self.add_charging_session_to_day(ctx, timestamp, what_day, reason=reason)
                        self.add_charging_to_days(ctx, timestamp)
                else:
                    finished = True

    def most_expensive_planner(self, ctx, day):
        total_cost, total_kwh = ctx.totals
        for sorted_hour in ctx.charging_plan[day]["sorted_hour_cost_prediction"][ctx.forecast_type]:
            sorted_timestamp = ctx.current_hour.replace(hour=sorted_hour) + datetime.timedelta(days=day)
            if sorted_timestamp <= ctx.current_hour:
                continue
            sorted_price = ctx.charging_plan[day]["hour_cost_prediction"][ctx.forecast_type][sorted_hour]["price"]
            sorted_battery_level = (ctx.charging_plan[day]["battery_level_flow"].get(sorted_hour) or [0.0])[0]
            # only the part of the hour the battery (incl. solar) doesn't cover already
            sorted_kwh_needed = self.uncovered_kwh(ctx, day, sorted_hour)
            if round(sorted_kwh_needed, 1) <= 0.0:
                continue

            remove_list = []
            from_timestamp, to_timestamp = self.charging_window(ctx, day)

            for timestamp, price in ctx.sorted_by_cheapest_price:
                if not in_between(timestamp, from_timestamp, to_timestamp) or timestamp < ctx.current_hour:
                    continue
                if timestamp >= sorted_timestamp:
                    continue
                kwh_profit = self.charge_kwh_profit(ctx, price, sorted_price)
                if kwh_profit < ctx.get_min_profit_per_kwh():
                    continue
                hour_in_charge_hours, kwh_available = self.kwh_available_in_hour(ctx.charge_hours, ctx.charging_plan, ctx.current_hour, ctx.max_kwh_charging, timestamp)
                if hour_in_charge_hours and kwh_available <= 0.0:
                    remove_list.append(timestamp)
                    continue

                what_day = days_between(ctx.current_hour, timestamp)
                if what_day < day - 1 or what_day not in ctx.charging_plan:
                    continue

                headroom_kwh = self.grid_headroom_kwh(ctx, timestamp, price)
                kwh_available = round(min(headroom_kwh, kwh_available), 1)
                if kwh_available <= 0.0:
                    continue
                kwh_needed = min(sorted_kwh_needed, kwh_available)
                if not self.is_profitable(ctx, kwh_profit, kwh_needed):
                    continue

                if round(kwh_needed, 1) > 0.0 and ctx.kwh_to_percentage(kwh_needed) > 0.0:
                    sorted_kwh_needed, total_cost, total_kwh, battery_level_added, cost_added = self.add_to_charge_hours(ctx.charge_hours, ctx.config, ctx.efficiency, ctx.kwh_to_percentage, ctx.max_kwh_charging, ctx.name, ctx.percentage_to_kwh, ctx.valuta_kwh, 
                        sorted_kwh_needed, total_cost, total_kwh, timestamp, price, kwh_available,
                        rules=["most_expensive_planner"],
                    )
                    ctx.totals[:] = total_cost, total_kwh
                    if timestamp in ctx.charge_hours and battery_level_added:
                        ctx.charging_plan[day]["total_cost"] += cost_added
                        self.allow_discharge(ctx, sorted_timestamp)
                        reason = (
                            f"<details><summary>{emoji_parse({'charging': True})}{ctx.r('title.most_expensive_hours')} ({price}/{battery_level_added:.0f}%)"
                            f"{self.other_day_text(ctx, day, what_day)}</summary>"
                            f"{ctx.r('prioritized_hour')}: **{sorted_timestamp}**<br>"
                            f"{ctx.r('prioritized_level')}: **{sorted_battery_level:.1f}%**<br>"
                            f"{ctx.r('prioritized_price')}: **{sorted_price}** {ctx.valuta_kwh}<br>"
                            f"{ctx.r('current_price')}: **{price} {ctx.valuta_kwh}**<br>"
                            f"{ctx.r('profit_charge_for_hour')}: **{kwh_profit:.2f} {ctx.valuta_kwh}**<br>"
                            f"{ctx.r('room_without_solar')}: **{headroom_kwh:.2f} kWh**<br>"
                            f"{ctx.r('level_added')}: **{battery_level_added:.2f}%**<br>"
                            "</details>"
                        )
                        self.add_charging_session_to_day(ctx, timestamp, what_day, reason=reason)
                        self.add_charging_to_days(ctx, timestamp)
                else:
                    break

            for remove_timestamp in remove_list:
                item = (remove_timestamp, ctx.grid_prices.get(remove_timestamp))
                if item in ctx.sorted_by_cheapest_price:
                    ctx.sorted_by_cheapest_price.remove(item)

    def needed_before_max_level_planner(self, ctx, day):
        """Charge the little the battery lacks before it reaches its highest level (e.g. solar fills it later)."""
        total_cost, total_kwh = ctx.totals
        highest_battery_level = ctx.get_battery_level() if day == 0 else sum(ctx.charging_plan[day]["battery_level_start_of_day"])
        highest_battery_level_timestamp = ctx.current_hour if day == 0 else ctx.charging_plan[day]["start_of_day"]
        from_hour = ctx.now.hour if day == 0 else 0

        for hour in range(from_hour, 24):
            battery_level = sum(ctx.charging_plan[day]["battery_level_flow"].get(hour, [0.0]))
            if battery_level > highest_battery_level:
                highest_battery_level = battery_level
                highest_battery_level_timestamp = highest_battery_level_timestamp.replace(hour=hour)

        from_timestamp, to_timestamp = self.charging_window(ctx, day)

        for hour in range(from_hour, highest_battery_level_timestamp.hour + 1):
            needed_timestamp = ctx.charging_plan[day]["start_of_day"].replace(hour=hour)
            if needed_timestamp <= ctx.current_hour:
                continue
            kwh_needed = self.uncovered_kwh(ctx, day, hour)
            if round(kwh_needed, 1) <= 0.0:
                continue
            _LOGGER.debug("Needed before max level: day %s hour %02d needs %.2f kWh", day, hour, kwh_needed)
            needed_price = ctx.grid_prices.get(needed_timestamp)
            if needed_price is None:
                continue

            for timestamp, price in ctx.sorted_by_cheapest_price:
                if round(kwh_needed, 1) <= 0.0 or ctx.kwh_to_percentage(kwh_needed) <= 0.0:
                    break
                if not in_between(timestamp, from_timestamp, to_timestamp) or timestamp < ctx.current_hour:
                    continue
                if timestamp >= needed_timestamp:
                    continue
                kwh_profit = self.charge_kwh_profit(ctx, price, needed_price)
                if kwh_profit < ctx.get_min_profit_per_kwh():
                    continue
                hour_in_charge_hours, kwh_available = self.kwh_available_in_hour(ctx.charge_hours, ctx.charging_plan, ctx.current_hour, ctx.max_kwh_charging, timestamp)
                if hour_in_charge_hours and kwh_available <= 0.0:
                    continue
                what_day = days_between(ctx.current_hour, timestamp)
                if what_day < day - 1 or what_day not in ctx.charging_plan:
                    continue
                kwh_available = round(min(kwh_available, self.grid_headroom_kwh(ctx, timestamp, price)), 1)
                if kwh_available <= 0.0 or not self.is_profitable(ctx, kwh_profit, min(kwh_needed, kwh_available)):
                    continue

                kwh_needed, total_cost, total_kwh, battery_level_added, cost_added = self.add_to_charge_hours(ctx.charge_hours, ctx.config, ctx.efficiency, ctx.kwh_to_percentage, ctx.max_kwh_charging, ctx.name, ctx.percentage_to_kwh, ctx.valuta_kwh, 
                    kwh_needed, total_cost, total_kwh, timestamp, price, kwh_available,
                    rules=["needed_before_max_level_planner"],
                )
                ctx.totals[:] = total_cost, total_kwh
                if timestamp in ctx.charge_hours and battery_level_added:
                    ctx.charging_plan[day]["total_cost"] += cost_added
                    self.allow_discharge(ctx, needed_timestamp)
                    reason = (
                        f"<details><summary>{emoji_parse({'needed_before_max_level_planner': True})}{ctx.r('title.needed')} "
                        f"({price}/{battery_level_added:.0f}%){self.other_day_text(ctx, day, what_day)}</summary>"
                        f"{ctx.r('needed_at')}: **{needed_timestamp.strftime('%H:%M')}** ({needed_price} {ctx.valuta_kwh})<br>"
                        f"{ctx.r('highest_level_time')}: **{highest_battery_level_timestamp.strftime('%H:%M')}**<br>"
                        f"{ctx.r('highest_level')}: **{highest_battery_level:.1f}%**<br>"
                        f"{ctx.r('current_price')}: **{price}** {ctx.valuta_kwh}<br>"
                        f"{ctx.r('profit_per_kwh')}: **{kwh_profit:.2f} {ctx.valuta_kwh}**<br>"
                        f"{ctx.r('level_added')}: **{battery_level_added:.2f}%**<br>"
                        "</details>"
                    )
                    self.add_charging_session_to_day(ctx, timestamp, what_day, reason=reason)
                    self.add_charging_to_days(ctx, timestamp)

    def negative_price_planner(self, ctx, day):
        """Fill the battery when the buy price is so low that charging pays even if the kWh are never used.

        Solar first is soft here: the solar surplus pushed out is sold for more than the grid kWh cost.
        """
        total_cost, total_kwh = ctx.totals
        for timestamp, price in ctx.sorted_by_cheapest_price:
            kwh_profit = self.charge_kwh_profit(ctx, price, 0.0)
            if kwh_profit < max(ctx.get_min_profit_per_kwh(), 0.01):
                break
            what_day = days_between(ctx.current_hour, timestamp)
            if what_day != day or timestamp < ctx.current_hour:
                continue
            hour_in_charge_hours, kwh_available = self.kwh_available_in_hour(ctx.charge_hours, ctx.charging_plan, ctx.current_hour, ctx.max_kwh_charging, timestamp)
            kwh_available = round(min(kwh_available, self.grid_headroom_kwh(ctx, timestamp, price)), 1)
            if kwh_available <= 0.0 or not self.is_profitable(ctx, kwh_profit, kwh_available):
                continue
            _, total_cost, total_kwh, battery_level_added, cost_added = self.add_to_charge_hours(ctx.charge_hours, ctx.config, ctx.efficiency, ctx.kwh_to_percentage, ctx.max_kwh_charging, ctx.name, ctx.percentage_to_kwh, ctx.valuta_kwh, 
                kwh_available, total_cost, total_kwh, timestamp, price, kwh_available, rules=["negative_price_planner"],
            )
            ctx.totals[:] = total_cost, total_kwh
            if timestamp in ctx.charge_hours and battery_level_added:
                ctx.charging_plan[day]["total_cost"] += cost_added
                reason = (
                    f"<details><summary>{emoji_parse({'negative_price_planner': True})}{ctx.r('title.negative_price')} ({price}/{battery_level_added:.0f}%)</summary>"
                    f"{ctx.r('current_price')}: **{price} {ctx.valuta_kwh}**<br>"
                    f"{ctx.r('profit_without_use')}: **{kwh_profit:.2f} {ctx.valuta_kwh}**<br>"
                    f"{ctx.r('level_added')}: **{battery_level_added:.2f}%**<br>"
                    "</details>"
                )
                self.add_charging_session_to_day(ctx, timestamp, what_day, reason=reason)
                self.add_charging_to_days(ctx, timestamp)

    def grid_charge_to_sell(self, ctx, day):
        """Charge from the grid only to sell the kWh again in the most expensive sell periods.

        A sale pairs with the cheapest earlier buy hour: sell price minus buy price incl. loss and
        wear has to beat min_profit_per_kwh, the sale min_sell_kwh and min_total_profit. The kWh
        have to fit in the battery until the sale, and an hour either charges or sells.
        """
        total_cost, total_kwh = ctx.totals
        min_gain = max(ctx.get_min_profit_per_kwh(), 0.01)
        exclude_hours = self.get_exclude_sell_hours()
        sell_prices, sell_price, using_grid_sell_price = self.sell_prices_for_window(ctx, 
            max(ctx.charging_plan[day]["start_of_day"], ctx.current_sell_price_timestamp), ctx.charging_plan[day]["end_of_day"]
        )
        for sell_timestamp, price in sorted(sell_prices.items(), key=lambda kv: (kv[1], kv[0]), reverse=True):
            hour_timestamp = reset_time_to_hour(sell_timestamp)
            data = ctx.charging_plan[day]
            if sell_timestamp.hour in exclude_hours or sell_timestamp in data["force_discharge_timestamps"]:
                continue
            if hour_in(sell_timestamp, data["charging_sessions"]) or self.solar_surplus_in_hour(ctx.charging_plan, ctx.current_hour, sell_timestamp) > 0.0:
                continue
            periods_in_hour = self.price_provider.sell_periods_in_hour(sell_timestamp)
            min_sell_kwh = self.number("min_sell_kwh") / periods_in_hour
            slot_kwh = abs(ctx.max_kwh_discharging) / periods_in_hour
            if hour_timestamp in data["discharge_timestamps"]:
                # the house uses the same discharge rate
                slot_kwh -= data["hour_cost_prediction"][ctx.forecast_type].get(hour_timestamp.hour, {}).get("kwh", 0.0) / periods_in_hour
            if slot_kwh < min_sell_kwh:
                continue

            for buy_timestamp, buy_price in ctx.sorted_by_cheapest_price:
                kwh_profit = self.charge_kwh_profit(ctx, buy_price, price)
                if kwh_profit < min_gain:
                    break
                buy_day = days_between(ctx.current_hour, buy_timestamp)
                if buy_timestamp < ctx.current_hour or buy_timestamp >= hour_timestamp or buy_day not in ctx.charging_plan:
                    continue
                _, kwh_available = self.kwh_available_in_hour(ctx.charge_hours, ctx.charging_plan, ctx.current_hour, ctx.max_kwh_charging, buy_timestamp)
                grid_kwh = round(min(kwh_available, self.room_until(ctx, buy_timestamp, hour_timestamp), slot_kwh / ctx.efficiency**2), 2)
                delivered_kwh = grid_kwh * ctx.efficiency**2
                if delivered_kwh < min_sell_kwh or not self.is_profitable(ctx, kwh_profit, delivered_kwh, periods_in_hour):
                    continue
                charged_before = ctx.charge_hours.get(buy_timestamp, {}).get("kWh", 0.0)
                _, total_cost, total_kwh, battery_level_added, cost_added = self.add_to_charge_hours(ctx.charge_hours, ctx.config, ctx.efficiency, ctx.kwh_to_percentage, ctx.max_kwh_charging, ctx.name, ctx.percentage_to_kwh, ctx.valuta_kwh, 
                    grid_kwh, total_cost, total_kwh, buy_timestamp, buy_price, grid_kwh, rules=["grid_charge_to_sell"],
                )
                ctx.totals[:] = total_cost, total_kwh
                if not battery_level_added:
                    continue
                delivered_kwh = (ctx.charge_hours[buy_timestamp]["kWh"] - charged_before) * ctx.efficiency**2
                ctx.charging_plan[buy_day]["total_cost"] += cost_added
                fixed_price_calc_text = (
                    f"{sell_price:.2f} - {ctx.grid_sell_prices.get(sell_timestamp, 0.0):.2f} =" if not using_grid_sell_price else ""
                )
                data["force_discharge_timestamps"][sell_timestamp] = {
                    "title": ctx.r("title.sell_bought_cheap"),
                    "emoji": emoji_parse({"grid_charge_to_sell": True}),
                    "kwh": delivered_kwh,
                    "profit": kwh_profit * delivered_kwh,
                    "reason": (
                        f"<details><summary>{emoji_parse({'grid_charge_to_sell': True})}{ctx.r('title.sell_bought_cheap')}<br>"
                        f"({delivered_kwh:.1f} kWh / +{kwh_profit * delivered_kwh:.2f} {ctx.valuta})</summary>"
                        f"{ctx.r('bought_at', time=buy_timestamp.strftime('%d/%m %H:%M'), price=f'{buy_price:.2f} {ctx.valuta_kwh}')}<br>"
                        f"{ctx.r('grid_sell_price')}: **{fixed_price_calc_text}{price:.2f} {ctx.valuta_kwh}**<br>"
                        f"{ctx.r('loss_wear_per_kwh')}: **{ctx.calc_battery_loss_cost(buy_price) + abs(ctx.config['solar']['powerwall_wear_cost_per_kwh']):.2f} {ctx.valuta_kwh}**<br>"
                        f"{ctx.r('profit_per_kwh')}: **{kwh_profit:.2f} {ctx.valuta_kwh}**<br>"
                        "</details>"
                    ),
                }
                reason = (
                    f"<details><summary>{emoji_parse({'grid_charge_to_sell': True})}{ctx.r('title.buy_to_sell')} ({buy_price}/{battery_level_added:.0f}%)"
                    f"{self.other_day_text(ctx, day, buy_day)}</summary>"
                    f"{ctx.r('sold_at', time=sell_timestamp.strftime('%d/%m %H:%M'), price=f'{price:.2f} {ctx.valuta_kwh}')}<br>"
                    f"{ctx.r('profit_per_kwh')}: **{kwh_profit:.2f} {ctx.valuta_kwh}**<br>"
                    f"{ctx.r('level_added')}: **{battery_level_added:.2f}%**<br>"
                    "</details>"
                )
                self.add_charging_session_to_day(ctx, buy_timestamp, buy_day, reason=reason)
                self.add_charging_to_days(ctx, buy_timestamp)
                break
