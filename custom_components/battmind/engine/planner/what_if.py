"""The what-if calculation of the planner."""
from __future__ import annotations

import datetime
import math
import time
from bisect import bisect_left
from copy import deepcopy

from ...const import (
    NUMBER_SETTINGS,
    SELECT_SETTINGS,
    SWITCH_SETTINGS,
    TEXT_SETTINGS,
    WHAT_IF_NUMBERS,
    WHAT_IF_SWITCHES,
    WHAT_IF_TEXTS,
)
from ..optimizer import OptimizerConfig, OptimizerHour
from ..energy.energy import derive_power_values
from ...prices.base import calculate_sell_price
from ...utils.efficiency import efficiency_curve_mapping, parse_efficiency_curve
from ...utils.power import unit_factor_to_w
from ...utils.helpers import float_or_none
from .replay import replay_history

# seconds between two progress reports of a running simulation
PROGRESS_INTERVAL = 10.0
# missing hours of the requested period that are reported (and estimated)
MIN_MISSING_HOURS = 24
# the estimate of the requested period needs simulated data for this share of its hours
MIN_ESTIMATE_COVERAGE = 0.5
# a calendar month estimates missing hours with this many simulated hours (or a quarter of a short period)
MIN_REFERENCE_HOURS = 7 * 24

# what-if input -> battery configuration (config["solar"])
_CONFIG_OVERRIDES = {
    "battery_size": "powerwall_battery_size",
    "minimum_level": "powerwall_battery_level_min",
    "maximum_level": "powerwall_battery_level_max",
    "max_charging_power": "powerwall_charging_power_limit",
    "max_discharging_power": "powerwall_discharging_power",
    "wear_cost_per_kwh": "powerwall_wear_cost_per_kwh",
}


def _nearest_value(
    values: list[tuple[float, float]], timestamp: float
) -> tuple[float, bool] | None:
    """Return the closest recorded value, preferring the earlier sample on a tie."""
    if not values:
        return None
    index = bisect_left(values, (timestamp, -math.inf))
    candidates = values[max(0, index - 1):min(len(values), index + 1)]
    nearest_timestamp, nearest_value = min(
        candidates, key=lambda item: (abs(item[0] - timestamp), item[0] > timestamp)
    )
    return nearest_value, nearest_timestamp == timestamp


def _nearest_eds_tariffs(
    history: list[tuple[float, dict]], timestamp: float, hour: int
) -> tuple[dict[str, float], bool] | None:
    """Read the hourly tariff schedule from the closest recorded EDS state."""
    if not history:
        return None
    index = bisect_left(history, (timestamp,))
    candidates = history[max(0, index - 1):min(len(history), index + 1)]
    nearest_timestamp, schedule = min(
        candidates, key=lambda row: (abs(row[0] - timestamp), row[0] > timestamp)
    )
    additional = schedule.get("additional_tariffs") or {}
    hourly = schedule.get("tariffs") or {}
    try:
        tariffs = {
            "transmissions_nettarif": float(additional["transmissions_nettarif"]),
            "systemtarif": float(additional["systemtarif"]),
            "elafgift": float(additional["elafgift"]),
            "tariffs": float(hourly[str(hour)]),
            "surcharge": 0.0,
        }
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in tariffs.values()):
        return None
    return tariffs, nearest_timestamp == timestamp


def _requested_hours_per_month(start: float, end: float, tz) -> dict[str, int]:
    """Hours of the requested period per local calendar month ("YYYY-MM")."""
    months: dict[str, int] = {}
    timestamp = start
    while timestamp < end:
        key = datetime.datetime.fromtimestamp(timestamp, tz).strftime("%Y-%m")
        months[key] = months.get(key, 0) + 1
        timestamp += 3600
    return months


def _reference_months(
    requested: dict[str, int], simulated: dict[str, int], minimum_hours: float
) -> tuple[dict[str, list[int]], list[str]] | None:
    """Calendar months (1-12) that estimate the missing hours of each requested month.

    A month uses its own simulated hours of the same calendar month (any year) when they are
    enough, else the nearest calendar months with enough data (both on a tie). Returns the
    references and the months estimated from other months, or None without any reference.
    """
    pooled: dict[int, int] = {}
    for key, hours in simulated.items():
        pooled[int(key[5:])] = pooled.get(int(key[5:]), 0) + hours
    candidates = [month for month, hours in pooled.items() if hours >= minimum_hours]
    if not candidates:
        return None
    references, from_other_months = {}, []
    for key, hours in requested.items():
        if hours - simulated.get(key, 0) <= 0:
            continue
        month = int(key[5:])
        if month in candidates:
            references[key] = [month]
            continue

        def distance(candidate: int) -> int:
            return min((candidate - month) % 12, (month - candidate) % 12)

        nearest = min(distance(candidate) for candidate in candidates)
        references[key] = sorted(candidate for candidate in candidates if distance(candidate) == nearest)
        from_other_months.append(key)
    return references, from_other_months


def _estimated_bill(
    requested: dict[str, int], simulated: dict[str, int], months: dict[str, dict], references: dict[str, list[int]]
) -> float:
    """Bill with the battery of the missing hours: the average bill per hour of the reference months."""
    pooled: dict[int, list[float]] = {}
    for key, month in months.items():
        total = pooled.setdefault(int(key[5:]), [0.0, 0])
        total[0] += month["bill"]
        total[1] += month["hours"]
    estimated = 0.0
    for key, reference in references.items():
        per_hour = sum(pooled[month][0] / pooled[month][1] for month in reference) / len(reference)
        estimated += (requested[key] - simulated.get(key, 0)) * per_hour
    return estimated


class WhatIf:
    """The what-if calculation of the planner."""

    def what_if(self, data: dict, overrides: dict, progress=None) -> dict:
        """Compare battery assumptions against historical hourly statistics.

        progress (optional) is called from the worker thread after the first plan and then at most
        every PROGRESS_INTERVAL seconds with {"percent", "step", "steps", "hours", "total_hours"}.
        """
        stats = data.get("stats") or {}
        units = self._snap.units if self._snap else {}
        home = self.config["home"]
        ids = home["entity_ids"]

        def series(entity_id: str, power: bool = True) -> dict[float, float]:
            factor = unit_factor_to_w(units.get(entity_id)) if power else 1.0
            return {
                float(ts): float(value) * factor
                for ts, value in stats.get(entity_id) or []
                if isinstance(value, (int, float)) and math.isfinite(value)
            }

        consumption_id = ids["power_consumption_entity_id"]
        flow_id = ids["powerwall_watt_flow_entity_id"]
        solar_id = self.config["solar"]["entity_ids"]["production_entity_id"]
        ignored_ids = [entity_id for entity_id in ids["ignore_consumption_from_entity_ids"] or [] if entity_id]
        consumption = series(consumption_id)
        flow = series(flow_id)
        solar = series(solar_id)
        ignored = [series(entity_id) for entity_id in ignored_ids]
        required_ids = list(dict.fromkeys(
            entity_id
            for entity_id in (
                consumption_id,
                flow_id,
                solar_id,
                *ignored_ids,
            )
            if entity_id
        ))
        required = {entity_id: series(entity_id) for entity_id in required_ids}
        missing_series = [entity_id for entity_id, values in required.items() if not values]
        if missing_series:
            raise ValueError("No hourly long-term statistics for required entities: " + ", ".join(missing_series))

        providers = data.get("price_providers") or [
            {
                "name": "",
                "buy_entity_id": data["buy_entity_id"],
                "sell_entity_id": (
                    data["sell_entity_id"]
                    if data.get("sell_price_history_available", True)
                    else None
                ),
                "sell_fee": 0.0,
            }
        ]
        # Providers are in priority order: each hour uses the first provider with price
        # history for that hour. Buy and sell prices of an hour come from the same provider.
        price_sources = []
        for provider in providers:
            buy_id = provider["buy_entity_id"]
            sell_id = provider.get("sell_entity_id")
            buy_values = series(buy_id, power=False)
            sell_values = series(sell_id, power=False) if sell_id else {}
            price_sources.append({
                "provider": provider,
                "entity_ids": [buy_id, sell_id] if sell_id else [buy_id],
                "timestamps": buy_values.keys() & sell_values.keys() if sell_id else buy_values.keys(),
                "buy": buy_values,
                "sell": sell_values,
                "sell_price_history_available": bool(sell_id),
                "tariff_series": {
                    tariff: sorted(series(entity_id, power=False).items())
                    for tariff, entity_id in provider.get("sell_tariff_entity_ids", {}).items()
                },
                "eds_sell_tariff_history": sorted(
                    provider.get("sell_tariff_history") or [],
                    key=lambda row: row[0],
                ),
                "hours": 0,
            })
        tz = self._snap.tz if self._snap else None

        def hour_tariffs(source: dict, ts: float) -> tuple[dict[str, float], bool] | None:
            """Return the sell tariffs of an hour and whether they are exact, or None if unknown."""
            if source["tariff_series"]:
                tariffs = {}
                exact = True
                for key, tariff_values in source["tariff_series"].items():
                    nearest = _nearest_value(tariff_values, ts)
                    if nearest is None:
                        return None
                    tariffs[key], key_exact = nearest
                    exact = exact and key_exact
                return tariffs, exact
            provider = source["provider"]
            # A raw Energi Data Service price (tariffs disabled) has no tariff schedule.
            if provider.get("name") == "Energi Data Service" and provider.get("sell_includes_tariffs", True):
                local_hour = datetime.datetime.fromtimestamp(ts, tz).hour
                return _nearest_eds_tariffs(source["eds_sell_tariff_history"], ts, local_hour)
            return {}, True

        tariff_estimated_hours = 0
        tariff_unavailable_hours = 0
        hour_sources = {}
        hour_tariff_values = {}
        for ts in sorted(set.intersection(*(set(values) for values in required.values()))):
            priced = False
            for source in price_sources:
                if ts not in source["timestamps"]:
                    continue
                priced = True
                if source["sell_price_history_available"]:
                    tariffs = hour_tariffs(source, ts)
                    if tariffs is None:
                        continue
                    hour_tariff_values[ts] = tariffs
                hour_sources[ts] = source
                source["hours"] += 1
                break
            else:
                # Without tariffs the sell value is unknown: skip the hour like missing data
                # instead of simulating purchases without the matching sales.
                if priced:
                    tariff_unavailable_hours += 1
        timestamps = list(hour_sources)
        if not timestamps:
            raise ValueError(
                "No complete hourly data is available for the required entities and any configured price provider"
            )
        used_sources = [source for source in price_sources if source["hours"]]
        sell_price_history_available = all(
            source["sell_price_history_available"] for source in used_sources
        )
        sample_count = sum(len(required) + len(hour_sources[ts]["entity_ids"]) for ts in timestamps)
        if "long_term_statistics_timestamps" not in data:
            statistical_sample_count = sample_count
        else:
            statistical_timestamps = {
                entity_id: set(entity_timestamps)
                for entity_id, entity_timestamps in data["long_term_statistics_timestamps"].items()
            }
            statistical_sample_count = sum(
                sum(
                    timestamp in statistical_timestamps.get(entity_id, set())
                    for entity_id in (*required, *hour_sources[timestamp]["entity_ids"])
                )
                for timestamp in timestamps
            )
        data_sources = {
            "long_term_statistics_samples": statistical_sample_count,
            "recorder_history_fallback_samples": sample_count - statistical_sample_count,
        }
        positive_is_charging = home["invert_powerwall_watt_flow_entity_id"]
        hours = []
        planner_sell = {}  # the sell price per hour the planner sees
        for ts in timestamps:
            battery = flow.get(ts, 0.0)
            charging = max(battery if positive_is_charging else -battery, 0.0)
            discharging = max(-battery if positive_is_charging else battery, 0.0)
            values = derive_power_values(
                home,
                abs(consumption[ts]),
                sum(abs(item.get(ts, 0.0)) for item in ignored),
                charging,
                discharging,
                abs(solar.get(ts, 0.0)),
            )
            row = self._analysis_row(values)
            net_use = row["use"]
            produced = row["solar"]
            source = hour_sources[ts]
            hour_provider = source["provider"]
            buy_price = source["buy"][ts]
            sell_price = source["sell"].get(ts, buy_price)
            hour_sell_history = source["sell_price_history_available"]
            if hour_sell_history:
                tariffs, tariff_exact = hour_tariff_values[ts]
                if not tariff_exact:
                    tariff_estimated_hours += 1
                sell_price = calculate_sell_price(
                    sell_price,
                    {
                        "transmissions_nettarif": tariffs.get("transmissions_nettarif", 0.0),
                        "systemtarif": tariffs.get("systemtarif", 0.0),
                        "elafgift": tariffs.get("elafgift", 0.0),
                        "tariffs": tariffs.get("tariffs", 0.0),
                        "surcharge": tariffs.get("surcharge", 0.0),
                    },
                    float(hour_provider.get("sell_fee", 0.0)),
                    # a raw sell price has no tariffs: the selling costs come from the buy sensor
                    None if tariffs else hour_provider.get("sell_costs"),
                )
                sell_value = self.sell_value(sell_price)
            else:
                sell_value = self.sell_value(0.0)
            can_sell = hour_sell_history or self.config["solar"]["production_price"] != -1.0
            timestamp = datetime.datetime.fromtimestamp(ts, tz).replace(tzinfo=None)
            if can_sell:
                planner_sell[timestamp] = sell_price if hour_sell_history else 0.0
            hours.append(OptimizerHour(
                timestamp,
                buy_price,
                sell_value,
                max(net_use - produced, 0.0),
                max(produced - net_use, 0.0),
                can_sell=can_sell,
            ))
        if not hours:
            raise ValueError("No matching hourly consumption and price statistics were found")

        current_config, current_settings = self._what_if_setup({})
        scenario_config, scenario_settings = self._what_if_setup(overrides)
        requested_start = overrides.get("starting_level")
        starts = []
        for config in (current_config, scenario_config):
            level_min = config["solar"]["powerwall_battery_level_min"]
            level_max = config["solar"]["powerwall_battery_level_max"]
            if level_min >= level_max:
                raise ValueError("Minimum level must be lower than maximum level")
            start = float(requested_start if requested_start is not None else level_min)
            if not level_min <= start <= level_max:
                raise ValueError("Starting level must be between minimum and maximum level")
            starts.append(start)
        current_start_level, start_level = starts

        unchanged = (scenario_config, scenario_settings, start_level) == (current_config, current_settings, current_start_level)
        steps = 1 if unchanged else 2
        # the first plan is reported at once: the history is loaded and the simulation runs
        last_report = [-PROGRESS_INTERVAL]

        def reporter(step: int):
            def report(done: int) -> None:
                now = time.monotonic()
                if progress is None or now - last_report[0] < PROGRESS_INTERVAL:
                    return
                last_report[0] = now
                progress({
                    "percent": int(((step - 1) * len(hours) + done) * 100 / (steps * len(hours))),
                    "step": step,
                    "steps": steps,
                    "hours": done,
                    "total_hours": len(hours),
                })
            return report

        def calculate(config: dict, settings: dict, initial_level: float, step: int) -> dict:
            replay = replay_history(
                type(self), config, f"{self.name}_what_if", self.texts, settings, tz, hours, planner_sell, initial_level,
                reporter(step),
            )
            summaries = replay["summaries"]

            def total(column: str, key: str) -> float:
                return round(sum(summary[column][key] for summary in summaries), 2)

            without = {key: total("without", key) for key in summaries[0]["without"]}
            with_battery = {key: total("with", key) for key in summaries[0]["with"]}
            cycled = sum(summary["delivered_house_kwh"] + summary["with"]["sold_kwh"] for summary in summaries)
            profit = round(without["bill"] - with_battery["bill"], 2)
            return {
                "config": replay["config"],
                "without": without,
                "with": with_battery,
                "profit": profit,
                "profit_per_kwh": round(profit / cycled, 2) if cycled > 0 else 0.0,
                "delivered_house_kwh": round(sum(item["delivered_house_kwh"] for item in summaries), 2),
                "charged_grid_kwh": round(sum(item["charged_grid_kwh"] for item in summaries), 2),
                "charged_solar_kwh": round(sum(item["charged_solar_kwh"] for item in summaries), 2),
                "end_level": summaries[-1]["end_level"],
                "start": summaries[0]["start"].isoformat(),
                "end": summaries[-1]["end"].isoformat(),
                "months": {
                    key: {"hours": month["hours"], "profit": round(month["profit"], 2), "bill": round(month["bill"], 2)}
                    for key, month in sorted(replay["months"].items())
                },
            }

        current = calculate(current_config, current_settings, current_start_level, 1)
        summary = deepcopy(current) if unchanged else calculate(scenario_config, scenario_settings, start_level, 2)
        current_cfg, cfg = current.pop("config"), summary.pop("config")
        coverage, estimate = self._what_if_coverage(
            data.get("requested_period"), tz, timestamps, hours, required, price_sources, current, summary
        )
        return {
            "price_provider": ", ".join(
                source["provider"]["name"] for source in used_sources if source["provider"].get("name")
            ) or None,
            "price_provider_hours": {
                source["provider"]["name"]: source["hours"]
                for source in used_sources
                if source["provider"].get("name")
            },
            "tariff_estimated_hours": tariff_estimated_hours,
            "tariff_unavailable_hours": tariff_unavailable_hours,
            "period": {
                "start": hours[0].timestamp.isoformat(),
                "end": hours[-1].timestamp.isoformat(),
                "hours": len(hours),
                "days": round(len(hours) / 24.0, 1),
            },
            "coverage": coverage,
            "estimate": estimate,
            "assumptions": {
                **self._what_if_assumptions(cfg, scenario_settings, start_level),
                "sell_price_history_available": sell_price_history_available,
            },
            "current_assumptions": self._what_if_assumptions(current_cfg, current_settings, current_start_level),
            "data_sources": data_sources,
            "current": current,
            "scenario": summary,
            "difference": {
                "profit": round(summary["profit"] - current["profit"], 2),
                "bill": round(summary["with"]["bill"] - current["with"]["bill"], 2),
            },
        }

    @staticmethod
    def _what_if_coverage(
        requested_period: dict | None,
        tz,
        timestamps: list[float],
        hours: list[OptimizerHour],
        required: dict[str, dict[float, float]],
        price_sources: list[dict],
        current: dict,
        scenario: dict,
    ) -> tuple[dict | None, dict | None]:
        """The simulated part of the requested period and an estimate of the whole period.

        (None, None) without a requested period or when less than MIN_MISSING_HOURS are missing.
        The estimate is None with too little data (MIN_ESTIMATE_COVERAGE, MIN_REFERENCE_HOURS).
        """
        if not requested_period:
            return None, None
        start, end = float(requested_period["start"]), float(requested_period["end"])
        requested = _requested_hours_per_month(start, end, tz)
        requested_hours = sum(requested.values())
        missing_hours = max(requested_hours - len(timestamps), 0)
        if missing_hours < MIN_MISSING_HOURS:
            return None, None
        simulated: dict[str, int] = {}
        for hour in hours:
            key = hour.timestamp.strftime("%Y-%m")
            simulated[key] = simulated.get(key, 0) + 1

        # the data starts with the entity that starts last; the prices with the provider that starts first
        firsts = {entity_id: min(values) for entity_id, values in required.items()}
        price_first = None
        for source in price_sources:
            if not source["timestamps"]:
                continue
            series = dict(zip(source["entity_ids"], (source["buy"], source["sell"])))
            entity_id = max(series, key=lambda key: min(series[key]))
            first = min(source["timestamps"])
            if price_first is None or first < price_first[1]:
                price_first = (entity_id, first)
        if price_first is not None:
            firsts[price_first[0]] = price_first[1]
        limited_by = None
        entity_id, first = max(firsts.items(), key=lambda item: item[1])
        if first - start >= MIN_MISSING_HOURS * 3600:
            limited_by = {
                "entity_id": entity_id,
                "first": datetime.datetime.fromtimestamp(first, tz).replace(tzinfo=None).isoformat(),
            }
        before = min(max(int((timestamps[0] - start) // 3600), 0), missing_hours)
        days = requested_period.get("days", round(requested_hours / 24.0, 1))
        coverage = {
            "requested_days": days,
            "requested_hours": requested_hours,
            "missing_hours": missing_hours,
            "missing_before_hours": before,
            "gap_hours": missing_hours - before,
            "limited_by": limited_by,
            "months": {
                key: {"requested_hours": month_hours, "simulated_hours": simulated.get(key, 0)}
                for key, month_hours in requested.items()
            },
        }
        if len(timestamps) < MIN_ESTIMATE_COVERAGE * requested_hours:
            return coverage, None
        found = _reference_months(requested, simulated, min(MIN_REFERENCE_HOURS, requested_hours / 4.0))
        if found is None:
            return coverage, None
        references, from_other_months = found
        columns = {}
        for name, column in (("current", current), ("scenario", scenario)):
            estimated = round(_estimated_bill(requested, simulated, column["months"], references), 2)
            columns[name] = {
                "bill": column["with"]["bill"],
                "estimated": estimated,
                "total": round(column["with"]["bill"] + estimated, 2),
            }
        estimate = {
            "days": days,
            "estimated_hours": sum(max(requested[key] - simulated.get(key, 0), 0) for key in references),
            "reference_months": references,
            "estimated_from_other_months": from_other_months,
            **columns,
            "difference": round(columns["scenario"]["total"] - columns["current"]["total"], 2),
        }
        return coverage, estimate

    def _what_if_setup(self, overrides: dict) -> tuple[dict, dict]:
        """Configuration and settings of a replay: the current ones with the what-if values."""
        config = deepcopy(self.config)
        solar = config["solar"]
        # the replay has no charging history: the measured round trip loss is used as configured
        solar["powerwall_charge_discharge_loss"] = self.get_charging_loss()
        solar["use_charge_discharge_loss_from_history"] = False
        # nor the analysis: measured efficiency curves are used as configured
        for direction in ("charging", "discharging"):
            key = f"battery_{direction}_efficiency_curve"
            if not parse_efficiency_curve(solar.get(key)):
                solar[key] = efficiency_curve_mapping(self.learned_efficiency_curve(direction))
        settings = {key: self.setting(key) for key in (*SWITCH_SETTINGS, *NUMBER_SETTINGS, *SELECT_SETTINGS, *TEXT_SETTINGS)}
        for input_key, config_key in _CONFIG_OVERRIDES.items():
            if input_key in overrides:
                solar[config_key] = float(overrides[input_key])
        if "round_trip_loss" in overrides:
            # a fixed loss instead of the configured efficiency curves
            solar["powerwall_charge_discharge_loss"] = float(overrides["round_trip_loss"]) / 100.0
            settings["enhance_battery_efficiency"] = False
        for key in (*WHAT_IF_SWITCHES, *WHAT_IF_NUMBERS, *WHAT_IF_TEXTS):
            if key in overrides and not (key == "enhance_battery_efficiency" and "round_trip_loss" in overrides):
                settings[key] = overrides[key]
        return config, settings

    @staticmethod
    def _what_if_assumptions(cfg: OptimizerConfig, settings: dict, start_level: float) -> dict:
        def number(key: str) -> float:
            value = float_or_none(settings.get(key))
            return float(NUMBER_SETTINGS[key]["default"]) if value is None else value

        def enabled(key: str) -> bool:
            value = settings.get(key)
            return bool(SWITCH_SETTINGS[key]["default"] if value is None else value)

        return {
            "battery_size": cfg.battery_size,
            "minimum_level": cfg.level_min,
            "maximum_level": cfg.level_max,
            "starting_level": start_level,
            "max_charging_power": cfg.max_charge * 1000.0,
            "max_discharging_power": cfg.max_discharge * 1000.0,
            "round_trip_loss": round((1.0 - cfg.efficiency**2) * 100.0, 2),
            "wear_cost_per_kwh": cfg.wear,
            **{key: number(key) for key in WHAT_IF_NUMBERS},
            "exclude_sell_hours": str(settings.get("exclude_sell_hours") or ""),
            "rules": {key: enabled(key) for key in WHAT_IF_SWITCHES},
            "control_mode": "optimizer" if enabled("optimizer_control") else "rules",
        }
