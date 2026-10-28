"""The analysis published as entity states."""
from __future__ import annotations

import datetime
import logging

from ...const import CHARGING_TYPES
from ...utils.efficiency import parse_efficiency_curve
from .helpers import ANALYSIS_LIST_DAYS, ANALYSIS_MONTHS, ANALYSIS_TABLE_DAYS, DAY_SUMS, FORECAST_TYPES, MONTH_NAMES, PRICE_CHART_DAYS, PRICE_CHART_LEADS, PRICE_CORRECTION_DAYS, PRICE_CORRECTION_MIN_DAYS, PRICE_FORECAST_DAYS, PRICE_RANK_HOURS, _is_num, _num

_LOGGER = logging.getLogger(__name__)


class AnalysisPublishing:
    """The analysis published as entity states."""

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
        efficiency_loss = self.effective_round_trip_loss()
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
            # the measured consumption per hour, and per type what it forecast on average (measured + bias)
            result = {"hours": int(hours), "actual": round(use / hours, 3)}
            for kind in FORECAST_TYPES:
                absolute = sum(_num((day.get("forecast", {}).get(kind) or [0, 0])[0]) for day in selected)
                bias = sum(_num((day.get("forecast", {}).get(kind) or [0, 0])[1]) for day in selected)
                result[kind] = {
                    "mae": round(absolute / hours, 3),
                    "bias": round(bias / hours, 3),
                    "relative": round(absolute / use, 3) if use > 0 else None,
                    "forecast": round((use + bias) / hours, 3),
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

        # price forecast against the real prices ---------------------------------
        def price_errors(first: datetime.date) -> list[dict]:
            selected = [day for date, day in self.analysis_db["price_days"].items() if date >= first.isoformat()]
            result = []
            for lead in range(1, PRICE_FORECAST_DAYS + 1):
                entries = [day[str(lead)] for day in selected if isinstance(day.get(str(lead)), dict)]
                hours = sum(_num(entry.get("n")) for entry in entries)
                if not hours:
                    continue
                ranked = len(entries) * PRICE_RANK_HOURS
                result.append({
                    "lead": lead,
                    "days": len(entries),
                    "mae": round(sum(_num(entry.get("abs")) for entry in entries) / hours, 3),
                    "bias": round(sum(_num(entry.get("bias")) for entry in entries) / hours, 3),
                    "max": round(max(_num(entry.get("max")) for entry in entries), 3),
                    "cheap": round(sum(_num(entry.get("cheap")) for entry in entries) / ranked, 3),
                    "cheap_miss": round(sum(_num(entry.get("cheap_miss")) for entry in entries) / len(entries), 3),
                    "dear": round(sum(_num(entry.get("dear")) for entry in entries) / ranked, 3),
                    "dear_miss": round(sum(_num(entry.get("dear_miss")) for entry in entries) / len(entries), 3),
                })
            return result

        chart_first = (today - datetime.timedelta(days=PRICE_CHART_DAYS)).isoformat()
        corrections = self.price_forecast_corrections()
        price_last_30 = price_errors(today - datetime.timedelta(days=PRICE_CORRECTION_DAYS))
        for entry in price_last_30:
            entry["correction"] = corrections.get(entry["lead"])

        def corrected(key: str, row: dict) -> float | None:
            """The newest forecast of a coming hour as the plan uses it (with the correction)."""
            lead = (datetime.date.fromisoformat(key[:10]) - today).days
            if not corrections or _is_num(row.get("real")) or not _is_num(row.get(str(lead))):
                return None
            return round(row[str(lead)] + corrections.get(lead, 0.0), 3)

        price_forecast = {
            "rank_hours": PRICE_RANK_HOURS,
            "correction_on": bool(self.enabled("price_forecast_correction")),
            "correction_min_days": PRICE_CORRECTION_MIN_DAYS,
            "last_7": price_errors(today - datetime.timedelta(days=7)),
            "last_30": price_last_30,
            "days": [
                {"date": date, **{f"d{lead}": round(day[str(lead)]["abs"] / day[str(lead)]["n"], 3) for lead in PRICE_CHART_LEADS if str(lead) in day}}
                for date, day in sorted(self.analysis_db["price_days"].items())[-ANALYSIS_LIST_DAYS:]
                if day
            ],
            "hours": [
                {"t": key, "real": row.get("real"), "corrected": corrected(key, row), **{f"d{lead}": row.get(str(lead)) for lead in PRICE_CHART_LEADS}}
                for key, row in sorted(self.analysis_db["price_hours"].items())
                if key[:10] >= chart_first
            ],
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
                "used": round(self.effective_round_trip_loss(), 3),
                "last_30": loss_window(last_30),
                "all": loss_window(all_days),
            },
            "self_sufficiency": {"last_7": sufficiency(last_7), "last_30": sufficiency(last_30), "all": sufficiency(all_days)},
            "forecast": forecast,
            "efficiency_curves": {
                "enabled": self.enabled("enhance_battery_efficiency"),
                "charging": [
                    {"power": round(power), "efficiency": round(efficiency * 100, 2)}
                    for power, efficiency in parse_efficiency_curve(solar_cfg.get("battery_charging_efficiency_curve"))
                ],
                "discharging": [
                    {"power": round(power), "efficiency": round(efficiency * 100, 2)}
                    for power, efficiency in parse_efficiency_curve(solar_cfg.get("battery_discharging_efficiency_curve"))
                ],
            },
            "plans": plans,
            "price_forecast": price_forecast,
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
