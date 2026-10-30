"""Analysis (engine part): what the home battery earned and how well BattMind planned.

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
- price_hours: the buy price forecast of the price integration per hour and days ahead
  (without BattMind's daily adder), and the real price once it is known; kept a week.
- price_days: when all real prices of a day are known, the forecast error per days ahead and
  how well the forecast found the day's cheapest and most expensive hours.
- efficiency: AC and battery kWh per AC power step for charging and discharging, measured between
  changes of the battery level (utils/efficiency); the measured efficiency curves.

The first time the energy per hour of the last year is read from the recorder's long-term
statistics (coordinator); forecasts, plans and scores only exist from now on. Everything is
published per month and day on sensor.<prefix>_analysis.
"""
from __future__ import annotations

import datetime
import logging

from ..energy.energy import derive_power_values
from ...utils.power import unit_factor_to_w
from .publishing import AnalysisPublishing
from .recording import AnalysisRecording
from .helpers import ANALYSIS_DAYS, ANALYSIS_BACKFILL_VERSION, PRICE_FORECAST_DAYS, PRICE_CORRECTION_DAYS, PRICE_CORRECTION_MIN_DAYS, PRICE_CORRECTION_MAX, _num, _is_num, _r, summarize_day

_LOGGER = logging.getLogger(__name__)


class Analysis(AnalysisRecording, AnalysisPublishing):
    """The analysis database and sensor.<prefix>_analysis."""

    def load_analysis_db(self, database) -> None:
        db = database if isinstance(database, dict) else {}
        for key in ("hours", "days", "plans", "rule_days", "price_hours", "price_days"):
            if not isinstance(db.get(key), dict):
                db[key] = {}
        self.analysis_db = db

    def price_forecast_corrections(self) -> dict[int, float]:
        """Currency/kWh added to the price forecast per days ahead: minus its bias the last 30 days.

        Only for days ahead with PRICE_CORRECTION_MIN_DAYS judged days, at most
        PRICE_CORRECTION_MAX either way, and only when the option is on.
        """
        if not self.enabled("price_forecast_correction"):
            return {}
        first = (self.now().date() - datetime.timedelta(days=PRICE_CORRECTION_DAYS)).isoformat()
        selected = [day for date, day in self.analysis_db.get("price_days", {}).items() if date >= first and isinstance(day, dict)]
        corrections = {}
        for lead in range(1, PRICE_FORECAST_DAYS + 1):
            entries = [day[str(lead)] for day in selected if isinstance(day.get(str(lead)), dict)]
            hours = sum(_num(entry.get("n")) for entry in entries)
            if len(entries) >= PRICE_CORRECTION_MIN_DAYS and hours:
                bias = sum(_num(entry.get("bias")) for entry in entries) / hours
                corrections[lead] = round(min(max(-bias, -PRICE_CORRECTION_MAX), PRICE_CORRECTION_MAX), 3)
        return corrections

    def analysis_backfilled(self) -> bool:
        return self.analysis_db.get("backfilled") == ANALYSIS_BACKFILL_VERSION

    # hourly ------------------------------------------------------------ #

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

    # publish ----------------------------------------------------------- #
