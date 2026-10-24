"""Analysis (engine mixin): what the home battery earned and how well BattMind planned.

Recorded every hour (hour_end) in the analysis database, from the same power values as the
charging history:

- hours: the energy of the last 2 days (house, solar, battery), the battery level, the
  buy/sell price and the consumption the plan predicted for the hour per forecast type.
- days: the hours added up per day: where the house got its energy from (self-sufficiency),
  what went in and out of the battery (measured round trip loss), the error of each
  consumption forecast type, the most expensive hours with an empty battery and, when the
  day is over, the hindsight score: the saving reached compared with the best possible
  use of the battery with that day's consumption, solar and prices.
- plans: every evening the grid charging, sale and solar surplus planned for tomorrow.
- rule_days: charged/discharged kWh, cost and savings per charging rule, from the hourly
  entries of the charging history (the history combines old hours per day and loses the
  rules of each hour).

The first time the energy per hour of the last year is read from the recorder's long-term
statistics (coordinator); forecasts, plans and scores only exist from now on. Everything is
published per month and day on sensor.<prefix>_analysis.
"""
from __future__ import annotations

import datetime
import logging

from .const import CHARGING_TYPES
from .engine_energy import derive_power_values
from .power import unit_factor_to_w

_LOGGER = logging.getLogger(__name__)

ANALYSIS_DAYS = 400  # days kept in the database
# raised when the backfill reads more: the days from the statistics are read again (2: prices with state_class total)
ANALYSIS_BACKFILL_VERSION = 2
ANALYSIS_MONTHS = 13  # months published
ANALYSIS_LIST_DAYS = 31  # days published for the daily charts
ANALYSIS_TABLE_DAYS = 14  # days published for the daily tables
EXPENSIVE_HOURS = 4  # the most expensive hours of a day that are checked for an empty battery
EMPTY_MARGIN = 2.0  # % above the minimum battery level that counts as empty
SCORE_STEPS = 60  # battery levels in the hindsight optimisation
FORECAST_TYPES = ("ema", "trend", "average")
# rules that charge from the grid / that discharge (sell) the battery, see CHARGING_TYPES
CHARGE_RULES = (
    "cheapest_hour_fill_planner", "most_expensive_planner", "needed_before_max_level_planner",
    "negative_price_planner", "grid_charge_to_sell",
)
SALE_RULES = (
    "most_profit", "sell_excess_kwh_available", "force_powerwall_max_hours_old_discharge",
    "discharge_before_solar", "grid_charge_to_sell",
)
DAY_SUMS = (
    "home", "ignored", "solar", "charged", "discharged", "solar_home", "battery_home", "grid_home",
    "solar_battery", "grid_battery", "export_solar", "export_battery", "solar_surplus",
)
MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)


def _num(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _r(value, decimals: int = 3):
    return round(float(value), decimals) if _is_num(value) else None


def split_flows(home: float, solar: float, charge: float, discharge: float) -> dict[str, float]:
    """kWh of an hour by source: solar first to the house, then the battery, the rest from the grid."""
    solar_home = min(solar, home)
    battery_home = min(discharge, home - solar_home)
    solar_left = solar - solar_home
    solar_battery = min(charge, solar_left)
    return {
        "solar_home": solar_home,
        "battery_home": battery_home,
        "grid_home": max(home - solar_home - battery_home, 0.0),
        "solar_battery": solar_battery,
        "grid_battery": charge - solar_battery,
        "export_solar": solar_left - solar_battery,
        "export_battery": discharge - battery_home,
    }


def grid_cost(kwh: float, buy: float, sell: float) -> float:
    """Cost of kWh from the grid (negative kWh: sold at the sell price)."""
    return kwh * buy if kwh > 0 else kwh * sell


def hindsight_cost(rows: list[dict], start: float, end: float, low: float, high: float,
                   max_charge: float, max_discharge: float, efficiency: float, wear: float) -> float | None:
    """Lowest grid cost of a day with perfect knowledge of consumption, solar and prices.

    rows: per hour {"load": kWh house - solar (negative = surplus), "buy", "sell"}. The battery
    starts with start kWh, ends with at least end kWh and stays within low..high; charging and
    discharging are limited per hour and lose efficiency each way, wear is paid per kWh charged.
    """
    if high - low <= 0.01 or efficiency <= 0.0:
        return None
    step = (high - low) / SCORE_STEPS
    up = int(max_charge * efficiency / step + 1e-9)
    down = int(max_discharge / efficiency / step + 1e-9)
    inf = float("inf")
    costs = [inf] * (SCORE_STEPS + 1)
    costs[min(max(round((start - low) / step), 0), SCORE_STEPS)] = 0.0
    for row in rows:
        new = [inf] * (SCORE_STEPS + 1)
        for i, cost in enumerate(costs):
            if cost == inf:
                continue
            for j in range(max(i - down, 0), min(i + up, SCORE_STEPS) + 1):
                delta = (j - i) * step
                if delta > 0:
                    ac = delta / efficiency
                    total = cost + grid_cost(row["load"] + ac, row["buy"], row["sell"]) + wear * ac
                else:
                    total = cost + grid_cost(row["load"] + delta * efficiency, row["buy"], row["sell"])
                if total < new[j]:
                    new[j] = total
        costs = new
    target = min(max(round((end - low) / step), 0), SCORE_STEPS)
    best = min(costs[target:])
    return None if best == inf else best


def summarize_day(rows: list[dict], soc_start: float | None, minimum: float) -> dict:
    """The hours of a day (sorted) added up."""
    day = dict.fromkeys(DAY_SUMS, 0.0)
    price_sum = price_hours = 0.0
    errors = {kind: [0.0, 0.0] for kind in FORECAST_TYPES}  # [sum |error|, sum error]
    predicted_hours = 0
    predicted_use = 0.0
    if soc_start is None and rows and _is_num(rows[0].get("soc")):
        # no previous day: the level at the end of the first hour
        soc_start = rows[0]["soc"]
    level = soc_start
    expensive: list[tuple[float, float | None, dict, dict]] = []
    for row in rows:
        home, solar = _num(row.get("home")), _num(row.get("solar"))
        charge, discharge = _num(row.get("charge")), _num(row.get("discharge"))
        flows = split_flows(home, solar, charge, discharge)
        for key, value in flows.items():
            day[key] += value
        day["home"] += home
        day["ignored"] += _num(row.get("ignored"))
        day["solar"] += solar
        day["charged"] += charge
        day["discharged"] += discharge
        day["solar_surplus"] += max(solar - _num(row.get("use")), 0.0)
        if _is_num(row.get("buy")):
            price_sum += row["buy"]
            price_hours += 1
            expensive.append((row["buy"], level, flows, row))
        prediction = row.get("pred") or {}
        if all(_is_num(prediction.get(kind)) for kind in FORECAST_TYPES):
            predicted_hours += 1
            predicted_use += _num(row.get("use"))
            for kind in FORECAST_TYPES:
                error = prediction[kind] - _num(row.get("use"))
                errors[kind][0] += abs(error)
                errors[kind][1] += error
        if _is_num(row.get("soc")):
            level = row["soc"]

    result = {key: round(value, 3) for key, value in day.items()}
    result["hours"] = len(rows)
    result["price_sum"] = round(price_sum, 3)
    result["price_hours"] = int(price_hours)
    result["soc_start"] = _r(soc_start, 1)
    result["soc_end"] = _r(level, 1)
    if predicted_hours:
        result["forecast"] = {
            "hours": predicted_hours,
            "use": round(predicted_use, 3),
            **{kind: [round(errors[kind][0], 3), round(errors[kind][1], 3)] for kind in FORECAST_TYPES},
        }
    # the most expensive hours: was the battery empty while the house used grid power?
    empty = [
        (price, flows["grid_home"]) for price, start_level, flows, _ in sorted(expensive, key=lambda item: item[0], reverse=True)[:EXPENSIVE_HOURS]
        if start_level is not None and start_level <= minimum + EMPTY_MARGIN and flows["grid_home"] >= 0.05
    ]
    if expensive:
        result["expensive"] = {
            "hours": len(empty),
            "kwh": round(sum(kwh for _, kwh in empty), 3),
            "cost": round(sum(price * kwh for price, kwh in empty), 3),
        }
    return result


def rules_of_entries(entries: list[dict]) -> dict[str, dict[str, float]]:
    """Charging history entries of a day per rule: charged kWh (c) and cost (cc), discharged kWh (d),
    their value (v: what the house did not buy + sales) and the savings (s)."""
    result: dict[str, dict[str, float]] = {}

    def add(rule: str, share: float, **values: float) -> None:
        bucket = result.setdefault(rule, {"c": 0.0, "cc": 0.0, "d": 0.0, "v": 0.0, "s": 0.0})
        for key, value in values.items():
            bucket[key] += value * share

    for entry in entries:
        rules = {key for key, value in (entry.get("rules") or {}).items() if value is True}
        charged = entry.get("charged") if isinstance(entry.get("charged"), dict) else {}
        discharged = entry.get("discharged") if isinstance(entry.get("discharged"), dict) else {}
        prices = entry.get("prices") if isinstance(entry.get("prices"), dict) else {}
        c_kwh = _num(charged.get("kWh"))
        if c_kwh > 0.0:
            cost = _num(charged.get("cost"))
            local = min(_num(charged.get("kWh_from_local_energy")), c_kwh)
            grid = c_kwh - local
            # the cost split like the charge price: solar at the sell price, the rest at the buy price
            weight_local = local * max(_num(prices.get("sell_price")), 0.0)
            weight_grid = grid * max(_num(prices.get("buy_price")), 0.0)
            local_cost = cost * weight_local / (weight_local + weight_grid) if weight_local + weight_grid > 0 else cost * local / c_kwh
            if local > 0.0:
                add("solar", 1.0, c=local, cc=local_cost)
            if grid > 0.0:
                charge_rules = [rule for rule in CHARGE_RULES if rule in rules] or ["no_rule"]
                for rule in charge_rules:
                    add(rule, 1 / len(charge_rules), c=grid, cc=cost - local_cost)
        d_kwh = _num(discharged.get("kWh"))
        if d_kwh > 0.0:
            savings = _num(discharged.get("savings"))
            value = savings + _num(discharged.get("cost"))
            sale_rules = [rule for rule in SALE_RULES if rule in rules] or (["force_discharge"] if "force_discharge" in rules else ["discharging"])
            for rule in sale_rules:
                add(rule, 1 / len(sale_rules), d=d_kwh, v=value, s=savings)
    return {rule: {key: round(value, 3) for key, value in bucket.items()} for rule, bucket in result.items()}


class AnalysisMixin:
    """The analysis database and sensor.<prefix>_analysis."""

    def load_analysis_db(self, database) -> None:
        db = database if isinstance(database, dict) else {}
        for key in ("hours", "days", "plans", "rule_days"):
            if not isinstance(db.get(key), dict):
                db[key] = {}
        self.analysis_db = db

    def analysis_backfilled(self) -> bool:
        return self.analysis_db.get("backfilled") == ANALYSIS_BACKFILL_VERSION

    # hourly ------------------------------------------------------------ #

    def analysis_hour_end(self, timestamp: datetime.datetime) -> None:
        """hour_end: record the hour, tomorrow's plan (at 23) and publish. Never fails the hour."""
        try:
            self._analysis_record_hour(timestamp.replace(minute=0, second=0, microsecond=0))
            if timestamp.hour == 23:
                self._analysis_record_plan(timestamp.date() + datetime.timedelta(days=1))
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

    # backfill ---------------------------------------------------------- #

    def analysis_update(self, data: dict) -> None:
        """The first time: the energy per hour from the long-term statistics {entity: [(ts, mean)]}."""
        stats = data.get("stats") or {}
        home = self.config["home"]
        ids = home["entity_ids"]
        units = self._snap.units if self._snap else {}
        tz = self._snap.tz if self._snap else None

        def series(entity_id: str, power: bool = True) -> dict[float, float]:
            factor = unit_factor_to_w(units.get(entity_id)) if power else 1.0
            return {float(ts): float(value) * factor for ts, value in stats.get(entity_id) or [] if _is_num(value)}

        consumption = series(ids["power_consumption_entity_id"])
        flow = series(ids["powerwall_watt_flow_entity_id"])
        solar = series(self.config["solar"]["entity_ids"]["production_entity_id"])
        ignored = [series(entity_id) for entity_id in ids["ignore_consumption_from_entity_ids"] or [] if entity_id]
        soc = series(ids["powerwall_battery_level_entity_id"], power=False)
        price = series(data.get("price_entity_id") or "", power=False)
        positive_is_charging = home["invert_powerwall_watt_flow_entity_id"]

        by_date: dict[str, dict[str, dict]] = {}
        for ts in sorted(consumption):
            hour = datetime.datetime.fromtimestamp(ts, tz).replace(tzinfo=None)
            battery = flow.get(ts, 0.0)
            charging = max(battery if positive_is_charging else -battery, 0.0)
            discharging = max(-battery if positive_is_charging else battery, 0.0)
            values = derive_power_values(
                home, abs(consumption[ts]), sum(abs(s.get(ts, 0.0)) for s in ignored), charging, discharging, abs(solar.get(ts, 0.0)),
            )
            row = self._analysis_row(values)
            row["soc"] = _r(soc.get(ts), 1)
            row["buy"] = _r(price.get(ts))
            by_date.setdefault(hour.date().isoformat(), {})[hour.strftime("%Y-%m-%dT%H")] = row

        today = self.now().date()
        recent = (today - datetime.timedelta(days=1)).isoformat()
        oldest = (today - datetime.timedelta(days=ANALYSIS_DAYS)).isoformat()
        days = self.analysis_db["days"]
        minimum = self.config["solar"]["powerwall_battery_level_min"]
        previous_soc = None
        for date in sorted(by_date):
            rows = by_date[date]
            if date < oldest:
                continue
            if date >= recent:
                for key, row in rows.items():
                    known = self.analysis_db["hours"].setdefault(key, row)
                    if known.get("buy") is None and row.get("buy") is not None:
                        known["buy"] = row["buy"]
            elif date not in days or days[date].get("statistics"):
                days[date] = {**summarize_day([rows[key] for key in sorted(rows)], previous_soc, minimum), "final": True, "statistics": True}
            previous_soc = days.get(date, {}).get("soc_end", previous_soc)
        for date in (today - datetime.timedelta(days=1), today):
            self._analysis_day(date, final=date < today)
        self.analysis_db["backfilled"] = ANALYSIS_BACKFILL_VERSION
        self.mark_dirty("analysis")
        _LOGGER.info("Analysis: %s days read from the long-term statistics", len(by_date))
        self.publish_analysis()

    # rules ------------------------------------------------------------- #

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

    # publish ----------------------------------------------------------- #

    def _analysis_prune(self) -> None:
        oldest = (self.now().date() - datetime.timedelta(days=ANALYSIS_DAYS)).isoformat()
        for name in ("days", "plans", "rule_days"):
            table = self.analysis_db[name]
            for key in [key for key in table if key < oldest]:
                del table[key]

    def _month_label(self, month: str) -> str:
        name = MONTH_NAMES[int(month[5:7]) - 1]
        return f"{self.t(f'ui.calendar.month_names.{name}', default=name.capitalize())[:3]} {month[2:4]}"

    def publish_analysis(self) -> None:
        try:
            self._analysis_rule_days()
            self._analysis_prune()
            self._publish_analysis()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Publishing the analysis failed")

    def _publish_analysis(self) -> None:
        solar_cfg = self.config["solar"]
        size = float(solar_cfg["powerwall_battery_size"]) or 1.0
        wear = abs(solar_cfg["powerwall_wear_cost_per_kwh"])
        today = self.now().date()
        days = self.analysis_db["days"]
        closed = {date: day for date, day in days.items() if date < today.isoformat()}

        # economy per month from the charging history ------------------------
        economy: dict[str, dict[str, float]] = {}
        for timestamp, entry in self.charging_history_db.items():
            if not isinstance(timestamp, datetime.datetime) or not isinstance(entry, dict):
                continue
            charged = entry.get("charged") if isinstance(entry.get("charged"), dict) else {}
            discharged = entry.get("discharged") if isinstance(entry.get("discharged"), dict) else {}
            month = economy.setdefault(timestamp.strftime("%Y-%m"), dict.fromkeys(("c", "cl", "cc", "d", "dc", "s"), 0.0))
            c_kwh = _num(charged.get("kWh"))
            month["c"] += c_kwh
            month["cl"] += min(_num(charged.get("kWh_from_local_energy")), c_kwh)
            month["cc"] += _num(charged.get("cost"))
            month["d"] += _num(discharged.get("kWh"))
            month["dc"] += _num(discharged.get("cost"))
            month["s"] += _num(discharged.get("savings"))

        # energy per month from the days -------------------------------------
        energy: dict[str, dict] = {}
        for date in sorted(days):
            day = days[date]
            month = energy.setdefault(date[:7], {**dict.fromkeys(DAY_SUMS, 0.0), "price_sum": 0.0, "price_hours": 0, "soc_start": None, "soc_end": None, "days": 0})
            for key in (*DAY_SUMS, "price_sum", "price_hours"):
                month[key] += _num(day.get(key))
            if month["soc_start"] is None:
                month["soc_start"] = day.get("soc_start")
            if _is_num(day.get("soc_end")):
                month["soc_end"] = day["soc_end"]
            month["days"] += 1

        def loss_of(charged: float, discharged: float, soc_start, soc_end) -> float | None:
            """Measured round trip loss: what went in and did not come out (corrected for the level change)."""
            if charged < size:
                return None
            stored = self.percentage_to_kwh(soc_end - soc_start) if _is_num(soc_start) and _is_num(soc_end) else 0.0
            return round(1 - (discharged + stored) / charged, 3)

        months = []
        for key in sorted(set(economy) | set(energy))[-ANALYSIS_MONTHS:]:
            e = economy.get(key, dict.fromkeys(("c", "cl", "cc", "d", "dc", "s"), 0.0))
            f = energy.get(key)
            value = e["s"] + e["dc"]
            row = {
                "month": key,
                "label": self._month_label(key),
                "charged": round(e["c"], 1),
                "charged_grid": round(e["c"] - e["cl"], 1),
                "charged_solar": round(e["cl"], 1),
                "charged_cost": round(e["cc"], 2),
                "charge_unit": round(e["cc"] / e["c"], 3) if e["c"] > 0.05 else None,
                "discharged": round(e["d"], 1),
                "value_unit": round(value / e["d"], 3) if e["d"] > 0.05 else None,
                "savings": round(e["s"], 2),
                "savings_unit": round(e["s"] / e["d"], 3) if e["d"] > 0.05 else None,
                "cycles": round(e["d"] / size, 1),
                "wear": round(e["c"] * wear, 2),
                "savings_per_cycle": round(e["s"] / (e["d"] / size), 2) if e["d"] >= size else None,
            }
            row["spread"] = round(row["value_unit"] - row["charge_unit"], 3) if row["value_unit"] is not None and row["charge_unit"] is not None else None
            if f:
                home = f["home"]
                row.update(
                    avg_price=round(f["price_sum"] / f["price_hours"], 3) if f["price_hours"] else None,
                    home=round(home, 1),
                    solar=round(f["solar"], 1),
                    solar_home=round(f["solar_home"], 1),
                    battery_home=round(f["battery_home"], 1),
                    grid_home=round(f["grid_home"], 1),
                    export=round(f["export_solar"] + f["export_battery"], 1),
                    self_sufficiency=round((f["solar_home"] + f["battery_home"]) / home, 3) if home > 0 else None,
                    solar_share=round(f["solar_home"] / home, 3) if home > 0 else None,
                    battery_share=round(f["battery_home"] / home, 3) if home > 0 else None,
                    measured_charged=round(f["charged"], 1),
                    measured_discharged=round(f["discharged"], 1),
                    loss=loss_of(f["charged"], f["discharged"], f["soc_start"], f["soc_end"]),
                )
            months.append(row)

        def window(first: datetime.date) -> list[dict]:
            return [closed[date] for date in sorted(closed) if date >= first.isoformat()]

        last_30 = window(today - datetime.timedelta(days=30))
        last_7 = window(today - datetime.timedelta(days=7))
        all_days = window(datetime.date.min)

        def loss_window(selected: list[dict]) -> float | None:
            if not selected:
                return None
            return loss_of(
                sum(_num(day.get("charged")) for day in selected), sum(_num(day.get("discharged")) for day in selected),
                selected[0].get("soc_start"), selected[-1].get("soc_end"),
            )

        def sufficiency(selected: list[dict]) -> float | None:
            home = sum(_num(day.get("home")) for day in selected)
            return round(sum(_num(day.get("solar_home")) + _num(day.get("battery_home")) for day in selected) / home, 3) if home > 0 else None

        # rules over the last 12 months ---------------------------------------
        first_rule_day = (today - datetime.timedelta(days=365)).isoformat()
        totals: dict[str, dict[str, float]] = {}
        sale_kinds: dict[str, dict[str, float]] = {}
        for date, rules in self.analysis_db["rule_days"].items():
            for rule, bucket in rules.items():
                if date >= first_rule_day:
                    total = totals.setdefault(rule, dict.fromkeys(("c", "cc", "d", "v", "s"), 0.0))
                    for key in total:
                        total[key] += _num(bucket.get(key))
                if _num(bucket.get("d")) > 0:
                    kind = sale_kinds.setdefault(date[:7], {"home": 0.0, "sale": 0.0})
                    kind["home" if rule == "discharging" else "sale"] += _num(bucket.get("s"))
        discharged = sum(total["d"] for total in totals.values())
        value_unit = sum(total["v"] for total in totals.values()) / discharged if discharged > 0.05 else None
        efficiency_loss = min(abs(self.get_charging_loss()), 0.9)
        rules = []
        for rule, total in totals.items():
            entry = {
                "rule": rule,
                "emoji": CHARGING_TYPES.get(rule, {}).get("emoji", ""),
                "name": self.t(f"charging_type.{rule}.description", default=rule),
                "charged": round(total["c"], 1),
                "charged_cost": round(total["cc"], 2),
                "charge_unit": round(total["cc"] / total["c"], 3) if total["c"] > 0.05 else None,
                "discharged": round(total["d"], 1),
                "savings": round(total["s"], 2) if total["d"] > 0 else None,
                # what the charged kWh are worth when used like the battery's discharges on average
                "estimated": round(total["c"] * (1 - efficiency_loss) * value_unit - total["cc"] - total["c"] * wear, 2)
                if total["c"] > 0.05 and value_unit is not None else None,
            }
            rules.append(entry)
        rules.sort(key=lambda entry: (entry["discharged"] > 0, -(entry["charged"] + entry["discharged"])))
        for row in months:
            row["savings_home"] = round(sale_kinds.get(row["month"], {}).get("home", 0.0), 2)
            row["savings_sale"] = round(sale_kinds.get(row["month"], {}).get("sale", 0.0), 2)

        # consumption forecast per type ----------------------------------------
        def forecast_errors(selected: list[dict]) -> dict | None:
            hours = sum(_num(day.get("forecast", {}).get("hours")) for day in selected)
            use = sum(_num(day.get("forecast", {}).get("use")) for day in selected)
            if not hours:
                return None
            result = {"hours": int(hours)}
            for kind in FORECAST_TYPES:
                absolute = sum(_num((day.get("forecast", {}).get(kind) or [0, 0])[0]) for day in selected)
                bias = sum(_num((day.get("forecast", {}).get(kind) or [0, 0])[1]) for day in selected)
                result[kind] = {
                    "mae": round(absolute / hours, 3),
                    "bias": round(bias / hours, 3),
                    "relative": round(absolute / use, 3) if use > 0 else None,
                }
            return result

        forecast_30 = forecast_errors(last_30)
        forecast = {
            "selected": self.get_forecast_type(),
            "last_7": forecast_errors(last_7),
            "last_30": forecast_30,
            "best": min(FORECAST_TYPES, key=lambda kind: forecast_30[kind]["mae"]) if forecast_30 else None,
            "days": [
                {"date": date, **{kind: round(day["forecast"][kind][0] / day["forecast"]["hours"], 3) for kind in FORECAST_TYPES}}
                for date, day in sorted(closed.items())[-ANALYSIS_LIST_DAYS:]
                if day.get("forecast", {}).get("hours")
            ],
        }

        # plans against actual -------------------------------------------------
        plans = []
        for date, plan in sorted(self.analysis_db["plans"].items()):
            day = closed.get(date)
            if day is None:
                continue
            plans.append({
                "date": date,
                "plan_grid": plan.get("grid"),
                "grid": round(_num(day.get("grid_battery")), 2),
                "plan_sale": plan.get("sale"),
                "sale": round(_num(day.get("export_battery")), 2),
                "plan_solar": plan.get("solar"),
                "solar_database": plan.get("solar_database"),
                "solar_forecast": plan.get("solar_forecast"),
                "solar": round(_num(day.get("solar_surplus")), 2),
            })
        plans = plans[-ANALYSIS_LIST_DAYS:]

        def solar_errors(key: str) -> dict | None:
            pairs = [(row[key], row["solar"]) for row in plans if _is_num(row.get(key))]
            if not pairs:
                return None
            return {
                "days": len(pairs),
                "mae": round(sum(abs(p - a) for p, a in pairs) / len(pairs), 2),
                "bias": round(sum(p - a for p, a in pairs) / len(pairs), 2),
            }

        # hindsight score --------------------------------------------------------
        scores = []
        for date, day in sorted(closed.items()):
            score = day.get("score")
            if not score:
                continue
            saved = score["no_battery"] - score["actual"]
            possible = score["no_battery"] - score["optimal"]
            scores.append({
                "date": date,
                "saved": round(saved, 2),
                "possible": round(possible, 2),
                "score": round(min(max(saved / possible, -1.0), 1.0), 3) if possible > 0.05 else None,
            })
        recent_scores = [row for row in scores if row["date"] >= (today - datetime.timedelta(days=30)).isoformat()]
        possible_30 = sum(row["possible"] for row in recent_scores)

        # expensive hours with an empty battery ----------------------------------
        expensive = [
            {"date": date, **day["expensive"]} for date, day in sorted(closed.items()) if isinstance(day.get("expensive"), dict)
        ]
        expensive_30 = [row for row in expensive if row["date"] >= (today - datetime.timedelta(days=30)).isoformat()]

        this_month = next((row for row in months if row["month"] == today.strftime("%Y-%m")), {})
        self.set_state("analysis", this_month.get("savings", 0.0))
        attributes = {
            "battery_size": size,
            "months": months,
            "total": {
                "savings": round(sum(row["savings"] for row in months), 2),
                "cycles": round(sum(row["cycles"] for row in months), 1),
                "charged": round(sum(row["charged"] for row in months), 1),
                "discharged": round(sum(row["discharged"] for row in months), 1),
                "wear": round(sum(row["wear"] for row in months), 2),
            },
            "rules": rules,
            "loss": {
                "configured": round(solar_cfg["powerwall_charge_discharge_loss"], 3),
                "used": round(self.get_charging_loss(), 3),
                "last_30": loss_window(last_30),
                "all": loss_window(all_days),
            },
            "self_sufficiency": {"last_7": sufficiency(last_7), "last_30": sufficiency(last_30), "all": sufficiency(all_days)},
            "forecast": forecast,
            "plans": plans,
            "solar_forecast": {key: solar_errors(source) for key, source in (("plan", "plan_solar"), ("database", "solar_database"), ("forecast", "solar_forecast"))},
            "scores": scores[-ANALYSIS_TABLE_DAYS:],
            "score_30": round(sum(row["saved"] for row in recent_scores) / possible_30, 3) if possible_30 > 0.05 else None,
            "score_days": len(recent_scores),
            "expensive": expensive[-ANALYSIS_TABLE_DAYS:],
            "expensive_30": {
                "days": len(expensive_30),
                "days_empty": sum(1 for row in expensive_30 if row["hours"]),
                "hours": sum(row["hours"] for row in expensive_30),
                "kwh": round(sum(row["kwh"] for row in expensive_30), 1),
                "cost": round(sum(row["cost"] for row in expensive_30), 2),
            },
        }
        for key, value in attributes.items():
            self.set_attr("analysis", key, value)
