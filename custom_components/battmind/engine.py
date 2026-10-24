"""BattMind calculation engine.

The engine owns all runtime state (databases, charging plan, price cache, ...) and runs
in the BattMind worker thread. Each call goes through :meth:`BattMindEngine.run_job`
with a :class:`Snapshot` of Home Assistant data collected on the event loop. Nothing in
the engine touches Home Assistant; outputs (states, attributes, notifications, database
saves) are returned in a :class:`JobResult` and published by the coordinator.

The algorithm is split in mixins:

* :mod:`engine_planner` - the charging plan and the overview markdown
* :mod:`engine_history` - charging history and the cost of the energy in the battery
* :mod:`engine_energy`  - power measurements, solar prediction and the hourly databases
* :mod:`engine_analysis` - economy, energy and planning quality per month and day
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from pprint import pformat
from typing import Any, Callable

from .codec import encode
from .const import (
    CHARGING_TYPES,
    ENTITY_UNAVAILABLE_STATES,
    NUMBER_SETTINGS,
    PRICE_ADDER_DAY_BETWEEN_DIVIDER,
    SWITCH_SETTINGS,
)
from .engine_analysis import AnalysisMixin
from .engine_energy import EnergyMixin
from .engine_history import HistoryMixin
from .engine_planner import PlannerMixin
from .formatting import active_charging_rules, emoji_parse, emoji_rules, format_charging_rule, get_list_values, translate
from .helpers import (
    average,
    calculate_ema,
    calculate_trend,
    day_of_week,
    days_between,
    float_or_none,
    limit_dict_size,
    minutes_between,
    reset_time_to_hour,
    reverse_list,
    start_of_day,
)
from .power import WindowCalculator
from .prices import create_price_provider, price_at

_LOGGER = logging.getLogger(__name__)

# Power values of windows ending before now - this are cached between jobs
POWER_CACHE_SETTLED = datetime.timedelta(hours=2)
POWER_CACHE_MAX_AGE = datetime.timedelta(hours=52)


@dataclass
class Snapshot:
    """Plain data collected on the event loop for one worker job."""

    now: datetime.datetime
    tz: Any
    settings: dict[str, Any] = field(default_factory=dict)
    entity_states: dict[str, Any] = field(default_factory=dict)
    entity_platforms: dict[str, str | None] = field(default_factory=dict)
    units: dict[str, str | None] = field(default_factory=dict)
    buffers: dict[str, list] = field(default_factory=dict)
    statistics: dict[str, list] = field(default_factory=dict)
    price_data: dict | None = None
    forecast_dict: dict = field(default_factory=lambda: {"hourly": [], "daily": []})
    solcast: dict[str, list] = field(default_factory=dict)
    sun_events: dict = field(default_factory=dict)


@dataclass
class JobResult:
    """Everything a worker job wants the event loop to publish."""

    states: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, dict[str, Any]] = field(default_factory=dict)
    notifications: list[tuple[str, str, str]] = field(default_factory=list)
    db_saves: dict[str, Any] = field(default_factory=dict)
    solar_kwh_increment: float = 0.0
    value: Any = None
    error: BaseException | None = None
    request_reload: bool = False
    worker_thread: str = ""
    duration: float = 0.0


class BattMindEngine(PlannerMixin, HistoryMixin, EnergyMixin, AnalysisMixin):
    """Holds the BattMind state and implements the algorithm."""

    def __init__(self, config: dict, prefix: str, texts: dict[str, str]) -> None:
        self.config = config
        self.name = prefix
        self.texts = texts  # runtime texts, see formatting.translate
        self.testing = bool(config.get("testing_mode"))
        self.title = f"BattMind ({prefix})"

        solar = config["solar"]
        self.max_kwh_charging = solar["powerwall_charging_power_limit"] / 1000
        self.max_kwh_discharging = solar["powerwall_discharging_power"] / 1000

        try:
            self.price_provider = create_price_provider(config)
        except ValueError as err:
            _LOGGER.error("BattMind price provider not configured: %s", err)
            self.price_provider = None

        # databases (persisted)
        self.charging_history_db: dict = {}
        self.power_values_db: dict = {}
        self.kwh_avg_prices_db: dict = {}
        self.solar_available_db: dict = {}
        self.analysis_db: dict = {"hours": {}, "days": {}, "plans": {}, "rule_days": {}}

        # runtime state
        self.grid_price_cache: dict = {"using_offline_prices": False}
        self.sell_price_cache: dict = {"using_offline_prices": False}
        self.plan: dict = {}
        self.optimizer_shadow: dict | None = None
        self.charge_hours: dict = {}
        self.battery_expenses: dict = {}
        self.charging_history_total: dict = {}
        self.history_footer_bytes: int | None = None
        self.session_rules: dict = {}
        self.solar_prediction_db: dict = {}
        self.powerwall_price_details: dict = {}
        self.integration_offline_since: dict = {}
        self.overview_history: dict = {}
        self.forecast_type = "ema"
        self.last_plan_hash: str | None = None
        self.last_overview = ""
        self.last_powerwall_action = ""
        self.error_count = 0

        # per job
        self._snap: Snapshot | None = None
        self._out: JobResult | None = None
        self._wc: WindowCalculator | None = None
        self._dirty: set[str] = set()
        self._job_cache: dict[str, Any] = {}
        # (from, to) -> power_values() result, kept between jobs for settled windows
        self._power_cache: dict[tuple[datetime.datetime, datetime.datetime], dict] = {}

    # ------------------------------------------------------------------ #
    # Jobs
    # ------------------------------------------------------------------ #

    def run_job(self, snapshot: Snapshot, func: Callable, *args, **kwargs) -> JobResult:
        """Run func with snapshot as input. Called in the (single) worker thread."""
        started = time.perf_counter()
        self._snap = snapshot
        self._out = result = JobResult(worker_thread=threading.current_thread().name)
        self._wc = WindowCalculator(snapshot.buffers, snapshot.statistics, snapshot.units, snapshot.tz)
        self._dirty = set()
        self._job_cache = {}
        self._prune_power_cache(snapshot.now)
        try:
            result.value = func(*args, **kwargs)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("BattMind job %s failed", getattr(func, "__name__", func))
            result.error = err
        finally:
            for name in self._dirty:
                result.db_saves[name] = encode(self._databases()[name])
            self._snap = self._out = self._wc = None
        result.duration = time.perf_counter() - started
        return result

    def _databases(self) -> dict[str, dict]:
        return {
            "charging_history": self.charging_history_db,
            "power_values": self.power_values_db,
            "kwh_avg_prices": self.kwh_avg_prices_db,
            "solar_available": self.solar_available_db,
            "analysis": self.analysis_db,
        }

    def mark_dirty(self, name: str) -> None:
        self._dirty.add(name)

    def startup(self, databases: dict) -> dict:
        """Load the databases, set the static entities and fetch the prices."""
        self.set_charging_rule(f"📟{self.t('ui.startup.loading_db')}")
        self.emoji_description()
        self.set_default_entity_states()
        self.load_power_values_db(databases.get("power_values"))
        self.load_solar_available_db(databases.get("solar_available"))
        self.load_kwh_prices(databases.get("kwh_avg_prices"))
        self.update_grid_prices(initial_run=True)
        self.load_charging_history(databases.get("charging_history"))
        self.load_analysis_db(databases.get("analysis"))
        self.publish_analysis()
        _LOGGER.info(
            "Loaded databases: charging history %s entries, power values %s hours, price history %s days, solar %s hours",
            len(self.charging_history_db), len(self.power_values_db), len(self.kwh_avg_prices_db.get("mean", [])),
            len(self.solar_available_db),
        )
        _LOGGER.info(
            "Prices: %s buy and %s sell periods (%s)",
            len(self.grid_price_cache.get("prices", {})), len(self.sell_price_cache.get("prices", {})),
            "offline from the price history" if self.grid_price_cache.get("using_offline_prices") else "online",
        )
        # save everything once, so databases imported from the PyScript YAML files are kept
        self._dirty.update(self._databases())
        return {"solar_configured": self.is_solar_configured(), "powerwall_configured": self.is_powerwall_configured()}

    def hour_end(self, timestamp: datetime.datetime):
        """Runs at minute 59: store the hour in the databases and the charging history."""
        self.current_battery_level_expenses()
        self.solar_available_append_to_db(
            self.local_energy_available(period=60, without_all_exclusion=True, solar_only=True)
        )
        self.power_values_to_db(self.power_values(period=60))
        self.kwh_charged_by_solar()
        session = self.charging_history(timestamp=timestamp)
        self.analysis_hour_end(timestamp)
        return session

    # ------------------------------------------------------------------ #
    # Snapshot access
    # ------------------------------------------------------------------ #

    def t(self, key: str, default: str | None = None, **fmt) -> str:
        return translate(self.texts, key, default, **fmt)

    def now(self) -> datetime.datetime:
        return self._snap.now if self._snap else datetime.datetime.now().replace(microsecond=0)

    def setting(self, key: str):
        value = self._snap.settings.get(key) if self._snap else None
        if value is not None:
            return value
        if key in SWITCH_SETTINGS:
            return SWITCH_SETTINGS[key]["default"]
        if key in NUMBER_SETTINGS:
            return NUMBER_SETTINGS[key]["default"]
        return None

    def enabled(self, key: str) -> bool:
        """State of a setting switch."""
        return bool(self.setting(key))

    def number(self, key: str) -> float:
        """Value of a setting number."""
        value = float_or_none(self.setting(key))
        return float(NUMBER_SETTINGS[key]["default"]) if value is None else value

    def entity_state(self, entity_id: str):
        return self._snap.entity_states.get(entity_id) if self._snap else None

    def get_battery_level(self) -> float:
        entity_id = self.config["home"]["entity_ids"]["powerwall_battery_level_entity_id"]
        value = float_or_none(self.entity_state(entity_id))
        if value is None and self._wc is not None:
            value = self._wc.last_numeric(entity_id)
        if value is None:
            _LOGGER.warning("Cant get the battery level from %s, using 100", entity_id)
            return 100.0
        return value

    def get_sun_events(self, dt: datetime.datetime | None = None) -> dict:
        dt = dt or self.now()
        events = (self._snap.sun_events if self._snap else {}).get(dt.date())
        if events and events.get("sunrise") and events.get("sunset"):
            return events
        base = start_of_day(dt)
        return {"sunrise": base.replace(hour=7), "sunset": base.replace(hour=19)}

    # ------------------------------------------------------------------ #
    # Output
    # ------------------------------------------------------------------ #

    def set_state(self, key: str, value) -> None:
        if self._out is not None:
            self._out.states[key] = value

    def set_attr(self, key: str, attr: str, value) -> None:
        if self._out is not None:
            self._out.attributes.setdefault(key, {})[attr] = value

    def notify(self, message: str, title: str = "", notification_id: str | None = None) -> None:
        if self._out is not None:
            self._out.notifications.append((message, title, notification_id or f"{self.name}_notification"))

    def set_charging_rule(self, text: str = "") -> None:
        self.set_state("current_charging_rule", format_charging_rule(text, self.testing))

    # ------------------------------------------------------------------ #
    # Configuration and settings
    # ------------------------------------------------------------------ #

    def is_solar_configured(self) -> bool:
        return bool(self.config["solar"]["entity_ids"]["production_entity_id"])

    def is_powerwall_configured(self) -> bool:
        return bool(self.config["home"]["entity_ids"]["powerwall_watt_flow_entity_id"])

    def is_entity_available(self, entity_id: str) -> bool:
        """Availability check that warns once an integration has been offline for 30 min."""
        integration = self._snap.entity_platforms.get(entity_id) if self._snap else None
        state = self.entity_state(entity_id)
        if state not in ENTITY_UNAVAILABLE_STATES:
            if integration is not None:
                self.integration_offline_since.pop(integration, None)
            return True

        _LOGGER.warning("Entity %s not available: state is %s", entity_id, state)
        if integration is not None:
            since = self.integration_offline_since.setdefault(integration, self.now())
            if minutes_between(since, self.now()) > 30:
                self.notify(
                    f"⛔{self.t('ui.entity_unavailable.message', entity=entity_id, timestamp=since)}",
                    title=f"{self.title} {self.t('ui.entity_unavailable.title')}",
                    notification_id=f"{self.name}_is_entity_available_{entity_id}",
                )
        return False

    def get_exclude_sell_hours(self) -> list[int]:
        value = str(self.setting("exclude_sell_hours") or "")
        return [int(hour) for hour in value.replace(" ", "").split(",") if hour.isdigit()]

    def get_min_profit_per_kwh(self) -> float:
        """Used by every charge/discharge rule, not only by only_discharge_on_profit."""
        return self.number("min_profit_per_kwh")

    def get_min_total_profit(self) -> float:
        return self.number("min_total_profit")

    def get_forecast_type(self) -> str:
        value = str(self.setting("forecast_type") or "ema").split(" ")[0].lower()
        self.forecast_type = value if value in ("average", "ema", "trend") else "ema"
        return self.forecast_type

    def get_forecast_value(self, data: list | None = None) -> float:
        """Forecast from a history list (newest first) using the selected forecast type."""
        if not data or not isinstance(data, list):
            return 0.0
        values = get_list_values(data)
        if self.forecast_type == "average":
            return round(average(values), 2)
        if self.forecast_type == "trend":
            return round(calculate_trend(reverse_list(values)), 2)
        return round(calculate_ema(reverse_list(values)), 2)

    # ------------------------------------------------------------------ #
    # Battery math
    # ------------------------------------------------------------------ #

    def kwh_to_percentage(self, kwh: float) -> float:
        return kwh / self.config["solar"]["powerwall_battery_size"] * 100

    def percentage_to_kwh(self, percentage: float) -> float:
        return percentage * self.config["solar"]["powerwall_battery_size"] / 100

    def kwh_needed_for_charging(self, target_level=None, battery=None) -> float:
        target_level = self.config["solar"]["powerwall_battery_level_min"] if target_level is None else target_level
        battery = self.get_battery_level() if battery is None else battery
        return max(round(self.percentage_to_kwh(target_level - battery), 2), 0.0)

    def get_charging_loss(self) -> float:
        """Round trip loss: configured, or measured from the history after 6 full cycles."""
        if "charging_loss" in self._job_cache:
            return self._job_cache["charging_loss"]
        solar = self.config["solar"]
        loss = solar["powerwall_charge_discharge_loss"]
        charged_kwh = self.charging_history_total.get("charged_kwh", 0.0)
        if solar["use_charge_discharge_loss_from_history"] and charged_kwh > solar["powerwall_battery_size"] * 6:
            in_battery = self.percentage_to_kwh(self.get_battery_level() - solar["powerwall_battery_level_min"])
            discharged_kwh = self.charging_history_total.get("discharged_kwh", 0.0) + in_battery
            loss = 1 - discharged_kwh / charged_kwh
        self._job_cache["charging_loss"] = loss
        return loss

    def battery_efficiency(self) -> float:
        """One way efficiency: the round trip loss is split evenly over charging and discharging."""
        return math.sqrt(1.0 - min(abs(self.get_charging_loss()), 0.9))

    def calc_battery_loss_cost(self, price: float) -> float:
        """Loss part of the price of 1 kWh out of the battery: it costs price / (1 - round trip loss)."""
        loss = min(abs(self.get_charging_loss()), 0.9)
        return price * loss / (1.0 - loss)

    # ------------------------------------------------------------------ #
    # Prices
    # ------------------------------------------------------------------ #

    def update_grid_prices(self, initial_run: bool = False) -> None:
        if self.price_provider is None:
            return
        try:
            self.price_provider.update_prices(self._snap.price_data if self._snap else None, self.now())
            self.get_grid_prices(update_prices=True)
            self.get_sell_prices(update_prices=True)
            if not initial_run:
                self.append_kwh_prices()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error in update_grid_prices: %s", err)
            self.notify(
                f"Error in update_grid_prices: {err} {type(err)}",
                title=f"{self.title} error",
                notification_id=f"{self.name}_update_grid_prices_error",
            )

    def get_hour_prices(self, update_prices: bool = False, sell_prices: bool = False) -> dict:
        """Prices per period. Returned dicts are shared - do not modify them.

        Online prices are refreshed with update_prices=True (startup + minute 2). When the
        provider has no prices, the last successful prices are used for 2 hours and after
        that offline prices forecast from the price history database.
        """
        now = self.now()
        cache = self.sell_price_cache if sell_prices else self.grid_price_cache
        kind = "sell" if sell_prices else "grid"

        if "prices" in cache and not cache["using_offline_prices"] and not update_prices:
            return cache["prices"]

        provider = self.price_provider
        prices = (provider.sell_prices if sell_prices else provider.buy_prices) if provider else {}
        if prices:
            cache.update(
                prices=prices,
                last_update=now,
                using_offline_prices=False,
                sources=provider.sell_price_sources if sell_prices else provider.buy_price_sources,
            )
            cache.pop("missing_hours", None)
            return prices

        if cache.get("prices") and "last_update" in cache and minutes_between(cache["last_update"], now) <= 120:
            _LOGGER.warning("No online %s prices, using the last successful prices", kind)
            return cache["prices"]

        _LOGGER.warning("No online %s prices, using offline prices from the price history", kind)
        try:
            prices, missing_hours = self._offline_prices("history_sell" if sell_prices else "history")
        except KeyError as err:
            message = f"Can't get offline prices: {err}"
            _LOGGER.error(message)
            self.notify(message, f"{self.title} error", f"{self.name}_get_hour_prices_offline_prices_error")
            raise
        cache.update(
            prices=prices,
            last_update=now,
            using_offline_prices=True,
            sources={timestamp: "offline" for timestamp in missing_hours},
            missing_hours=missing_hours,
        )
        return prices

    def _offline_prices(self, history_key: str) -> tuple[dict, dict]:
        """Prices for the next 7 days forecast from the price history per hour and weekday."""
        if not self.kwh_avg_prices_db:
            self.load_kwh_prices(None)
        current_hour = reset_time_to_hour(self.now())
        history = self.kwh_avg_prices_db[history_key]
        prices = {}
        for hour in range(24):
            for day in range(7):
                timestamp = current_hour.replace(hour=hour) + datetime.timedelta(days=day)
                adder = days_between(current_hour, timestamp) / PRICE_ADDER_DAY_BETWEEN_DIVIDER
                prices[timestamp] = round(self.get_forecast_value(history[hour][day]) + adder, 2)
        prices = dict(sorted(prices.items()))
        return prices, prices

    def get_grid_prices(self, update_prices: bool = False) -> dict:
        return self.get_hour_prices(update_prices=update_prices)

    def get_sell_prices(self, update_prices: bool = False) -> dict:
        return self.get_hour_prices(update_prices=update_prices, sell_prices=True)

    def _current_sell_price(self) -> float:
        """Sell price now (fixed price, provider price or price history)."""
        if not self.is_solar_configured():
            return 0.0
        fixed = self.config["solar"]["production_price"]
        if fixed != -1.0:
            return fixed
        result = price_at(self.get_sell_prices(), self.now())
        if result is not None:
            return round(result[1], 3)
        try:
            return self.get_forecast_value(self.kwh_avg_prices_db["history_sell"][self.now().hour][day_of_week(self.now())])
        except KeyError:
            return 0.0

    def get_powerwall_kwh_price(self) -> float:
        """kr/kWh of the energy in the battery, estimated from what was charged the last 24 hours.

        Used when the charging history has no price yet. Cached for the job, as the planner
        asks for it for every hour it evaluates.
        """
        if "powerwall_kwh_price" in self._job_cache:
            return self._job_cache["powerwall_kwh_price"]
        solar = self.config["solar"]
        min_battery_kwh = self.percentage_to_kwh(solar["powerwall_battery_level_min"])
        kwh = max(self.percentage_to_kwh(self.get_battery_level()) - min_battery_kwh, min_battery_kwh)
        timestamp = reset_time_to_hour(self.now())
        prices = self.get_grid_prices()
        sell_prices = self.get_sell_prices()
        history = self.kwh_avg_prices_db

        charged_kwh: list[float] = []
        total_cost: list[float] = []
        kwh_prices: list[float] = []
        try:
            for hour in range(24):
                if sum(charged_kwh) > kwh:
                    break
                loop_timestamp = timestamp - datetime.timedelta(hours=hour)
                loop_end = loop_timestamp.replace(minute=59, second=59)
                days_ago = (timestamp - loop_timestamp).days
                dow = day_of_week(loop_timestamp)
                data = self.power_values(loop_end - datetime.timedelta(hours=1), loop_end)

                charged = data["powerwall_charging_consumption"] / 1000.0
                if charged <= 0.0:
                    continue
                consumption = data["power_consumption_without_all_exclusion"] / 1000.0
                solar_available = max(data["solar_production"] / 1000.0 - consumption, 0.0)
                solar_share = min(solar_available, charged) / charged
                grid_share = 1.0 - solar_share

                if loop_timestamp in prices:
                    grid_price = prices[loop_timestamp]
                else:
                    try:
                        grid_price = history["history"][hour][dow][days_ago]
                    except (KeyError, IndexError):
                        grid_price = self.get_forecast_value(history["history"][hour][dow])
                if loop_timestamp in sell_prices:
                    sell_price = sell_prices[loop_timestamp]
                else:
                    try:
                        sell_price = history["history_sell"][hour][dow][days_ago]
                    except (KeyError, IndexError):
                        sell_price = self.get_forecast_value(history["history_sell"][hour][dow])

                kwh_price = grid_price * grid_share + sell_price * solar_share
                kwh_prices.append(round(kwh_price, 3))
                charged_kwh.append(round(charged, 3))
                total_cost.append(round(charged * kwh_price, 3))
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error getting the battery kWh price: %s", err)
            price = self._current_sell_price()
            self._job_cache["powerwall_kwh_price"] = price
            return price

        if charged_kwh:
            self.powerwall_price_details = {
                "powerwall_kwh": charged_kwh,
                "powerwall_total_cost": total_cost,
                "kwh_price_list": kwh_prices,
                "kr_per_kwh": round(sum(total_cost) / sum(charged_kwh), 3) if sum(charged_kwh) > 0.0 else 0.0,
            }
        unit = sum(total_cost) / sum(charged_kwh) if sum(charged_kwh) > 0.0 else 0.0
        price = unit + self.calc_battery_loss_cost(unit) + abs(solar["powerwall_wear_cost_per_kwh"])
        self._job_cache["powerwall_kwh_price"] = price
        return price

    @staticmethod
    def find_nth_local_min(price_dict: dict, n: int = 1, include_edge_last: bool = True):
        """Timestamp of the n'th local price minimum (in time order)."""
        times = sorted(price_dict)
        values = [price_dict[t] for t in times]
        # values[i - 1] wraps around for i == 0, like the PyScript version
        minima = [times[i] for i in range(len(values) - 1) if values[i - 1] >= values[i] < values[i + 1]]
        if include_edge_last and len(values) >= 2 and values[-1] < values[-2]:
            minima.append(times[-1])
        return minima[n - 1] if n - 1 < len(minima) else None

    # ------------------------------------------------------------------ #
    # Skip unchanged plans
    # ------------------------------------------------------------------ #

    def plan_inputs_hash(self) -> str:
        """Hash of everything the plan depends on. The plan is skipped when it is unchanged."""
        cache = self.grid_price_cache
        settings = self._snap.settings if self._snap else {}
        data = (
            reset_time_to_hour(self.now()),
            self.forecast_type,
            cache.get("using_offline_prices"),
            tuple(cache.get("prices", {}).items()),
            self.get_battery_level(),
            tuple(self.battery_expenses.items()),
            tuple(sorted(settings.items())),
        )
        return hashlib.blake2b(repr(data).encode(), digest_size=16).hexdigest()

    # ------------------------------------------------------------------ #
    # Overview helpers
    # ------------------------------------------------------------------ #

    def append_overview_output(self, title: str) -> None:
        key = f"{self.now().strftime('%Y-%m-%d %H:%M')} {title}"
        if key not in self.overview_history:
            self.overview_history[key] = [self.last_overview]
            self.overview_history = limit_dict_size(self.overview_history, 10)

    def emoji_description(self) -> None:
        lines = [f"## {self.t('ui.emoji_description.header')} ##"]
        for key, entry in sorted(CHARGING_TYPES.items(), key=lambda kv: kv[1]["priority"]):
            lines.append(f"* **{entry['emoji']} {self.t(f'charging_type.{key}.description', default='No description')}**")
        self.set_state("emoji_description", self.t("ui.emoji_description.markdown_hint", name=self.name))
        self.set_attr("emoji_description", "description", "\n".join(lines))

    def set_default_entity_states(self) -> None:
        self.set_state("overview", self.t("ui.set_default_entity_states.markdown_guide", name=self.name))
        if not self.last_overview:
            self.last_overview = f"<center>\n\n**{self.t('ui.set_default_entity_states.no_overview')}**\n\n</center>"
            self.set_attr("overview", "overview", self.last_overview)

    # ------------------------------------------------------------------ #
    # Current action
    # ------------------------------------------------------------------ #

    def _plan_today(self) -> dict:
        today = self.plan.get(0, {})
        return today if today.get("start_of_day") == start_of_day(self.now()) else {}

    def _current_hour_in(self, key: str):
        """Timestamp in today's plan[key] with the current hour, else None."""
        current_hour = self.now().hour
        for timestamp in self._plan_today().get(key, ()):
            if timestamp.hour == current_hour:
                return timestamp
        return None

    def current_hour_in_charge_hours(self):
        current_hour = reset_time_to_hour(self.now())
        for timestamp in self.charge_hours:
            if isinstance(timestamp, datetime.datetime) and reset_time_to_hour(timestamp) == current_hour:
                return timestamp
        return None

    def current_period_in_force_discharge(self):
        now = self.now()
        periods = self.price_provider.sell_periods_in_hour(now) if self.price_provider else 1
        period_length = datetime.timedelta(minutes=60 / periods)
        for timestamp in self._plan_today().get("force_discharge_timestamps", {}):
            if timestamp <= now < timestamp + period_length:
                return timestamp
        return None

    def charge_if_needed(self, force_recalculate: bool = False) -> str:
        """Update the plan and decide what the battery should do right now."""
        t = self.t
        try:
            self.cheap_grid_charge_hours(force_recalculate=force_recalculate)

            if (charge_hour := self.current_hour_in_charge_hours()) is not None:
                action = "grid_charging"
                self.session_rules.update(active_charging_rules(self.charge_hours[charge_hour]))
                rule = t("ui.charge_if_needed.planned_charging", emoji=emoji_parse(self.charge_hours[charge_hour]))
            elif (sale_period := self.current_period_in_force_discharge()) is not None:
                action = "force_discharge"
                self.session_rules["force_discharge"] = True
                # the rule that sells (most profit, old energy, ...) for the analysis per rule
                self.session_rules.update(emoji_rules(self._plan_today()["force_discharge_timestamps"][sale_period].get("emoji", "")))
                rule = t("ui.charge_if_needed.force_discharge")
            elif self._current_hour_in("discharge_timestamps") is not None:
                action = "discharge_allowed"
                rule = t("ui.charge_if_needed.discharge_allowed")
            elif self._current_hour_in("force_powerwall_max_hours_old_discharge_timestamps") is not None:
                action = "discharge_allowed"
                self.session_rules["force_powerwall_max_hours_old_discharge"] = True
                rule = t("ui.charge_if_needed.force_powerwall_max_hours_old_discharge")
            elif self._current_hour_in("blocked_solar_charge_timestamps") is not None:
                action = "blocked_solar_charge"
                rule = t("ui.charge_if_needed.blocked_solar_charge")
            else:
                action = "force_solar_only_charging"
                rule = t("ui.charge_if_needed.not_charging")

            if (float_or_none(self.entity_state(self.config["solar"]["entity_ids"]["production_entity_id"])) or 0.0) > 0:
                self.session_rules["solar"] = True

            to_timestamp = self.now()
            from_timestamp = to_timestamp - datetime.timedelta(minutes=self.config["cron_interval"])
            watt_flow = int(self._wc.average(self.config["home"]["entity_ids"]["powerwall_watt_flow_entity_id"], from_timestamp, to_timestamp))
            if watt_flow != 0:
                # normalised: + = charging, - = discharging
                charging = (watt_flow if self.config["home"]["invert_powerwall_watt_flow_entity_id"] else -watt_flow) > 0
                key = "charging_watt" if charging else "discharging_watt"
                rule += f"\n{emoji_parse(self.session_rules)}{t(f'ui.charge_if_needed.{key}', watt=watt_flow)}{emoji_parse({'average': True})}"

            self.set_charging_rule(rule)
            self.set_state("powerwall_action", action)
            if action != self.last_powerwall_action:
                _LOGGER.info("Battery action: %s (%s)", action, rule.split("\n")[0])
            self.last_powerwall_action = action
            self.error_count = 0
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Error running charge_if_needed()")
            self.notify(
                f"Error running charge_if_needed() ({self.error_count + 1}/3 before reload): {err} {type(err)}",
                title=f"{self.title} error",
                notification_id=f"{self.name}_charge_if_needed_restart_count_error",
            )
            self.error_count += 1
            if self.error_count >= 3 and self._out is not None:
                self._out.request_reload = True
            self.set_charging_rule(f"{emoji_parse({'error': True})}Fejl: {err}")
        return self.last_powerwall_action

    # ------------------------------------------------------------------ #
    # Debug info
    # ------------------------------------------------------------------ #

    @classmethod
    def _plain(cls, value):
        """Datetimes to strings so pformat output is readable."""
        if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
            return value.isoformat()
        if isinstance(value, dict):
            return {cls._plain(k): cls._plain(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._plain(v) for v in value]
        return value

    def debug_info(self, extra: dict | None = None) -> str:
        extra = extra or {}
        sections = {
            "Status": (
                {
                    "testing": self.testing,
                    "solar_configured": self.is_solar_configured(),
                    "forecast_type": self.forecast_type,
                    "error_count": self.error_count,
                    "last_plan_hash": self.last_plan_hash,
                    "history_footer_bytes": self.history_footer_bytes,
                    "integration_offline_since": self.integration_offline_since,
                    **extra.get("status", {}),
                },
                {"config": self.config},
            ),
            "Worker": (extra.get("worker", {}), None),
            "Charging plan & prices": (
                None,
                {
                    "plan": self.plan,
                    "charge_hours": self.charge_hours,
                    "grid_price_cache": self.grid_price_cache,
                    "sell_price_cache": self.sell_price_cache,
                },
            ),
            "Battery cost": (None, {"battery_expenses": self.battery_expenses, "powerwall_price_details": self.powerwall_price_details}),
            "Solar prediction": (None, {"solar_prediction_db": self.solar_prediction_db}),
            "Databases": (None, {"power_values_db": self.power_values_db, "solar_available_db": self.solar_available_db}),
            "Session rules": (None, {"session_rules": self.session_rules}),
        }
        lines = ["<center>\n", f"#### Debug info runtime {self.now()} ####\n", "</center>\n"]
        for section, (table, details) in sections.items():
            lines.append(f"<center>\n\n### {section}\n</center>\n")
            if table:
                lines += ["| Variable | Value |", "|---:|:---|"]
                lines += [f"| {key}: | {self._plain(value)} |" for key, value in table.items()]
            for key, value in (details or {}).items():
                lines += ["<details>", f"<summary>{key}: Show dictionary</summary>\n", f"```\n{pformat(self._plain(value))}\n```", "</details>\n"]
            lines.append("---")
        if self.overview_history:
            lines.append("<center>\n\n### Overview History\n</center>\n")
            for title, overview in sorted(self.overview_history.items(), reverse=True):
                lines += ["<details>", f"<summary>{title}: Show snapshot</summary>\n", *overview, "</details>\n"]
        output = "\n".join(lines)
        self.notify(output, title=f"{self.title} debug info", notification_id=f"{self.name}_debug_info")
        return output

    # ------------------------------------------------------------------ #
    # Power value cache
    # ------------------------------------------------------------------ #

    def _prune_power_cache(self, now: datetime.datetime) -> None:
        settled = now - POWER_CACHE_SETTLED
        oldest = now - POWER_CACHE_MAX_AGE
        self._power_cache = {key: value for key, value in self._power_cache.items() if oldest <= key[1] <= settled}
