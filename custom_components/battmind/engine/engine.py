"""BattMind calculation engine.

The engine owns all runtime state (databases, charging plan, price cache, ...) and runs
in the BattMind worker thread. Each call goes through :meth:`BattMindEngine.run_job`
with a :class:`Snapshot` of Home Assistant data collected on the event loop. Nothing in
the engine touches Home Assistant; outputs (states, attributes, notifications, database
saves) are returned in a :class:`JobResult` and published by the coordinator.

The algorithm is split in parts:

* :mod:`planner.planner` - the charging plan and the overview markdown
* :mod:`history.history` - charging history and the cost of the energy in the battery
* :mod:`energy`  - power measurements, solar prediction and the hourly databases
* :mod:`analysis.analysis` - economy, energy and planning quality per month and day
"""
from __future__ import annotations

import datetime
import logging
import threading
import time
from typing import Any, Callable

from ..utils.codec import encode
from ..const import ENTITY_UNAVAILABLE_STATES, NUMBER_SETTINGS, SWITCH_SETTINGS
from .analysis.analysis import Analysis
from .energy.energy import Energy
from .history.history import History
from .planner.planner import Planner
from ..utils.formatting import format_charging_rule, get_list_values, translate
from ..utils.helpers import (
    average,
    calculate_ema,
    calculate_trend,
    float_or_none,
    minutes_between,
    reverse_list,
    start_of_day,
)
from ..utils.power import WindowCalculator
from ..prices import create_price_provider
from .state import Snapshot, JobResult
from .battery import Battery
from .pricing import Pricing
from .planning import Planning
from .diagnostics import Diagnostics

_LOGGER = logging.getLogger(__name__)

# Power values of windows ending before now - this are cached between jobs
POWER_CACHE_SETTLED = datetime.timedelta(hours=2)
POWER_CACHE_MAX_AGE = datetime.timedelta(hours=52)


class BattMindEngine(Planner, History, Energy, Analysis, Battery, Pricing, Planning, Diagnostics):
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
        self.analysis_db: dict = {"hours": {}, "days": {}, "plans": {}, "rule_days": {}, "price_hours": {}, "price_days": {}}

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

    # ------------------------------------------------------------------ #
    # Prices
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Skip unchanged plans
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Overview helpers
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Current action
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Debug info
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Power value cache
    # ------------------------------------------------------------------ #

    def _prune_power_cache(self, now: datetime.datetime) -> None:
        settled = now - POWER_CACHE_SETTLED
        oldest = now - POWER_CACHE_MAX_AGE
        self._power_cache = {key: value for key, value in self._power_cache.items() if oldest <= key[1] <= settled}
