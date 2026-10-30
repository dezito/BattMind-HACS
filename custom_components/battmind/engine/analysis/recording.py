"""Recording of the hours, days, prices and plans."""
from __future__ import annotations

import datetime
import logging

from ...const import HISTORY_BUFFER_HOURS, PRICE_ADDER_DAY_BETWEEN_DIVIDER
from ...utils.efficiency import add_efficiency_measurement, measure_efficiency
from ...utils.helpers import days_between, reset_time_to_hour
from ...utils.power import unit_factor_to_w
from .helpers import ANALYSIS_DAYS, FORECAST_TYPES, PRICE_FORECAST_DAYS, PRICE_HOURS_DAYS, _is_num, _num, _r, evaluate_price_day, grid_cost, hindsight_cost, hourly_prices, rules_of_entries, summarize_day

_LOGGER = logging.getLogger(__name__)


class AnalysisRecording:
    """Recording of the hours, days, prices and plans."""

    def analysis_hour_end(self, timestamp: datetime.datetime) -> None:
        """hour_end: record the hour, tomorrow's plan (at 23) and publish. Never fails the hour."""
        try:
            self._analysis_record_hour(timestamp.replace(minute=0, second=0, microsecond=0))
            self._analysis_record_prices()
            if timestamp.hour == 23:
                self._analysis_record_plan(timestamp.date() + datetime.timedelta(days=1))
            self._analysis_record_efficiency()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Analysis of the hour %s failed", timestamp)
        self.publish_analysis()

    def _analysis_row(self, values: dict) -> dict:
        """kWh of one hour from power_values() (W averaged over the hour)."""
        def kwh(key: str) -> float:
            return round(_num(values.get(key)) / 1000, 3)

        return {
            "home": kwh("power_consumption"),
            "ignored": kwh("ignored_consumption"),
            "use": kwh("power_consumption_without_all_exclusion"),
            "solar": kwh("solar_production"),
            "charge": kwh("powerwall_charging_consumption"),
            "discharge": kwh("powerwall_discharging_consumption"),
        }

    def _analysis_record_hour(self, start: datetime.datetime) -> None:
        end = start.replace(minute=59, second=59)
        row = self._analysis_row(self.power_values(start, end))
        row["soc"] = round(self.get_battery_level(), 1)
        row["buy"] = _r(self.get_grid_prices().get(start))
        row["sell"] = _r(self.get_sell_prices().get(start))
        today = self._plan_today()
        predictions = today.get("hour_cost_prediction", {}) if start.date() == self.now().date() else {}
        row["pred"] = {
            kind: round(predictions[kind][start.hour]["kwh"], 3)
            for kind in FORECAST_TYPES
            if isinstance(predictions.get(kind), dict) and isinstance(predictions[kind].get(start.hour), dict)
        }
        hours = self.analysis_db["hours"]
        hours[start.strftime("%Y-%m-%dT%H")] = row
        # a day whose last hour was missed (restart) is finished by the next day
        for date in sorted({key[:10] for key in hours}):
            if date < start.date().isoformat() and not self.analysis_db["days"].get(date, {}).get("final"):
                self._analysis_day(datetime.date.fromisoformat(date), final=True)
        self._analysis_day(start.date(), final=start.hour == 23)
        oldest = (start.date() - datetime.timedelta(days=1)).isoformat()
        for key in [key for key in hours if key[:10] < oldest]:
            del hours[key]
        self.mark_dirty("analysis")

    def _analysis_day(self, date: datetime.date, final: bool) -> None:
        key = date.isoformat()
        rows = [row for hour, row in sorted(self.analysis_db["hours"].items()) if hour[:10] == key]
        if not rows:
            return
        solar = self.config["solar"]
        previous = self.analysis_db["days"].get((date - datetime.timedelta(days=1)).isoformat(), {})
        day = summarize_day(rows, previous.get("soc_end"), solar["powerwall_battery_level_min"])
        if final:
            day["final"] = True
            if (score := self._analysis_score(rows, day)) is not None:
                day["score"] = score
        self.analysis_db["days"][key] = day

    def _analysis_score(self, rows: list[dict], day: dict) -> dict | None:
        """The day's grid cost: actual, without the battery and with the best possible battery use."""
        if len(rows) < 20 or not all(_is_num(row.get("buy")) and _is_num(row.get("sell")) for row in rows):
            return None
        if not _is_num(day.get("soc_start")) or not _is_num(day.get("soc_end")):
            return None
        solar = self.config["solar"]
        wear = abs(solar["powerwall_wear_cost_per_kwh"])
        # the hours store the raw sell price, a sale is worth sell_value() of it
        loads = [
            {"load": _num(row["home"]) - _num(row.get("ignored")) - _num(row["solar"]), "buy": row["buy"], "sell": self.sell_value(row["sell"])}
            for row in rows
        ]
        no_battery = sum(grid_cost(load["load"], load["buy"], load["sell"]) for load in loads)
        actual = sum(
            grid_cost(load["load"] + _num(row["charge"]) - _num(row["discharge"]), load["buy"], load["sell"]) + wear * _num(row["charge"])
            for load, row in zip(loads, rows)
        )
        optimal = hindsight_cost(
            loads,
            self.percentage_to_kwh(day["soc_start"]),
            self.percentage_to_kwh(day["soc_end"]),
            self.percentage_to_kwh(solar["powerwall_battery_level_min"]),
            self.percentage_to_kwh(solar["powerwall_battery_level_max"]),
            self.max_kwh_charging,
            self.max_kwh_discharging,
            self.battery_efficiency(),
            wear,
        )
        if optimal is None:
            return None
        return {"actual": round(actual, 3), "no_battery": round(no_battery, 3), "optimal": round(min(optimal, actual), 3)}

    def _analysis_record_efficiency(self) -> None:
        """Charging and discharging efficiency per AC power step, measured since the last time (utils/efficiency)."""
        ids = self.config["home"]["entity_ids"]
        flow_id, level_id = ids["powerwall_watt_flow_entity_id"], ids["powerwall_battery_level_entity_id"]
        if self._wc is None or not flow_id or not level_id:
            return
        database = self.analysis_db.setdefault("efficiency", {})
        end = self.now().replace(tzinfo=self._wc.tz).timestamp()
        # the buffers hold the states of the last HISTORY_BUFFER_HOURS
        start = max(_num(database.get("until")), end - (HISTORY_BUFFER_HOURS - 2) * 3600)
        measurements, until = measure_efficiency(
            self._wc.buffers.get(flow_id) or [],
            self._wc.buffers.get(level_id) or [],
            start,
            end,
            unit_factor_to_w(self._wc.units.get(flow_id)),
            self.config["home"]["invert_powerwall_watt_flow_entity_id"],
            self.config["solar"]["powerwall_battery_size"],
        )
        for direction, ac_kwh, battery_kwh, seconds in measurements:
            add_efficiency_measurement(database.setdefault(direction, {}), ac_kwh, battery_kwh, seconds)
        database["until"] = until
        self.mark_dirty("analysis")

    def _analysis_record_prices(self) -> None:
        """The buy price forecast per days ahead, the real prices and the days they are all known for."""
        provider = self.price_provider
        if provider is None:
            return
        current_hour = reset_time_to_hour(self.now())
        hours = self.analysis_db["price_hours"]
        sources = provider.buy_price_sources
        forecast = {ts: price for ts, price in provider.buy_prices.items() if str(sources.get(ts, "")).endswith(":forecast")}
        for hour, price in hourly_prices(forecast).items():
            lead = days_between(current_hour, hour)
            if 1 <= lead <= PRICE_FORECAST_DAYS:
                # the providers add lead / divider to forecasts to prefer known prices
                hours.setdefault(hour.strftime("%Y-%m-%dT%H"), {})[str(lead)] = round(price - lead / PRICE_ADDER_DAY_BETWEEN_DIVIDER, 3)
        for hour, price in hourly_prices(provider.buy_real_prices).items():
            hours.setdefault(hour.strftime("%Y-%m-%dT%H"), {})["real"] = round(price, 3)

        price_days = self.analysis_db["price_days"]
        by_date: dict[str, list[dict]] = {}
        for key in sorted(hours):
            by_date.setdefault(key[:10], []).append(hours[key])
        for date, rows in by_date.items():
            # a whole day (23 hours on the day summer time starts)
            if date not in price_days and sum(1 for row in rows if _is_num(row.get("real"))) >= 23:
                price_days[date] = evaluate_price_day(rows)
        oldest = (current_hour.date() - datetime.timedelta(days=PRICE_HOURS_DAYS)).isoformat()
        for key in [key for key in hours if key[:10] < oldest]:
            del hours[key]
        self.mark_dirty("analysis")

    def _analysis_record_plan(self, date: datetime.date) -> None:
        """The evening before: grid charging, sale and solar surplus planned for date."""
        plan_day = next(
            (day for day in self.plan.values()
             if isinstance(day, dict) and isinstance(day.get("start_of_day"), datetime.datetime) and day["start_of_day"].date() == date),
            {},
        )
        grid = sum(
            _num(value.get("kWh")) for timestamp, value in self.charge_hours.items()
            if isinstance(timestamp, datetime.datetime) and timestamp.date() == date and isinstance(value, dict)
        )
        sale = sum(_num(value.get("kwh")) for value in plan_day.get("force_discharge_timestamps", {}).values() if isinstance(value, dict))
        entry = {"grid": round(grid, 2), "sale": round(sale, 2)}
        prediction = self.solar_prediction_db.get(1) or {}
        if self.is_solar_configured() and prediction.get("total"):
            entry["solar"] = round(sum(prediction["total"]), 2)
            entry["solar_database"] = round(sum(prediction["total_database"]), 2) if prediction.get("total_database") else None
            entry["solar_forecast"] = round(sum(prediction["total_forecast"]), 2) if prediction.get("total_forecast") else None
        self.analysis_db["plans"][date.isoformat()] = entry
        self.mark_dirty("analysis")

    def _analysis_rule_days(self) -> None:
        """Charged/discharged per rule and day from the hourly charging history entries.

        A day with more than one entry still has its hours; a day with one entry may be
        combined already and keeps what was saved before (combined days are only used the
        first time, with the rules of the whole day).
        """
        by_date: dict[str, list[dict]] = {}
        for timestamp, entry in self.charging_history_db.items():
            if isinstance(timestamp, datetime.datetime) and isinstance(entry, dict):
                by_date.setdefault(timestamp.date().isoformat(), []).append(entry)
        rule_days = self.analysis_db["rule_days"]
        changed = False
        for date, entries in by_date.items():
            if len(entries) == 1 and date in rule_days:
                continue
            rules = rules_of_entries(entries)
            if rule_days.get(date) != rules:
                rule_days[date] = rules
                changed = True
        if changed:
            self.mark_dirty("analysis")

    def _analysis_prune(self) -> None:
        oldest = (self.now().date() - datetime.timedelta(days=ANALYSIS_DAYS)).isoformat()
        for name in ("days", "plans", "rule_days", "price_days"):
            table = self.analysis_db[name]
            for key in [key for key in table if key < oldest]:
                del table[key]
