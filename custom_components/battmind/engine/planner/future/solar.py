"""The rule that optimises solar charging."""
from __future__ import annotations

import logging
from collections import defaultdict

from ....utils.formatting import emoji_parse
from ....utils.helpers import average
from .._helpers import _optimize_solar_blocking

_LOGGER = logging.getLogger(__name__)


class FutureSolar:
    """Solar charging optimisation."""

    def optimize_solar_charging(self, ctx, day):
        """Sell the solar surplus in the expensive hours and fill the battery in the cheapest solar hours.

        Only when the sun fills the battery to max anyway; the battery still ends the
        day at the same level, so the plan for the following days is unchanged.
        """
        cfg = ctx.config["solar"]
        data = ctx.charging_plan[day]
        start = data["start_of_day"]
        solar = data.get("solar_kwh_prediction", [])
        predicted_prices = data.get("solar_cost_prediction", [])
        base = data.get("battery_level_flow_without_solar", {})
        original = data.get("battery_level_flow", {})
        blocked = data["blocked_solar_charge_timestamps"] = {}
        if not self.enabled("solar_charging") or not base or not original:
            return

        minimum = cfg["powerwall_battery_level_min"]
        maximum = cfg["powerwall_battery_level_max"]
        from_hour = ctx.now.hour if day == 0 else 0
        hours = [h for h in sorted(base) if h >= from_hour and original.get(h)]
        if not any(sum(original[h]) >= maximum - 0.01 for h in hours):
            _LOGGER.debug("Optimize solar charging: day %s the battery doesn't reach %s%%, nothing to sell", day, maximum)
            return

        # What the sold surplus is worth from the real sell prices when known, else the predicted ones (rounded to 0.1)
        sell_by_hour = defaultdict(list)
        for ts, price in ctx.grid_sell_prices.items():
            if ts.date() == start.date():
                sell_by_hour[ts.hour].append(self.sell_value(price))
        busy = {
            ts.hour for ts in [*data.get("charging_sessions", {}), *data["force_discharge_timestamps"]]
            if ts.date() == start.date()
        }
        delta, available, prices = {}, {}, {}
        for h in hours:
            # the solar surplus covers the house, so no consumption from the battery in solar hours
            delta[h] = base[h][1] if len(base[h]) > 1 and not (h < len(solar) and solar[h] > 0) else 0.0
            if h < len(solar) and h < len(predicted_prices) and solar[h] > 0:
                # same conversion as battery_level_flow_prediction()
                available[h] = ctx.kwh_to_percentage(min(solar[h], ctx.max_kwh_charging)) * ctx.efficiency
                prices[h] = average(sell_by_hour[h]) if sell_by_hour.get(h) else float(predicted_prices[h])
        candidates = [h for h in available if h not in busy]
        if not candidates:
            return

        blocked_hours, flow, charged, original_charged = _optimize_solar_blocking(
            hours, delta, available, prices, candidates, float(original[hours[0]][0]), minimum, maximum
        )
        if not blocked_hours:
            return

        gain = ctx.percentage_to_kwh(sum((original_charged[h] - charged[h]) * prices[h] for h in available)) / ctx.efficiency
        moved_text = "".join(
            f"{ctx.r('at_time', time=f'{h:02d}:00')}: {ctx.percentage_to_kwh(charged[h] - original_charged[h]) / ctx.efficiency:.2f} kWh ({prices[h]:.2f} {ctx.valuta_kwh})<br>"
            for h in sorted(available)
            if ctx.percentage_to_kwh(charged[h] - original_charged[h]) / ctx.efficiency >= 0.01
        )
        moved_to = [h for h in sorted(available) if ctx.percentage_to_kwh(charged[h] - original_charged[h]) / ctx.efficiency >= 0.01]
        charged_at = f" ({moved_to[0]:02d}:00)" if moved_to else ""
        for h in sorted(blocked_hours):
            ts = start.replace(hour=h, minute=0, second=0, microsecond=0)
            blocked[ts] = {
                "reason": (
                    f"<details><summary>{emoji_parse({'blocked_solar_charge': True})}{ctx.r('title.blocked_solar')}{charged_at}</summary>"
                    f"<b>{ctx.r('before_blocking')}:</b> {sum(original[h]):.1f}%<br>"
                    f"<b>{ctx.r('after_blocking')}:</b> {sum(flow[h]):.1f}%<br>"
                    f"<b>{ctx.r('solar_sold')}:</b> {ctx.percentage_to_kwh(available[h]) / ctx.efficiency:.2f} kWh<br>"
                    f"<b>{ctx.r('sell_price')}:</b> {prices[h]:.2f} {ctx.valuta_kwh}<br>"
                    f"<b>{ctx.r('expected_gain_day')}:</b> {gain:.2f} {ctx.valuta}<br><br>"
                    f"<b>{ctx.r('charged_instead')}:</b><br>{moved_text}</details>"
                )
            }
            if ts in data["discharge_timestamps"]:
                data["discharge_timestamps"].remove(ts)
        if blocked:
            _LOGGER.debug("Optimize solar charging: day %s blocking solar charging at %s", day, ", ".join(ts.strftime("%H:%M") for ts in sorted(blocked)))
