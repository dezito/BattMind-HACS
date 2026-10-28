"""The rules that sell energy to the grid."""
from __future__ import annotations

import datetime
import logging

from ....utils.formatting import emoji_parse
from ....utils.helpers import days_between, hour_in, hours_between, reset_time_to_hour, round_up

_LOGGER = logging.getLogger(__name__)


class FutureSelling:
    """Most profit and sale of excess kWh."""

    def most_profit(self, ctx, day):
        """Sell battery energy in the most expensive sell periods when that pays more than using it.

        The kWh sold are missing in hours the battery would otherwise cover, so the cheapest of
        those hours are bought from the grid instead. Only hours in the same discharge period
        count (the battery isn't full in between, else the saved energy just pushes solar out).
        A sale has to beat that buy price by min_profit_per_kwh, pay the battery kWh cost itself,
        sell at least min_sell_kwh and earn min_total_profit (per hour, split over the price periods).
        """
        minimum = max(ctx.config["solar"]["powerwall_battery_level_min"], self.number("usage_reserve"))
        maximum = ctx.config["solar"]["powerwall_battery_level_max"]
        min_profit_per_kwh = ctx.get_min_profit_per_kwh()
        # selling has to earn something more than using the energy, also with min_profit_per_kwh = 0
        min_gain_per_kwh = max(min_profit_per_kwh, 0.01)
        from_timestamp = max(ctx.charging_plan[day]["start_of_day"], ctx.current_sell_price_timestamp)
        lowest_timestamp, _, _ = self.lowest_battery_level_until_next_charge(ctx, day)
        grid_sell_prices_for_day, sell_price, using_grid_sell_price = self.sell_prices_for_window(ctx, 
            from_timestamp, lowest_timestamp + datetime.timedelta(hours=1)
        )
        exclude_hours = self.get_exclude_sell_hours()

        # kWh the battery can deliver at the end of each hour, the hours it is full and
        # the hours it covers (cheapest first)
        window, deliverable, full_before, usage = [], {}, {}, []
        full_count = 0
        loop_timestamp = reset_time_to_hour(from_timestamp)
        while loop_timestamp <= lowest_timestamp:
            data = ctx.charging_plan.get(days_between(ctx.current_hour, loop_timestamp))
            flow = data["battery_level_flow"].get(loop_timestamp.hour) if data else None
            if flow:
                window.append(loop_timestamp)
                deliverable[loop_timestamp] = ctx.percentage_to_kwh(max(sum(flow) - minimum, 0.0)) * ctx.efficiency
                full_before[loop_timestamp] = full_count
                prediction = data["hour_cost_prediction"][ctx.forecast_type].get(loop_timestamp.hour)
                grid_price = ctx.grid_prices.get(loop_timestamp)
                if (
                    prediction is not None and grid_price is not None
                    and loop_timestamp in data["discharge_timestamps"] and self.solar_surplus_in_hour(ctx.charging_plan, ctx.current_hour, loop_timestamp) <= 0.0
                ):
                    kwh = min(prediction["kwh"], ctx.percentage_to_kwh(max(flow[0] - minimum, 0.0)) * ctx.efficiency)
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
            what_day = days_between(ctx.current_hour, timestamp)
            hour = timestamp.hour
            hour_timestamp = reset_time_to_hour(timestamp)
            if timestamp < from_timestamp or hour in exclude_hours or hour_timestamp not in deliverable:
                continue
            if timestamp in ctx.charging_plan[what_day]["force_discharge_timestamps"] or hour_in(timestamp, ctx.charging_plan[what_day]["charging_sessions"]):
                continue
            flow = ctx.charging_plan[what_day]["battery_level_flow"][hour]
            raw, loss_cost, battery_kwh_cost = self._get_predicted_battery_cost(ctx, what_day, hour)
            if price - battery_kwh_cost < min_profit_per_kwh:
                continue

            periods_in_hour = self.price_provider.sell_periods_in_hour(timestamp)
            min_sell_kwh = self.number("min_sell_kwh") / periods_in_hour
            slot_kwh = abs(ctx.max_kwh_discharging) / periods_in_hour
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
                usage_text += f"{ctx.r('at_time', time=item[1].strftime('%H:%M'))}: {take:.2f} kWh ({item[0]:.2f} {ctx.valuta_kwh})<br>"
                if item[2] <= 0.01:
                    # the battery isn't used in this hour, the energy is sold instead
                    usage_data = ctx.charging_plan[days_between(ctx.current_hour, item[1])]
                    if item[1] in usage_data["discharge_timestamps"]:
                        usage_data["discharge_timestamps"].remove(item[1])
                    usage_data["blocked_discharge_timestamps"][item[1]] = (
                        f"<details><summary>{emoji_parse({'most_profit': True})}{ctx.r('title.sold_instead_at', time=timestamp.strftime('%H:%M'))}</summary>"
                        f"{ctx.r('price_now')}: **{item[0]:.2f} {ctx.valuta_kwh}**<br>"
                        f"{ctx.r('sell_price_at', time=timestamp.strftime('%H:%M'))}: **{price:.2f} {ctx.valuta_kwh}**<br>"
                        "</details>"
                    )
            usage = [item for item in usage if item[2] > 0.01]

            grid_sell_profit = (price - battery_kwh_cost) * kwh
            _LOGGER.debug("Most profit: selling %.2f kWh at %s (%.2f more than using it)", kwh, timestamp, gain)
            fixed_price_calc_text = (
                f"{sell_price:.2f} - {ctx.grid_sell_prices.get(timestamp, 0.0):.2f} =" if not using_grid_sell_price else ""
            )
            ctx.charging_plan[what_day]["force_discharge_timestamps"][timestamp] = {
                "title": ctx.r("title.sell_instead_of_use"),
                "emoji": emoji_parse({"most_profit": True}),
                "kwh": kwh,
                "profit": grid_sell_profit,
                "reason": (
                    f"<details><summary>{emoji_parse({'most_profit': True})}{ctx.r('title.sell_instead_of_use')}<br>"
                    f"({kwh:.1f} kWh / +{gain:.2f} {ctx.valuta})</summary>"
                    f"{ctx.r('battery_cost_basis')}: **{raw:.2f} {ctx.valuta_kwh}**<br>"
                    f"{ctx.r('loss_cost')}: **{loss_cost:.2f} {ctx.valuta_kwh}**<br>"
                    f"{ctx.r('wear_cost')}: **{abs(ctx.config['solar']['powerwall_wear_cost_per_kwh']):.2f} {ctx.valuta_kwh}**<br>"
                    f"**{ctx.r('battery_cost_total')}**: **{battery_kwh_cost:.2f} {ctx.valuta_kwh}**<br>"
                    f"{ctx.r('grid_sell_price')}: **{fixed_price_calc_text}{price:.2f} {ctx.valuta_kwh}**<br>"
                    f"{ctx.r('kwh_sold')}: **{kwh:.2f} kWh**<br>"
                    f"{ctx.r('profit_selling')}: **{grid_sell_profit:.2f} {ctx.valuta}**<br>"
                    f"{ctx.r('bought_instead')}:<br>{usage_text}"
                    f"{ctx.r('gain_vs_use')}: **{gain:.2f} {ctx.valuta}**<br>"
                    "</details>"
                ),
            }

    def sell_excess_kwh_available(self, ctx, day):
        from_timestamp = max(ctx.charging_plan[day]["start_of_day"], ctx.current_sell_price_timestamp)
        day_periods_in_hour = self.price_provider.sell_periods_in_hour(ctx.charging_plan[day]["start_of_day"])
        lowest_timestamp, lowest_battery_level, using_next_day = self.lowest_battery_level_until_next_charge(ctx, day)

        usage_minimum = max(ctx.config["solar"]["powerwall_battery_level_min"], self.number("usage_reserve"))
        lowest_battery_level = round(lowest_battery_level, 0) - usage_minimum
        excess_kwh_available = round(ctx.percentage_to_kwh(max(lowest_battery_level, 0.0)) * ctx.efficiency, 1)
        min_sell_kwh = self.number("min_sell_kwh") / day_periods_in_hour

        if excess_kwh_available < min_sell_kwh or excess_kwh_available <= 0.0:
            ctx.charging_plan[day]["force_discharge_timestamps_empty"] = (
                f"<details><summary>âš ï¸ {ctx.r('title.no_excess')}</summary>"
                f"{ctx.r('lowest_level_time')}: **{lowest_timestamp}**<br>"
                f"{ctx.r('lowest_level')}: **{lowest_battery_level:.1f}%**<br>"
                f"{ctx.r('kwh_available_to_sell')}: **{excess_kwh_available:.2f} kWh**<br>"
                f"{ctx.r('min_kwh_to_sell')}: **{min_sell_kwh:.2f} kWh**<br>"
                "</details>"
            )
            return

        if using_next_day:
            _LOGGER.debug("Using lowest battery level of next day (%s) for excess kWh on day %s", lowest_timestamp, day)

        discharge_hours_needed = int(round_up(excess_kwh_available / (abs(ctx.max_kwh_discharging) / day_periods_in_hour)))
        # until the lowest point (can be days ahead with a big battery), at least the evening + night
        grid_sell_prices_for_day, sell_price, using_grid_sell_price = self.sell_prices_for_window(ctx, 
            from_timestamp, max(ctx.charging_plan[day]["end_of_day"] + datetime.timedelta(hours=6), lowest_timestamp + datetime.timedelta(hours=1))
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
                what_day = days_between(ctx.charging_plan[0]["start_of_day"], timestamp)
                if what_day not in ctx.charging_plan:
                    continue
                if timestamp in ctx.charging_plan[what_day]["force_discharge_timestamps"]:
                    continue

                if hour_in(timestamp, ctx.charging_plan[what_day]["charging_sessions"]):
                    continue
                battery_level = ctx.charging_plan[what_day]["battery_level_flow"].get(timestamp.hour)
                if not battery_level:
                    continue
                loop_raw, loop_loss_cost, loop_battery_kwh_cost = self._get_predicted_battery_cost(ctx, what_day, timestamp.hour)
                periods_in_hour = self.price_provider.sell_periods_in_hour(timestamp)
                usable_kwh = ctx.percentage_to_kwh(max(sum(battery_level) - usage_minimum, 0.0)) * ctx.efficiency
                excess_current = min(excess_kwh_available, abs(ctx.max_kwh_discharging) / periods_in_hour, usable_kwh)
                if excess_current < self.number("min_sell_kwh") / periods_in_hour:
                    continue
                excess_profit = excess_current * price - excess_current * loop_battery_kwh_cost
                kwh_profit = price - loop_battery_kwh_cost
                min_total_profit = self.get_min_total_profit() / periods_in_hour

                already_planned_now = (
                    day == 0
                    and timestamp in previous_force_discharge
                    and hours_between(ctx.now, timestamp) == 0
                    and excess_profit > 0.0
                )
                if not already_planned_now:
                    if kwh_profit < ctx.get_min_profit_per_kwh():
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
                if timestamp in ctx.charging_plan[day]["discharge_timestamps"]:
                    ctx.charging_plan[day]["discharge_timestamps"].remove(timestamp)

                other_day = (
                    f"<br><center>**({ctx.charging_plan[day]['start_of_day'].date().strftime('%d/%m')})**</center>"
                    if what_day > day else ""
                )
                fixed_price_calc_text = (
                    f"{sell_price:.2f} - {ctx.grid_sell_prices.get(timestamp, 0.0):.2f} =" if not using_grid_sell_price else ""
                )
                ctx.charging_plan[what_day]["force_discharge_timestamps"][timestamp] = {
                    "title": ctx.r("title.sell_excess"),
                    "emoji": emoji_parse({"sell_excess_kwh_available": True}),
                    "kwh": excess_current,
                    "profit": excess_profit,
                    "reason": (
                        f"<details><summary>{emoji_parse({'sell_excess_kwh_available': True})}{ctx.r('title.sell_excess')}<br>"
                        f"({excess_current:.1f} kWh / {excess_profit:.2f} {ctx.valuta}){other_day}</summary>"
                        f"{ctx.r('battery_cost_basis')}: **{loop_raw:.2f} {ctx.valuta_kwh}**<br>"
                        f"{ctx.r('loss_cost')}: **{loop_loss_cost:.2f} {ctx.valuta_kwh}**<br>"
                        f"{ctx.r('wear_cost')}: **{abs(ctx.config['solar']['powerwall_wear_cost_per_kwh']):.2f} {ctx.valuta_kwh}**<br>"
                        f"**{ctx.r('battery_cost_total')}**: **{loop_battery_kwh_cost:.2f} {ctx.valuta_kwh}**<br>"
                        f"{ctx.r('grid_sell_price')}: **{fixed_price_calc_text}{price:.2f} {ctx.valuta_kwh}**<br>"
                        f"{ctx.r('excess_kwh_sold')}: **{excess_current:.2f} kWh**<br>"
                        f"{ctx.r('profit_selling')}: **{excess_profit:.2f} {ctx.valuta}**<br>"
                        "</details>"
                    ),
                }

        if len(ctx.charging_plan[day]["force_discharge_timestamps"]) == 0:
            raw, loss_cost, battery_kwh_cost = self._get_predicted_battery_cost(ctx, day)
            sorted_sell = sorted(grid_sell_prices_for_day.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
            max_price_timestamp = sorted_sell[0][0] if sorted_sell else None
            max_price = sorted_sell[0][1] if sorted_sell else 0.0
            profit = max_price - battery_kwh_cost
            hour_excluded = ""
            if exclude_hours and max_price_timestamp and max_price_timestamp.hour in exclude_hours:
                hour_excluded = f"âš ï¸ {ctx.r('max_price_excluded')}: <b>{max_price_timestamp.hour}:00</b><br>"
            ctx.charging_plan[day]["force_discharge_timestamps_empty"] = (
                f"<details><summary>âš ï¸{ctx.r('title.no_good_sell_time')} ({profit:.2f})</summary>"
                f"{hour_excluded}"
                f"{ctx.r('battery_cost_basis')}: <b>{raw:.2f} {ctx.valuta_kwh}</b><br>"
                f"{ctx.r('loss_cost')}: <b>{loss_cost:.2f} {ctx.valuta_kwh}</b><br>"
                f"{ctx.r('wear_cost')}: <b>{abs(ctx.config['solar']['powerwall_wear_cost_per_kwh']):.2f} {ctx.valuta_kwh}</b><br>"
                f"<b>{ctx.r('battery_cost_total')}: {battery_kwh_cost:.2f} {ctx.valuta_kwh}</b><br>"
                f"{ctx.r('max_price_time')}: <b>{max_price_timestamp if max_price_timestamp else 'N/A'}</b><br>"
                f"{ctx.r('max_sell_price')}: <b>{max_price:.2f} {ctx.valuta_kwh}</b><br>"
                f"{ctx.r('min_profit_per_kwh')}: <b>{ctx.get_min_profit_per_kwh():.2f} {ctx.valuta_kwh}</b><br>"
                f"{ctx.r('profit_selling')}: <b>{profit:.2f} {ctx.valuta_kwh}</b><br>"
                "</details>"
            )
