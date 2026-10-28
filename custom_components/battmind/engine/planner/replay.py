"""Historical replay of the live planner for the what-if simulator.

A separate engine with the scenario's configuration and settings plans every historical day
with the real BattMind planner (rules and optimizer) and the actual hours are then executed
with the plan of the selected controller. The live engine, its plan and its databases are
never touched.

The planner runs when the real prices of the next day are published (13:00) and plans until
the end of the next day; the plan is executed until the next publication. A history that
starts before 13:00 is first planned with the prices of its own day. The consumption and the
solar surplus of the planned hours are the actual values (a perfect forecast), so planning
again in between would not change the plan; no forecast or offline prices are used.
"""
from __future__ import annotations

import datetime
from typing import Callable

from ..energy.energy import POWER_VALUE_KEYS
from ..optimizer import OptimizerConfig, OptimizerHour, breakdown, simulate, step_hour
from ..state import Snapshot
from ...utils.helpers import average, days_between, reset_time_to_hour, start_of_day

# the hour the prices of the next day are published (Nord Pool)
NEXT_DAY_PRICES_HOUR = 13
# charging history kept for the cost and age of the energy in the battery
HISTORY_DAYS = 30
FALLBACK_ACTION = ("discharge_allowed", None)


class _KnownPrices:
    """The price provider of a replay: only the real prices known at the planning time."""

    def __init__(self) -> None:
        self.buy: dict = {}

    def buy_prices_available(self) -> bool:
        return bool(self.buy)

    def buy_price_entity_id(self) -> str:
        return ""

    def sell_periods_in_hour(self, timestamp: datetime.datetime) -> int:
        return 1

    def current_price_timestamp(self, timestamp: datetime.datetime, sell_prices: bool = False):
        return reset_time_to_hour(timestamp)


class ReplayInputs:
    """The inputs of a replay engine instead of the live entities, databases and forecasts."""

    def replay_setup(self) -> None:
        self.price_provider = _KnownPrices()
        self.replay_buy: dict = {}
        self.replay_sell: dict = {}
        self.replay_consumption: dict = {}

    def get_grid_prices(self, update_prices: bool = False) -> dict:
        return self.replay_buy

    def get_sell_prices(self, update_prices: bool = False) -> dict:
        return self.replay_sell

    def consumption_forecast_values(self, timestamp: datetime.datetime) -> list[float]:
        return [self.replay_consumption.get(timestamp, 0.0)]

    def local_energy_prediction(self) -> None:
        """The solar surplus prediction is set by replay_plan()."""

    def shadow_optimize(self, hours, start_level, cfg, end_value):
        if self.enabled("optimizer_control"):
            return super().shadow_optimize(hours, start_level, cfg, end_value)
        # the rules control the battery: the optimal plan isn't used, so it isn't calculated
        return simulate(hours, start_level, cfg, ["discharge_allowed"] * len(hours), end_value), [None] * len(hours)

    def _calculate_power_values(self, from_timestamp, to_timestamp) -> dict:
        # no measured hours before the plan: the consumption forecast is not corrected
        return dict.fromkeys(POWER_VALUE_KEYS, 0.0)

    def replay_plan(self, window: list[OptimizerHour], sell: dict, execute: int) -> list[tuple[str, float | None]]:
        """Plan the window like the live planner and return the action of the first execute hours."""
        now = window[0].timestamp
        self.replay_buy = {hour.timestamp: hour.buy for hour in window}
        self.price_provider.buy = self.replay_buy
        self.replay_sell = {hour.timestamp: sell[hour.timestamp] for hour in window if hour.timestamp in sell}
        self.replay_consumption = {hour.timestamp: hour.consumption * 1000.0 for hour in window}
        days = days_between(now, window[-1].timestamp) + 1
        self.config["days_to_predict"] = days
        self.solar_prediction_db = {day: {"total": [0.0] * 24, "total_sell": [0.0] * 24} for day in range(days + 1)}
        for hour in window:
            prediction = self.solar_prediction_db[days_between(now, hour.timestamp)]
            prediction["total"][hour.timestamp.hour] = hour.solar
            prediction["total_sell"][hour.timestamp.hour] = sell.get(hour.timestamp, 0.0)
        self.cheap_grid_charge_hours(force_recalculate=True)
        plans = self.shadow_plan_actions or {}
        plan = plans.get("optimal" if self.enabled("optimizer_control") else "rules") or {}
        return [plan.get(hour.timestamp, FALLBACK_ACTION) for hour in window[:execute]]

    def replay_optimizer_config(self) -> OptimizerConfig:
        return self.optimizer_config(self.max_kwh_charging, self.max_kwh_discharging, self.battery_efficiency())


def _segments(hours: list[OptimizerHour]) -> list[list[OptimizerHour]]:
    """Runs of consecutive hours (a gap in the history starts a new run)."""
    segments: list[list[OptimizerHour]] = []
    for hour in hours:
        if segments and (hour.timestamp - segments[-1][-1].timestamp).total_seconds() == 3600:
            segments[-1].append(hour)
        else:
            segments.append([hour])
    return segments


def _known_until(now: datetime.datetime) -> datetime.datetime:
    """End of the real prices known at now."""
    days = 2 if now.hour >= NEXT_DAY_PRICES_HOUR else 1
    return start_of_day(now) + datetime.timedelta(days=days)


def _next_planning(now: datetime.datetime) -> datetime.datetime:
    """The next publication of the prices of the next day."""
    publication = start_of_day(now).replace(hour=NEXT_DAY_PRICES_HOUR)
    return publication if now.hour < NEXT_DAY_PRICES_HOUR else publication + datetime.timedelta(days=1)


def replay_history(
    engine_class: type,
    config: dict,
    prefix: str,
    texts: dict,
    settings: dict,
    tz,
    hours: list[OptimizerHour],
    sell: dict,
    start_level: float,
    progress: Callable[[int], None] | None = None,
) -> dict:
    """Plan and execute the history with the configuration and settings; the economy of the period.

    hours: the actual hours (consumption and solar surplus in kWh, buy price, sell value);
    sell: the sell price per hour the planner sees (hours without a sell price can't sell);
    progress: called with the number of executed hours after every plan.
    """
    replay_class = type("ReplayEngine", (ReplayInputs, engine_class), {})
    engine = replay_class(config, prefix, texts)
    engine.replay_setup()
    level_entity = config["home"]["entity_ids"]["powerwall_battery_level_entity_id"] or f"sensor.{prefix}_battery_level"
    config["home"]["entity_ids"]["powerwall_battery_level_entity_id"] = level_entity

    def run(now: datetime.datetime, level: float, func, *args):
        snapshot = Snapshot(now=now, tz=tz, settings=settings, entity_states={level_entity: str(level)})
        result = engine.run_job(snapshot, func, *args)
        if result.error is not None:
            raise ValueError(f"The planner failed for {now}: {result.error}") from result.error
        return result.value

    cfg = run(hours[0].timestamp, start_level, engine.replay_optimizer_config)
    if not cfg.level_min <= start_level <= cfg.level_max:
        raise ValueError("Starting level must be between minimum and maximum level")
    # a kWh left in the battery saves the average buy price later (after wear and the min. profit)
    end_value = cfg.efficiency * max(average([hour.buy for hour in hours]) - cfg.wear - cfg.min_profit_per_kwh, 0.0)
    to_kwh = cfg.battery_size / 100.0
    summaries = []
    done = 0
    for index, segment in enumerate(_segments(hours)):
        segment_start = start_level if index == 0 else cfg.level_min
        level = segment_start
        actions, limits = [], []
        position = 0
        while position < len(segment):
            now = segment[position].timestamp
            known_until, next_planning = _known_until(now), _next_planning(now)
            window = [hour for hour in segment[position:] if hour.timestamp < known_until]
            execute = sum(hour.timestamp < next_planning for hour in window)
            for hour, (action, limit) in zip(window[:execute], run(now, level, engine.replay_plan, window, sell, execute)):
                details: dict = {}
                level, _, _, _ = step_hour(hour, level, action, cfg, limit, details)
                actions.append(action)
                limits.append(limit)
                charged = details["solar_in"] + details["grid"]
                if charged > 0.0:
                    cost = details["grid"] * hour.buy + details["solar_in"] * hour.sell
                    engine.charging_history_db[hour.timestamp] = {"charged": {
                        "percentage": round(charged / to_kwh, 1),
                        "kWh": round(charged, 3),
                        "kWh_from_grid": round(details["grid"], 3),
                        "kWh_from_local_energy": round(details["solar_in"], 3),
                        "cost": round(cost, 3),
                        "price": round(cost / charged, 3),
                    }}
            position += execute
            done += execute
            if progress is not None:
                progress(done)
            oldest = now - datetime.timedelta(days=HISTORY_DAYS)
            for key in [key for key in engine.charging_history_db if key < oldest]:
                del engine.charging_history_db[key]
        summaries.append(breakdown(segment, segment_start, cfg, actions, end_value, limits))
    return {"config": cfg, "summaries": summaries}
