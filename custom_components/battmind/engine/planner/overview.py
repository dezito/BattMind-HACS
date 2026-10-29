"""The overview markdown of the plan."""
from __future__ import annotations

import datetime
import logging
import math
from collections import defaultdict

from ...utils.formatting import emoji_parse, emoji_text_format
from ...utils.helpers import day_of_week_text, float_or_none, reset_time_to_hour
from ._helpers import _group_hour_rules, _join_timestamps_by_hour, _joined_reason

_LOGGER = logging.getLogger(__name__)


def _finite(value) -> float | None:
    number = float_or_none(value)
    return number if number is not None and math.isfinite(number) else None


def _ev_hour(value, tz) -> datetime.datetime | None:
    """The local (naive) hour of a Cable Juice Planner charge_plan time."""
    try:
        timestamp = datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(tz)
    return reset_time_to_hour(timestamp)


class Overview:
    """The overview markdown of the plan."""

    def _log_plan_summary(self, charging_plan: dict, charge_hours: dict) -> None:
        """One INFO line per new plan + the details at DEBUG."""
        days = [day for day in charging_plan if isinstance(day, int)]
        charges = sorted(ts for ts in charge_hours if isinstance(ts, datetime.datetime))
        sales = sorted(ts for day in days for ts in charging_plan[day]["force_discharge_timestamps"])
        blocked = sum(len(charging_plan[day]["blocked_discharge_timestamps"]) for day in days)
        blocked_solar = sum(len(charging_plan[day]["blocked_solar_charge_timestamps"]) for day in days)
        _LOGGER.info(
            "New plan: %s charge hours (%.2f kWh, %.2f), %s sell periods, %s blocked discharge hours, %s blocked solar hours",
            len(charges), charge_hours.get("total_kwh", 0.0), charge_hours.get("total_cost", 0.0),
            len(sales), blocked, blocked_solar,
        )
        for ts in charges:
            rules = [key for key, value in charge_hours[ts].items() if value is True]
            _LOGGER.debug("  charge %s: %.2f kWh at %.2f/kWh (%s)", ts, charge_hours[ts]["kWh"], charge_hours[ts]["Price"], ", ".join(rules))
        for day in days:
            for ts, data in sorted(charging_plan[day]["force_discharge_timestamps"].items()):
                _LOGGER.debug("  sell %s: %.2f kWh, profit %.2f (%s)", ts, data["kwh"], data["profit"], data["title"])

    def _plan_overview(self, overview: list[str], charging_plan: dict, days: list[int]) -> None:
        """📅 The charge/discharge plan per day (hours with the same rule joined) + the totals of the plan."""
        t = self.t
        valuta = t("ui.common.valuta")
        valuta_kwh = t("ui.common.valuta_kwh")
        try:
            overview.append("<center>\n")
            overview.append(f"## 📅 {t('ui.cheap_grid_charge_hours.plan_title')} ##")
            any_rows = False
            for day in days:
                data = charging_plan[day]
                joined = {}
                joined.update(data["force_powerwall_max_hours_old_discharge_timestamps"])
                joined.update(data["blocked_discharge_timestamps"])
                joined.update(data["blocked_solar_charge_timestamps"])
                joined.update(
                    _join_timestamps_by_hour(
                        data["force_discharge_timestamps"],
                        valuta,
                        t("ui.common.total"),
                        lambda timestamp: self.price_provider.sell_periods_in_hour(timestamp) if self.price_provider else 1,
                    )
                )
                joined.update(data["charging_sessions"])
                if not joined:
                    continue
                any_rows = True
                weekday = t(f"ui.calendar.weekday_names.{day_of_week_text(data['start_of_day'])}")
                overview.append(f"#### {weekday} {data['start_of_day'].strftime('%d/%m')}\n")
                overview.append(f"| {t('ui.common.time')} | {t('ui.common.reason')} |")
                overview.append("|:---|:---|")
                rows = [
                    (timestamp, value["reason"] if isinstance(value, dict) and "reason" in value else value)
                    for timestamp, value in sorted(joined.items(), key=lambda kv: kv[0])
                ]
                for group in _group_hour_rules(rows):
                    first, last = group[0][0], group[-1][0]
                    if len(group) == 1:
                        end = joined.get(first, {}).get("period_end") if isinstance(joined.get(first), dict) else None
                        time_text = f"{first.strftime('%H:%M')}-{end.strftime('%H:%M')}" if end else first.strftime("%H:%M")
                        overview.append(f"| **{time_text}** | {group[0][1]} |")
                    else:
                        until = reset_time_to_hour(last).replace(minute=59).strftime("%H:%M")  # the last hour included
                        overview.append(f"| **{first.strftime('%H:%M')}-{until}** | {_joined_reason(group)} |")
                overview.append("\n")
            if not any_rows:
                overview.append(f"**{t('ui.cheap_grid_charge_hours.no_upcoming_charging_planned')}**\n")
            overview.extend(self._plan_summary_lines(valuta, valuta_kwh))

            # all prices offline, or hours the provider has no price for, filled from the price history
            if self.grid_price_cache.get("missing_hours"):
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
            overview.append("</center>\n")
            overview.append("***")
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Failed to create the charge/discharge plan overview: %s", err)

    def _plan_summary_lines(self, valuta: str, valuta_kwh: str) -> list[str]:
        """The bill of the period without and with the battery (the plan that controls it), item by item."""
        shadow = self.optimizer_shadow or {}
        controls = shadow.get("controls", False)
        summary = shadow.get("summary_optimal" if controls else "summary_rules")
        if not summary:
            return []
        t = self.t
        prefix = "ui.cheap_grid_charge_hours.plan_summary"
        without, with_battery, profit = summary["without"], summary["with"], summary["profit"]

        def money(value: float, kwh: float | None = None, note: str = "") -> str:
            text = f"{value:.2f} {valuta}"
            if kwh is not None:
                text += f" ({kwh:.1f} kWh)"
            return f"{text}<br>({note})" if note else text

        def row(label: str, without_value, with_value, bold: bool = False, difference: float | None = None) -> str:
            """(value, kWh, note) per column, None = not there (–); the difference is with minus without."""
            if difference is None:
                difference = (with_value[0] if with_value else 0.0) - (without_value[0] if without_value else 0.0)
            cells = [
                money(*without_value) if without_value else "–",
                money(*with_value) if with_value else "–",
                f"{difference:+.2f}",
            ]
            if bold:
                label, cells = f"**{label}**", [f"**{cell}**" for cell in cells]
            return f"| {label} | {' | '.join(cells)} |"

        start, end = summary["start"], summary["end"]
        days = ((end - start).total_seconds() / 3600 + 1) / 24 if start and end else 0.0
        period = (
            f"{start.strftime('%d/%m %H:%M')} - {end.replace(minute=59).strftime('%d/%m %H:%M')} ({days:.1f} {t(f'{prefix}.days')})"
            if start and end else "-"
        )
        energy_without = (without["energy_value"], None, t(f"{prefix}.kept")) if abs(without["energy_value"]) >= 0.005 else None
        energy_with = (with_battery["energy_value"], None, t(f"{prefix}.left")) if abs(with_battery["energy_value"]) >= 0.005 else None
        return [
            f"\n<details><summary><b>💰 {t(f'{prefix}.total')}: {profit:+.2f} {valuta} ({summary['profit_per_kwh']:.2f} {valuta_kwh})</b><br>"
            f"📈 {summary['charged_grid_kwh']:.1f} kWh · 📤 {with_battery['sold_kwh']:.1f} kWh · ☀️ {summary['charged_solar_kwh']:.1f} kWh</summary>\n",
            f"|  | {t(f'{prefix}.without')} | {t(f'{prefix}.with')} | {t(f'{prefix}.difference')} |",
            "|:---|---:|---:|---:|",
            row(f"💸 {t(f'{prefix}.paid')}", (without["paid"],), (with_battery["paid"],), bold=True),
            row(f"↳ 🏠 {t(f'{prefix}.to_house')}", (without["house_cost"], without["house_kwh"]), (with_battery["house_cost"], with_battery["house_kwh"])),
            row(f"↳ 📈 {t(f'{prefix}.to_battery')}", None, (with_battery["battery_cost"], with_battery["battery_kwh"])),
            row(f"💵 {t(f'{prefix}.earned')}", (without["earned"],), (with_battery["earned"],), bold=True),
            row(f"↳ ☀️ {t(f'{prefix}.solar_surplus')}", (without["solar_income"], without["solar_kwh"]), (with_battery["solar_income"], with_battery["solar_kwh"])),
            row(f"↳ 📤 {t(f'{prefix}.from_battery')}", None, (with_battery["sold_income"], with_battery["sold_kwh"])),
            row(f"🛠️ {t(f'{prefix}.wear')}", None, (with_battery["wear"],)),
            row(f"🔋 {t(f'{prefix}.energy')}", energy_without, energy_with),
            row(
                f"= {t(f'{prefix}.bill')}",
                (without["bill"],),
                (with_battery["bill"],),
                bold=True,
                difference=-profit,  # the same as the title (not rounded twice)
            ),
            "",
            "|  |  |",
            "|:---|---:|",
            f"| **🔋 {t(f'{prefix}.delivers')}** | {summary['delivered_house_kwh'] + with_battery['sold_kwh']:.1f} kWh "
            f"({summary['delivered_house_kwh']:.1f} {t(f'{prefix}.to_house_short')}, {with_battery['sold_kwh']:.1f} {t(f'{prefix}.sold_short')}) |",
            f"| **🔋 {t(f'{prefix}.charged')}** | {summary['charged_grid_kwh'] + summary['charged_solar_kwh']:.1f} kWh "
            f"({summary['charged_grid_kwh']:.1f} {t(f'{prefix}.from_grid')}, {summary['charged_solar_kwh']:.1f} {t(f'{prefix}.from_solar')}) |",
            f"| **🔋 {t(f'{prefix}.end_level')}** | {summary['end_level']:.0f}% |",
            f"| **📆 {t(f'{prefix}.period')}** | {period} |",
            f"| **🧠 {t(f'{prefix}.controlled_by')}** | {t(f'{prefix}.optimizer') if controls else t(f'{prefix}.rules')} |",
            *self._ev_charging_lines(start, end, with_battery["bill"], valuta, valuta_kwh),
            f"\n*{t(f'{prefix}.note')}*",
            "</details>\n",
        ]

    def _ev_charging_totals(self, start: datetime.datetime, end: datetime.datetime) -> list[dict]:
        """The planned charging of each Cable Juice Planner car in the hours start..end; kwh None = no valid plan."""
        tz = self._snap.tz if self._snap else None
        cars = []
        for car in self._snap.ev_charging if self._snap else []:
            plan = car.get("charge_plan")
            hours, kwh, cost, valid = set(), 0.0, 0.0, isinstance(plan, list)
            for row in plan or []:
                hour = _ev_hour(row.get("time"), tz)
                row_kwh = _finite(row.get("kwh"))
                row_cost = _finite(row.get("cost"))
                if row_cost is None and row_kwh is not None and (price := _finite(row.get("price"))) is not None:
                    row_cost = row_kwh * price  # a Cable Juice Planner without the cost per hour
                if hour is None or row_kwh is None or row_cost is None:
                    valid = False  # never count a broken plan as no charging
                    break
                if start <= hour <= end:
                    hours.add(hour)
                    kwh += row_kwh
                    cost += row_cost
            name = str(car.get("name") or "-").replace("|", "/")
            cars.append({"name": name, "hours": len(hours), "kwh": kwh, "cost": cost} if valid else {"name": name, "hours": None, "kwh": None, "cost": None})
        return cars

    def _ev_charging_lines(self, start, end, bill: float, valuta: str, valuta_kwh: str) -> list[str]:
        """🚗 The cars' planned charging in the period and the bill incl. the cars (only with Cable Juice Planner cars)."""
        cars = self._ev_charging_totals(start, end) if start and end else []
        if not cars:
            return []
        t = self.t
        prefix = "ui.cheap_grid_charge_hours.plan_summary"

        def cells(hours: int, kwh: float, cost: float) -> list[str]:
            return [str(hours), f"{kwh:.1f}", f"{cost:.2f} {valuta}", f"{cost / kwh:.2f}" if kwh > 0.0 else "–"]

        lines = [
            "",
            f"**🚗 {t(f'{prefix}.ev_title')}**",
            "",
            f"| {t(f'{prefix}.ev_car')} | {t(f'{prefix}.ev_hours')} | kWh | {t('ui.common.price')} | {valuta_kwh} |",
            "|:---|---:|---:|---:|---:|",
        ]
        for car in cars:
            values = cells(car["hours"], car["kwh"], car["cost"]) if car["kwh"] is not None else ["–", "–", t(f"{prefix}.ev_unavailable"), "–"]
            lines.append(f"| {car['name']} | {' | '.join(values)} |")
        if all(car["kwh"] is not None for car in cars):  # not with a car missing: the totals would be too low
            total_cost = sum(car["cost"] for car in cars)
            if len(cars) > 1:
                total = cells(sum(car["hours"] for car in cars), sum(car["kwh"] for car in cars), total_cost)
                lines.append(f"| **{t(f'{prefix}.total')}** | {' | '.join(f'**{cell}**' for cell in total)} |")
            lines += ["", "|  |  |", "|:---|---:|", f"| **= {t(f'{prefix}.ev_bill')}** | **{bill + total_cost:.2f} {valuta}** |"]
        lines.append(f"\n*{t(f'{prefix}.ev_note')}*")
        return lines

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

        self._plan_overview(overview, charging_plan, days)

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
