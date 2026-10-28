"""Helpers the future-charging rules share: prices, profit, headroom and room in the battery."""
from __future__ import annotations

import datetime

from ....utils.helpers import average, days_between, hour_in, in_between, reset_time_to_hour
from .._helpers import _running_battery_price


class FutureSupport:
    """Price, profit and battery helpers used by the rules."""

    def add_charging_session_to_day(self, ctx, timestamp, what_day, reason="unknown"):
        ctx.charging_plan[what_day]["charging_sessions"][timestamp] = ctx.charge_hours[timestamp]
        ctx.charging_plan[what_day]["charging_sessions"][timestamp]["reason"] = reason
        if hour_in(timestamp, ctx.charging_plan[what_day]["discharge_timestamps"]) and timestamp in ctx.charging_plan[what_day]["discharge_timestamps"]:
            ctx.charging_plan[what_day]["discharge_timestamps"].remove(timestamp)

    def add_charging_to_days(self, ctx, timestamp):
        days = days_between(ctx.now, timestamp)
        for day in range(days, ctx.amount_of_days):
            self.battery_level_flow_prediction(ctx.charging_plan, ctx.config, ctx.efficiency, ctx.forecast_type, ctx.get_battery_level, ctx.kwh_to_percentage, ctx.max_kwh_charging, ctx.max_kwh_discharging, ctx.now, day, timestamp.hour if day == days else 0, reset_hour=True)

    def _get_predicted_battery_cost(self, ctx, day, hour=None):
        """(kWh price, loss, kWh price incl. loss and wear) of the energy in the battery at the end of the hour.

        A running average from now through the planned flow: grid charged kWh come in at the
        charge price, solar at what the surplus would have been sold for, and used/sold kWh
        leave at the average price (the price of what is left doesn't change).
        """
        target = ctx.charging_plan[day]["start_of_day"].replace(hour=23 if hour is None else hour)
        unit = ctx.battery_expenses.get("unit")
        if not isinstance(unit, (int, float)):
            unit = self.get_powerwall_kwh_price()
        # amounts as kWh charged (AC), like the charging history's unit price
        steps = []
        timestamp = ctx.current_hour
        while timestamp <= target:
            data = ctx.charging_plan.get(days_between(ctx.current_hour, timestamp))
            if data is None:
                break
            flow = data["battery_level_flow"].get(timestamp.hour)
            if flow:
                sessions = [session for ts, session in data["charging_sessions"].items() if reset_time_to_hour(ts) == timestamp]
                grid_kwh = sum(session["kWh"] for session in sessions)
                steps.append((
                    ctx.percentage_to_kwh(sum(flow) - flow[0]) / ctx.efficiency,
                    grid_kwh,
                    sum(session["Cost"] for session in sessions) / grid_kwh if grid_kwh > 0.0 else 0.0,
                    self.sell_price_in_hour(ctx, timestamp),
                ))
            timestamp += datetime.timedelta(hours=1)
        start_kwh = ctx.percentage_to_kwh(max(ctx.get_battery_level() - ctx.config["solar"]["powerwall_battery_level_min"], 0.0)) / ctx.efficiency
        raw = _running_battery_price(start_kwh, unit, steps)
        loss_cost = ctx.calc_battery_loss_cost(raw)
        return raw, loss_cost, raw + loss_cost + abs(ctx.config["solar"]["powerwall_wear_cost_per_kwh"])

    def sell_price_in_hour(self, ctx, timestamp):
        """Sell price of the hour (average of its periods), else the predicted solar sell price."""
        hour_timestamp = reset_time_to_hour(timestamp)
        prices, _, _ = self.sell_prices_for_window(ctx, hour_timestamp, hour_timestamp + datetime.timedelta(hours=1))
        if prices:
            return average(list(prices.values()))
        data = ctx.charging_plan.get(days_between(ctx.current_hour, hour_timestamp))
        predicted = data["solar_cost_prediction"] if data else []
        return float(predicted[hour_timestamp.hour]) if hour_timestamp.hour < len(predicted) else 0.0

    def grid_headroom_kwh(self, ctx, timestamp, price=None):
        """Grid kWh that can be charged at timestamp without pushing solar surplus out of the battery.

        Grid energy stays in the battery until it runs down to min level, so the highest
        predicted level (incl. solar) from the charge hour until then must stay below max.
        With price: solar first is soft - when the solar surplus that would be pushed out is
        sold for more than the grid kWh costs (e.g. negative buy prices), it may be pushed out.
        """
        minimum = ctx.config["solar"]["powerwall_battery_level_min"]
        maximum = ctx.config["solar"]["powerwall_battery_level_max"]
        highest = minimum
        loop_timestamp = reset_time_to_hour(timestamp)
        while (data := ctx.charging_plan.get(days_between(ctx.current_hour, loop_timestamp))) is not None:
            flow = data["battery_level_flow"].get(loop_timestamp.hour)
            if flow:
                level = sum(flow)
                if (
                    price is not None and loop_timestamp > timestamp and level >= maximum - 0.01
                    and self.solar_surplus_in_hour(ctx.charging_plan, ctx.current_hour, loop_timestamp) > 0.0
                    and self.sell_price_in_hour(ctx, loop_timestamp) - price >= max(ctx.get_min_profit_per_kwh(), 0.01)
                ):
                    # the battery is full of solar from here on anyway, the pushed out solar is sold well
                    break
                highest = max(highest, level, flow[0])
                if loop_timestamp > timestamp and level <= minimum + 0.01:
                    break
            loop_timestamp += datetime.timedelta(hours=1)
        return ctx.percentage_to_kwh(max(maximum - highest, 0.0)) / ctx.efficiency

    def charge_kwh_profit(self, ctx, charge_price, use_price):
        """Profit per kWh charged at charge_price and used/sold at use_price, after loss and wear."""
        return use_price - (charge_price + ctx.calc_battery_loss_cost(charge_price) + abs(ctx.config["solar"]["powerwall_wear_cost_per_kwh"]))

    def is_profitable(self, ctx, kwh_profit, kwh, periods_in_hour=1):
        """min_profit_per_kwh and min_total_profit (per hour, split over the price periods)."""
        return kwh_profit >= ctx.get_min_profit_per_kwh() and kwh_profit * kwh >= self.get_min_total_profit() / periods_in_hour

    def allow_discharge(self, ctx, timestamp):
        """The battery is charged for this hour, so it has to be allowed to discharge in it."""
        data = ctx.charging_plan.get(days_between(ctx.current_hour, timestamp))
        if data is None or timestamp in data["charging_sessions"]:
            return
        data["blocked_discharge_timestamps"].pop(timestamp, None)
        if timestamp not in data["discharge_timestamps"]:
            data["discharge_timestamps"].append(timestamp)

    def uncovered_kwh(self, ctx, day, hour):
        """Grid kWh to charge (incl. loss) for the consumption the battery/solar doesn't cover in the current plan."""
        minimum = ctx.config["solar"]["powerwall_battery_level_min"]
        flow = ctx.charging_plan[day]["battery_level_flow"].get(hour)
        prediction = ctx.charging_plan[day]["hour_cost_prediction"][ctx.forecast_type].get(hour)
        if not flow or prediction is None:
            return 0.0
        timestamp = ctx.charging_plan[day]["start_of_day"].replace(hour=hour)
        discharging = timestamp in ctx.charging_plan[day]["discharge_timestamps"]
        if self.solar_surplus_in_hour(ctx.charging_plan, ctx.current_hour, timestamp) > 0.0 or (discharging and sum(flow) > minimum + 0.01):
            return 0.0
        covered = ctx.percentage_to_kwh(max(flow[0] - minimum, 0.0)) * ctx.efficiency if discharging else 0.0
        return max(prediction["kwh"] - covered, 0.0) / ctx.efficiency**2

    def charging_window(self, ctx, day):
        from_timestamp = ctx.charging_plan[day]["start_of_day"] - datetime.timedelta(hours=6) if day > 0 else ctx.charging_plan[day]["start_of_day"]
        return from_timestamp, ctx.charging_plan[day]["end_of_day"]

    def other_day_text(self, ctx, day, what_day):
        if what_day != day:
            return f"<br><center>**({ctx.charging_plan[day]['start_of_day'].date().strftime('%d/%m')})**</center>"
        return ""

    def lowest_battery_level_until_next_charge(self, ctx, day):
        """Lowest level from the end of the day until the battery is empty or full again.

        Scans every following day of the plan (days_to_predict): with a big battery or a low
        consumption the energy lasts longer than one day. Once the battery is full again
        (solar/grid), energy sold before that is refilled, so the scan stops there.
        """
        minimum = ctx.config["solar"]["powerwall_battery_level_min"]
        maximum = ctx.config["solar"]["powerwall_battery_level_max"]
        lowest_timestamp = ctx.charging_plan[day]["end_of_day"]
        lowest_battery_level = sum(ctx.charging_plan[day]["battery_level_end_of_day"])
        using_next_day = False
        if not self.enabled("use_midnight_battery_level"):
            for next_day in range(day + 1, ctx.amount_of_days):
                for hour in range(24):
                    battery_level = ctx.charging_plan[next_day]["battery_level_flow"].get(hour)
                    if not battery_level:
                        continue
                    battery_level = sum(battery_level)
                    if battery_level >= maximum - 0.01:
                        return lowest_timestamp, lowest_battery_level, using_next_day
                    if battery_level < lowest_battery_level:
                        using_next_day = True
                        lowest_battery_level = battery_level
                        lowest_timestamp = ctx.charging_plan[next_day]["start_of_day"].replace(hour=hour)
                    if lowest_battery_level <= minimum + 0.01:
                        return lowest_timestamp, lowest_battery_level, using_next_day
        return lowest_timestamp, lowest_battery_level, using_next_day

    def sell_prices_for_window(self, ctx, from_timestamp, to_timestamp):
        """What a sold kWh is worth per sell period (see sell_value - can be negative with a fixed price)."""
        sell_price = ctx.config["solar"]["production_price"]
        using_grid_sell_price = sell_price == -1.0
        prices = {
            ts: price if using_grid_sell_price else round(self.sell_value(price), 2)
            for ts, price in ctx.grid_sell_prices.items() if in_between(ts, from_timestamp, to_timestamp)
        }
        return prices, sell_price, using_grid_sell_price

    def room_until(self, ctx, from_timestamp, to_timestamp):
        """Grid kWh that fit in the battery from from_timestamp until (excl.) to_timestamp."""
        highest = ctx.config["solar"]["powerwall_battery_level_min"]
        loop_timestamp = reset_time_to_hour(from_timestamp)
        while loop_timestamp < to_timestamp:
            data = ctx.charging_plan.get(days_between(ctx.current_hour, loop_timestamp))
            flow = data["battery_level_flow"].get(loop_timestamp.hour) if data else None
            if flow:
                highest = max(highest, sum(flow), flow[0])
            loop_timestamp += datetime.timedelta(hours=1)
        return ctx.percentage_to_kwh(max(ctx.config["solar"]["powerwall_battery_level_max"] - highest, 0.0)) / ctx.efficiency
