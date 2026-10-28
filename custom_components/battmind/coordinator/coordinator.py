"""Event loop side of BattMind.

The coordinator only does light work on the event loop: it collects a plain
:class:`Snapshot` of Home Assistant data, sends the calculation to the BattMind worker
thread and publishes the result. All heavy work (planning, history, predictions,
markdown) runs in the worker.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from logging.handlers import QueueHandler, QueueListener
from typing import Any, Callable

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from ..const import CONF_ENTITY_PREFIX, SIGNAL_UPDATE, build_config, default_options
from ..engine import BattMindEngine, JobResult
from ..utils.formatting import translate
from ..management.debug_export import DebugRecorder
from ..management.storage import BattMindStorage
from .data import CoordinatorData
from .events import CoordinatorEvents
from .lifecycle import CoordinatorLifecycle

_LOGGER = logging.getLogger(__name__)


class BattMindCoordinator(CoordinatorLifecycle, CoordinatorEvents, CoordinatorData):
    """Owns the engine, the worker thread and all scheduling for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.prefix: str = entry.data[CONF_ENTITY_PREFIX]
        self.config = build_config({**default_options(), **entry.options})
        self.testing = bool(self.config.get("testing_mode"))
        self.texts: dict[str, str] = {}  # runtime texts, loaded in async_setup
        self.engine: BattMindEngine | None = None
        self.storage = BattMindStorage(hass, entry.entry_id)

        self.data: dict[str, Any] = {}
        self.attributes: dict[str, dict[str, Any]] = {}
        self.settings: dict[str, Any] = {}
        # platform.key -> state from an imported backup, used by the entities instead of their last state
        self.restore_settings: dict[str, str] = {}
        self.solar_kwh_total: float | None = None
        self.started = False

        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"battmind_{self.prefix}_worker")
        self._job_lock = asyncio.Lock()
        self._unsubs: list[Callable[[], None]] = []
        self._started_unsub: Callable[[], None] | None = None
        self._charge_task: asyncio.Task | None = None
        self._charge_rerun = False
        self._charge_rerun_force = False
        self._settings_debounce: Callable[[], None] | None = None

        self._buffers: dict[str, list[tuple[float, float | None]]] = {}
        self._statistics: dict[str, list[tuple[float, float]]] = {}
        self._statistics_refreshed = 0.0
        self._entity_platforms: dict[str, str | None] = {}
        self._sun_cache: dict[datetime.date, dict] = {}

        home = self.config["home"]["entity_ids"]
        solar = self.config["solar"]["entity_ids"]
        battery_level = home["powerwall_battery_level_entity_id"]
        # entities whose history is buffered (live state events + one recorder load at startup)
        self.buffered_entities = sorted({
            e for e in (
                home["power_consumption_entity_id"],
                home["powerwall_watt_flow_entity_id"],
                battery_level,
                solar["production_entity_id"],
                *(home["ignore_consumption_from_entity_ids"] or []),
            ) if e
        })
        # hourly mean statistics are used for the power averages, not for the battery level
        self.statistics_entities = [e for e in self.buffered_entities if e != battery_level]
        self.configured_entities = sorted({
            *self.buffered_entities,
            *self.config["prices"]["entity_ids"].values(),
            *self.config["forecast"]["entity_ids"].values(),
            solar["forecast_entity_id"],
        } - {""})

        self._log_listener: QueueListener | None = None
        self._log_handler: QueueHandler | None = None

        self.last_job: dict[str, Any] = {}
        # last jobs and snapshots for the debug export (management/debug_export.py)
        self.debug = DebugRecorder()

    # ------------------------------------------------------------------ #
    # Configured entities
    # ------------------------------------------------------------------ #

    @property
    def legacy_prefix(self) -> str | None:
        """Prefix of the PyScript installation chosen for import in the config flow (None = no import)."""
        return self.entry.data.get("legacy_prefix")

    @property
    def signal(self) -> str:
        return SIGNAL_UPDATE.format(entry_id=self.entry.entry_id)

    # ------------------------------------------------------------------ #
    # Setup / unload
    # ------------------------------------------------------------------ #

    def t(self, key: str, default: str | None = None, **fmt) -> str:
        return translate(self.texts, key, default, **fmt)

    # ------------------------------------------------------------------ #
    # Startup sequence
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Triggers
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Settings entities
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # State publishing
    # ------------------------------------------------------------------ #

    async def _async_publish(self, result: JobResult, job_name: str) -> None:
        keys = set(result.states) | set(result.attributes)
        for key in keys:
            if key in result.states:
                self.data[key] = result.states[key]
            if key in result.attributes:
                self.attributes.setdefault(key, {}).update(result.attributes[key])

        self.last_job = {
            "job": job_name,
            "worker_thread": result.worker_thread,
            "duration_seconds": round(result.duration, 3),
            "finished": dt_util.now().isoformat(timespec="seconds"),
            "error": str(result.error) if result.error else None,
        }
        self.attributes.setdefault("current_charging_rule", {}).update(
            {
                "last_job": job_name,
                "worker_thread": result.worker_thread,
                "calculation_seconds": round(result.duration, 3),
            }
        )
        keys.add("current_charging_rule")

        if result.solar_kwh_increment:
            self.solar_kwh_total = round((self.solar_kwh_total or 0.0) + result.solar_kwh_increment, 3)
            keys.add("kwh_charged_by_solar")

        for key in keys:
            async_dispatcher_send(self.hass, self.signal, key)

        for message, title, notification_id in result.notifications:
            persistent_notification.async_create(self.hass, message, title=title, notification_id=notification_id)

        for name, encoded in result.db_saves.items():
            try:
                await self.storage.async_save(name, encoded)
                _LOGGER.debug("Saved database %s", name)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Could not save BattMind database %s: %s", name, err)

        if result.request_reload:
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

    # ------------------------------------------------------------------ #
    # Worker jobs
    # ------------------------------------------------------------------ #

    async def async_run_job(self, job_name: str, *args, include_prices: bool = False, include_forecast: bool = False) -> JobResult:
        """Collect a snapshot on the event loop and run the job in the worker thread."""
        async with self._job_lock:
            snapshot = await self._async_snapshot(include_prices=include_prices, include_forecast=include_forecast)
            func = getattr(self.engine, job_name)
            result: JobResult = await self.hass.loop.run_in_executor(
                self._executor, partial(self.engine.run_job, snapshot, func, *args)
            )
            await self._async_publish(result, job_name)
            self.debug.record(job_name, args, snapshot, result, self.data.get("current_charging_rule"))
            _LOGGER.debug("Job %s ran in %s for %.3fs", job_name, result.worker_thread, result.duration)
            return result

    def _local_now(self) -> datetime.datetime:
        return dt_util.now().replace(tzinfo=None, microsecond=0)

    # ------------------------------------------------------------------ #
    # Recorder buffers
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Services
    # ------------------------------------------------------------------ #

    async def async_what_if(self, overrides: dict, history_days: int) -> JobResult:
        """Load hourly recorder statistics and run an economic historical comparison."""
        if "recorder" not in self.hass.config.components:
            raise HomeAssistantError("Recorder is required for the historical what-if comparison")
        from homeassistant.components.recorder import get_instance, history
        from homeassistant.components.recorder.statistics import statistics_during_period

        price_ids = self.config["prices"]["entity_ids"]
        buy_id = price_ids["stromligning_buy_current_price"] or price_ids["energidataservice_buy_entity_id"]
        if not buy_id:
            raise HomeAssistantError("A buy-price entity is required for the historical what-if comparison")
        sell_price_history_available = bool(
            price_ids["stromligning_sell_current_price"] or price_ids["energidataservice_sell_entity_id"]
        )
        sell_id = (
            price_ids["stromligning_sell_current_price"]
            or price_ids["energidataservice_sell_entity_id"]
            or buy_id
        )
        entities = {entity_id for entity_id in (*self.statistics_entities, buy_id, sell_id) if entity_id}
        end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        start = end - datetime.timedelta(days=history_days)
        try:
            recorder = get_instance(self.hass)
            stats = await recorder.async_add_executor_job(
                statistics_during_period,
                self.hass,
                start,
                end,
                entities,
                "hour",
                None,
                {"mean", "state"},
            )
            states = await recorder.async_add_executor_job(
                partial(
                    history.get_significant_states,
                    self.hass,
                    start,
                    end,
                    entities,
                    None,
                    include_start_time_state=True,
                    significant_changes_only=False,
                    minimal_response=False,
                    no_attributes=True,
                )
            )
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"Could not read Recorder history and long-term statistics: {err}") from err
        recorder_hourly = self._recorder_hourly_means(states, start, end)
        combined_stats, statistical_timestamps = self._combine_what_if_statistics(entities, stats, recorder_hourly)
        data = {
            "buy_entity_id": buy_id,
            "sell_entity_id": sell_id,
            "sell_price_history_available": sell_price_history_available,
            "stats": combined_stats,
            "long_term_statistics_timestamps": statistical_timestamps,
        }
        return await self.async_run_job("what_if", data, overrides)

    async def async_debug_info(self) -> str:
        extra = {
            "status": {
                "started": self.started,
                "buffered_rows": {e: len(rows) for e, rows in self._buffers.items()},
                "statistics_rows": {e: len(rows) for e, rows in self._statistics.items()},
            },
            "worker": self.last_job,
        }
        result = await self.async_run_job("debug_info", extra)
        return result.value or ""

    async def async_recalc_charging_history_today(self) -> None:
        self.set_charging_rule("📟Recalculating today's charging history")
        await self.async_run_job("recalc_charging_history_today")
