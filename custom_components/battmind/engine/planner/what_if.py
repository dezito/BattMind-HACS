"""The what-if calculation of the planner."""
from __future__ import annotations

import datetime
import math
from copy import deepcopy
from dataclasses import replace

from ..optimizer import RULE_SWITCHES, OptimizerConfig, OptimizerHour, breakdown, optimize_per_day
from ...utils.efficiency import efficiency_at_power
from ..energy.energy import derive_power_values
from ...utils.power import unit_factor_to_w
from ...utils.helpers import average


class WhatIf:
    """The what-if calculation of the planner."""

    _RULE_SETTING_KEYS = tuple(dict.fromkeys(
        switch for switches in RULE_SWITCHES.values() for switch in switches
    ))

    def what_if(self, data: dict, overrides: dict) -> dict:
        """Compare battery assumptions against historical hourly statistics."""
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
            }
        ]
        candidates = []
        for provider in providers:
            buy_id = provider["buy_entity_id"]
            sell_id = provider.get("sell_entity_id")
            provider_required = dict(required)
            provider_required[buy_id] = series(buy_id, power=False)
            if sell_id:
                provider_required[sell_id] = series(sell_id, power=False)
            if any(not values for values in provider_required.values()):
                continue
            first = max(min(values) for values in provider_required.values())
            last = min(max(values) for values in provider_required.values())
            if first > last:
                continue
            span_hours = (last - first) / 3600.0
            hour_count = round(span_hours) + 1
            if abs(span_hours - round(span_hours)) > 1e-6:
                continue
            timestamps = [
                first + hour * 3600.0
                for hour in range(hour_count)
                if all(
                    first + hour * 3600.0 in values
                    for values in provider_required.values()
                )
            ]
            if timestamps:
                candidates.append(
                    (len(timestamps), provider, provider_required, timestamps)
                )
        if not candidates:
            raise ValueError(
                "No complete hourly data is available for the required entities and any configured price provider"
            )
        _, selected_provider, required, timestamps = max(candidates, key=lambda item: item[0])
        buy_id = selected_provider["buy_entity_id"]
        sell_id = selected_provider.get("sell_entity_id") or buy_id
        buy = series(buy_id, power=False)
        sell = series(sell_id, power=False)
        sell_price_history_available = bool(selected_provider.get("sell_entity_id"))
        sample_count = len(required) * len(timestamps)
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
                    for timestamp in timestamps
                )
                for entity_id in required
            )
        data_sources = {
            "long_term_statistics_samples": statistical_sample_count,
            "recorder_history_fallback_samples": sample_count - statistical_sample_count,
        }
        positive_is_charging = home["invert_powerwall_watt_flow_entity_id"]
        tz = self._snap.tz if self._snap else None
        hours = []
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
            hours.append(OptimizerHour(
                datetime.datetime.fromtimestamp(ts, tz).replace(tzinfo=None),
                buy[ts],
                self.sell_value(sell.get(ts, buy[ts])) if sell_price_history_available else self.sell_value(0.0),
                max(net_use - produced, 0.0),
                max(produced - net_use, 0.0),
                can_sell=(
                    sell_price_history_available
                    or self.config["solar"]["production_price"] != -1.0
                ),
            ))
        if not hours:
            raise ValueError("No matching hourly consumption and price statistics were found")

        solar_cfg = self.config["solar"]
        max_charge = abs(self.max_kwh_charging)
        max_discharge = abs(self.max_kwh_discharging)
        fallback = self._fallback_efficiency()
        base_cfg = OptimizerConfig(
            battery_size=solar_cfg["powerwall_battery_size"],
            level_min=solar_cfg["powerwall_battery_level_min"],
            level_max=solar_cfg["powerwall_battery_level_max"],
            max_charge=max_charge,
            max_discharge=max_discharge,
            efficiency=fallback,
            wear=abs(solar_cfg["powerwall_wear_cost_per_kwh"]),
            charge_efficiency_curve=self.charging_efficiency_curve(),
            discharge_efficiency_curve=self.discharging_efficiency_curve(),
            curve_fallback_efficiency=fallback,
            min_profit_per_kwh=self.get_min_profit_per_kwh(),
            min_sell_kwh=self.number("min_sell_kwh"),
            min_total_profit=self.get_min_total_profit(),
            usage_reserve=self.number("usage_reserve"),
        )
        rule_values = {key: self.enabled(key) for key in self._RULE_SETTING_KEYS}
        scenario_rule_values = {
            key: bool(overrides.get(key, value))
            for key, value in rule_values.items()
        }
        cfg = base_cfg
        changes = {}
        mapping = {
            "battery_size": "battery_size",
            "minimum_level": "level_min",
            "maximum_level": "level_max",
            "wear_cost_per_kwh": "wear",
            "usage_reserve": "usage_reserve",
            "min_profit_per_kwh": "min_profit_per_kwh",
            "min_total_profit": "min_total_profit",
            "min_sell_kwh": "min_sell_kwh",
        }
        for input_key, config_key in mapping.items():
            if input_key in overrides:
                changes[config_key] = float(overrides[input_key])
        if "max_charging_power" in overrides:
            changes["max_charge"] = float(overrides["max_charging_power"]) / 1000.0
        if "max_discharging_power" in overrides:
            changes["max_discharge"] = float(overrides["max_discharging_power"]) / 1000.0
        if "round_trip_loss" in overrides:
            loss = float(overrides["round_trip_loss"]) / 100.0
            efficiency = math.sqrt(1.0 - loss)
            changes.update(
                efficiency=efficiency,
                curve_fallback_efficiency=efficiency,
                charge_efficiency_curve=(),
                discharge_efficiency_curve=(),
            )
        cfg = replace(cfg, **changes)

        def effective_efficiency(calc_cfg: OptimizerConfig) -> OptimizerConfig:
            curve_fallback = calc_cfg.curve_fallback_efficiency or calc_cfg.efficiency
            charge_efficiency = efficiency_at_power(
                calc_cfg.charge_efficiency_curve, calc_cfg.max_charge * 1000.0, curve_fallback
            )
            discharge_efficiency = efficiency_at_power(
                calc_cfg.discharge_efficiency_curve, calc_cfg.max_discharge * 1000.0, curve_fallback
            )
            return replace(calc_cfg, efficiency=math.sqrt(charge_efficiency * discharge_efficiency))

        base_cfg = effective_efficiency(base_cfg)
        cfg = effective_efficiency(cfg)

        requested_start = overrides.get("starting_level")
        start_level = float(requested_start if requested_start is not None else cfg.level_min)
        current_start_level = float(requested_start if requested_start is not None else base_cfg.level_min)
        if cfg.level_min >= cfg.level_max:
            raise ValueError("Minimum level must be lower than maximum level")
        if not cfg.level_min <= start_level <= cfg.level_max:
            raise ValueError("Starting level must be between minimum and maximum level")
        if not base_cfg.level_min <= current_start_level <= base_cfg.level_max:
            raise ValueError("Starting level must also fit the configured battery limits")

        def calculate(calc_cfg: OptimizerConfig, initial_level: float, rules: dict[str, bool]) -> dict:
            def enabled(key: str) -> bool:
                return rules.get(key, self.enabled(key))

            end_value = calc_cfg.efficiency * max(
                average([hour.buy for hour in hours]) - calc_cfg.wear - calc_cfg.min_profit_per_kwh, 0.0
            )
            segments = []
            for hour in hours:
                if (
                    not segments
                    or (hour.timestamp - segments[-1][-1].timestamp).total_seconds() == 3600
                ):
                    if not segments:
                        segments.append([])
                    segments[-1].append(hour)
                else:
                    segments.append([hour])

            summaries = []
            for index, segment in enumerate(segments):
                segment_start_level = initial_level if index == 0 else calc_cfg.level_min
                result, _ = optimize_per_day(
                    deepcopy(segment), segment_start_level, calc_cfg, end_value, enabled
                )
                limits = [
                    grid if action == "grid_charging" else sold if action == "force_discharge" else None
                    for action, grid, sold in zip(result.actions, result.grid_kwh, result.sold_kwh)
                ]
                summaries.append(
                    breakdown(segment, segment_start_level, calc_cfg, result.actions, end_value, limits)
                )

            def total(column: str, key: str) -> float:
                return round(sum(summary[column][key] for summary in summaries), 2)

            without = {key: total("without", key) for key in summaries[0]["without"]}
            with_battery = {key: total("with", key) for key in summaries[0]["with"]}
            cycled = sum(summary["delivered_house_kwh"] + summary["with"]["sold_kwh"] for summary in summaries)
            profit = round(without["bill"] - with_battery["bill"], 2)
            return {
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
            }

        current = calculate(base_cfg, current_start_level, rule_values)
        summary = calculate(cfg, start_level, scenario_rule_values)
        return {
            "price_provider": selected_provider.get("name") or None,
            "period": {
                "start": hours[0].timestamp.isoformat(),
                "end": hours[-1].timestamp.isoformat(),
                "hours": len(hours),
                "days": round(len(hours) / 24.0, 1),
            },
            "assumptions": {
                "battery_size": cfg.battery_size,
                "minimum_level": cfg.level_min,
                "maximum_level": cfg.level_max,
                "starting_level": start_level,
                "max_charging_power": cfg.max_charge * 1000.0,
                "max_discharging_power": cfg.max_discharge * 1000.0,
                "round_trip_loss": round((1.0 - cfg.efficiency**2) * 100.0, 2),
                "wear_cost_per_kwh": cfg.wear,
                "usage_reserve": cfg.usage_reserve,
                "min_profit_per_kwh": cfg.min_profit_per_kwh,
                "min_total_profit": cfg.min_total_profit,
                "min_sell_kwh": cfg.min_sell_kwh,
                "sell_price_history_available": sell_price_history_available,
                "rules": scenario_rule_values,
            },
            "current_assumptions": {
                "battery_size": base_cfg.battery_size,
                "minimum_level": base_cfg.level_min,
                "maximum_level": base_cfg.level_max,
                "starting_level": current_start_level,
                "max_charging_power": base_cfg.max_charge * 1000.0,
                "max_discharging_power": base_cfg.max_discharge * 1000.0,
                "round_trip_loss": round((1.0 - base_cfg.efficiency**2) * 100.0, 2),
                "wear_cost_per_kwh": base_cfg.wear,
                "usage_reserve": base_cfg.usage_reserve,
                "min_profit_per_kwh": base_cfg.min_profit_per_kwh,
                "min_total_profit": base_cfg.min_total_profit,
                "min_sell_kwh": base_cfg.min_sell_kwh,
                "rules": rule_values,
                "control_mode": "optimizer" if self.enabled("optimizer_control") else "rules",
            },
            "data_sources": data_sources,
            "current": current,
            "scenario": summary,
            "difference": {
                "profit": round(summary["profit"] - current["profit"], 2),
                "bill": round(summary["with"]["bill"] - current["with"]["bill"], 2),
            },
        }
