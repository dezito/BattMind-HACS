"""Event loop side of BattMind.

The coordinator only does light work on the event loop: it collects a plain
:class:`Snapshot` of Home Assistant data, sends the calculation to the BattMind worker
thread and publishes the result. All heavy work (planning, history, predictions,
markdown) runs in the worker.
"""
from __future__ import annotations

import asyncio
import bisect
import datetime
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from queue import SimpleQueue
from typing import Any, Callable

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, SUN_EVENT_SUNRISE, SUN_EVENT_SUNSET
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENTITY_PREFIX,
    DOMAIN,
    ENTITY_UNAVAILABLE_STATES,
    HISTORY_BUFFER_HOURS,
    SIGNAL_UPDATE,
    build_config,
    default_options,
    normalize_language,
)
from .engine import BattMindEngine, JobResult, Snapshot
from .formatting import format_charging_rule, translate
from .helpers import float_or_none
from .power import merge_rows
from .prices import TARIFF_STATE_KEYS
from .storage import DB_NAMES, BattMindStorage, load_legacy_databases

_LOGGER = logging.getLogger(__name__)

SOLCAST_ENDINGS = ["today", "tomorrow", "day_3", "day_4", "day_5", "day_6", "day_7"]
STATISTICS_REFRESH_SECONDS = 300
SETTINGS_DEBOUNCE_SECONDS = 5
FORECAST_TIMEOUT = 30


def _strip_forecast_suffix(entity_id: str) -> str:
    parts = entity_id.split("_")
    if parts[-1] in SOLCAST_ENDINGS:
        return "_".join(parts[:-1])
    if len(parts) >= 2 and parts[-2] == "day" and parts[-1].isdigit():
        return "_".join(parts[:-2])
    return entity_id


class BattMindCoordinator:
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

    async def async_setup(self) -> None:
        # texts for the overview, history, charging rule and notifications ("runtime" in
        # translations/<language>.json, English is the fallback for missing keys)
        language = normalize_language(self.config.get("language"), normalize_language(self.hass.config.language))
        prefix = f"component.{DOMAIN}.runtime."
        translations = await async_get_translations(self.hass, language, "runtime", [DOMAIN])
        self.texts = {key.removeprefix(prefix): value for key, value in translations.items() if key.startswith(prefix)}
        self.engine = BattMindEngine(self.config, self.prefix, self.texts)

        if self.config.get("enable_diagnostic_file_log"):
            await self._async_enable_file_log()

        ent_reg = er.async_get(self.hass)
        for entity_id in self.configured_entities:
            entry = ent_reg.async_get(entity_id)
            self._entity_platforms[entity_id] = entry.platform if entry else None

        self.set_charging_rule(f"📟{self.t('ui.init.script_starting')}")

    @callback
    def async_start(self) -> None:
        """Start once the platforms (and the restored settings) are set up."""
        if self.hass.state is CoreState.running:
            self._start_background()
        else:
            self._started_unsub = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._start_background)

    @callback
    def _start_background(self, _event: Event | None = None) -> None:
        self._started_unsub = None
        self.entry.async_create_background_task(self.hass, self._async_startup(), name=f"battmind_{self.prefix}_startup")

    async def async_unload(self) -> None:
        if self._started_unsub:
            self._started_unsub()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._settings_debounce:
            self._settings_debounce()
        if self._charge_task and not self._charge_task.done():
            self._charge_task.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
        await self._async_disable_file_log()

    async def _async_enable_file_log(self) -> None:
        log_path = self.hass.config.path(f"battmind_{self.prefix}.log")

        def _create_handler() -> RotatingFileHandler:
            handler = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s (%(threadName)s) [%(name)s] %(message)s"))
            return handler

        file_handler = await self.hass.async_add_executor_job(_create_handler)
        queue: SimpleQueue = SimpleQueue()
        self._log_handler = QueueHandler(queue)
        self._log_handler.setLevel(logging.DEBUG)
        self._log_listener = QueueListener(queue, file_handler)
        self._log_listener.start()
        package_logger = logging.getLogger(__package__)
        package_logger.addHandler(self._log_handler)
        if package_logger.getEffectiveLevel() > logging.DEBUG:
            package_logger.setLevel(logging.DEBUG)

    async def _async_disable_file_log(self) -> None:
        if self._log_handler:
            logging.getLogger(__package__).removeHandler(self._log_handler)
            self._log_handler = None
        if self._log_listener:
            listener = self._log_listener
            self._log_listener = None
            await self.hass.async_add_executor_job(listener.stop)

    # ------------------------------------------------------------------ #
    # Startup sequence
    # ------------------------------------------------------------------ #

    async def _async_startup(self) -> None:
        t = self.t
        log_lines: list[str] = []
        _LOGGER.info("BattMind %s starting, calculations run in thread battmind_%s_worker_0", self.prefix, self.prefix)
        try:
            await self._async_validate_entities()

            self.set_charging_rule(f"📟{t('ui.startup.loading_db')}")
            databases = await self.storage.async_load()
            missing = [name for name in DB_NAMES if databases.get(name) is None]
            if missing and self.legacy_prefix:
                legacy = await self.hass.async_add_executor_job(
                    load_legacy_databases, self.hass.config.config_dir, self.legacy_prefix
                )
                imported = [name for name in missing if name in legacy]
                for name in imported:
                    databases[name] = legacy[name]
                if imported:
                    log_lines.append(f"📟Importeret eksisterende databaser: {', '.join(sorted(imported))}")

            self.set_charging_rule(f"📟{t('ui.startup.loading_history')}")
            await self._async_setup_buffers()
            _LOGGER.info(
                "History buffers: %s",
                ", ".join(f"{entity_id} {len(rows)} states" for entity_id, rows in self._buffers.items()) or "none",
            )

            result = await self.async_run_job("startup", databases, include_prices=True)
            info = result.value or {}
            solar = t("ui.startup.configuration.configured") if info.get("solar_configured") else t("ui.startup.configuration.not_configured")
            powerwall = t("ui.startup.configuration.configured") if info.get("powerwall_configured") else t("ui.startup.configuration.not_configured")
            log_lines[:0] = [
                "---",
                f"### BattMind🤵‍♂️🔋⚡ ({self.prefix})",
                "",
                f"**{t('ui.startup.configuration.header')}:**",
                f"📟{t('ui.startup.configuration.solar')}: {solar}",
                f"📟{t('ui.startup.configuration.powerwall')}: {powerwall}",
                "",
                f"📟{t('ui.startup.calc_charging_plan')}",
            ]
            self.set_charging_rule(f"📟{t('ui.startup.calc_charging_plan')}")

            self._register_triggers()
            self.started = True
            await self._async_charge_if_needed(force=True)
            await self.async_run_job("charging_history", self._local_now().replace(minute=0, second=0))
            if self.last_job.get("worker_thread"):
                log_lines.append(f"📟Worker: {self.last_job['worker_thread']}")
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Error during BattMind startup")
            log_lines.append(f"⛔Error during startup: {err}")
            self.set_charging_rule(f"⛔{t('ui.init.script_stopped')}.\n{t('ui.init.check_log')}:\n{err}")
        finally:
            for line in log_lines:
                if line.strip("-# "):
                    _LOGGER.info(line.lstrip("#* ").replace("**", ""))
            persistent_notification.async_create(
                self.hass,
                "\n".join(log_lines),
                title=f"📟BattMind ({self.prefix}) started",
                notification_id=f"{self.prefix}_startup_notification",
            )

    async def _async_validate_entities(self) -> None:
        t = self.t
        self.set_charging_rule(f"📟{t('ui.validation_procedure.entity_validation')}")
        entities = self.configured_entities
        missing = [e for e in entities if self.hass.states.get(e) is None]
        if missing:
            _LOGGER.warning("Entities not found, retrying in 2 minutes: %s", ", ".join(missing))
            self.set_charging_rule(f"📟{t('ui.validation_procedure.entity_missing_try_again')}")
            await asyncio.sleep(120)
            missing = [e for e in entities if self.hass.states.get(e) is None]
            if missing:
                persistent_notification.async_create(
                    self.hass,
                    "- " + "\n- ".join(missing) + f"\n\n{t('ui.validation_procedure.check_entity_ids')}",
                    title=f"BattMind ({self.prefix}) missing entity ids",
                    notification_id=f"{self.prefix}_validation_procedure_missing_entity_ids",
                )

        def _unavailable() -> list[str]:
            return [
                e for e in entities
                if (state := self.hass.states.get(e)) is not None and state.state in ENTITY_UNAVAILABLE_STATES
            ]

        unavailable = _unavailable()
        if not missing and not unavailable:
            _LOGGER.info("All %s configured entities are present and available", len(entities))
        if unavailable:
            _LOGGER.warning("Entities unavailable, waiting up to 2 minutes: %s", ", ".join(unavailable))
            self.set_charging_rule(f"📟{t('ui.validation_procedure.entity_unavailable_try_again')}")
            for _ in range(12):
                await asyncio.sleep(10)
                unavailable = _unavailable()
                if not unavailable:
                    break
            if unavailable:
                persistent_notification.async_create(
                    self.hass,
                    "- " + "\n- ".join(unavailable) + f"\n\n{t('ui.validation_procedure.check_entity_ids')}",
                    title=f"BattMind ({self.prefix}) {t('ui.validation_procedure.unavailable_entity_ids_title')}",
                    notification_id=f"{self.prefix}_validation_procedure_unavailable_entity_ids",
                )

    # ------------------------------------------------------------------ #
    # Triggers
    # ------------------------------------------------------------------ #

    def _register_triggers(self) -> None:
        interval = int(self.config["cron_interval"])
        self._unsubs.append(async_track_time_change(self.hass, self._handle_cron, minute=f"/{interval}", second=0))
        self._unsubs.append(async_track_time_change(self.hass, self._handle_hour_end, minute=59, second=0))
        self._unsubs.append(async_track_time_change(self.hass, self._handle_price_update, minute=2, second=0))

    @callback
    def _handle_cron(self, _now: datetime.datetime) -> None:
        if self.settings.get("deactivate_script"):
            return
        self.async_request_charge()

    @callback
    def _handle_hour_end(self, _now: datetime.datetime) -> None:
        self.entry.async_create_background_task(self.hass, self._async_hour_end(), name=f"battmind_{self.prefix}_hour_end")

    @callback
    def _handle_price_update(self, _now: datetime.datetime) -> None:
        self.entry.async_create_background_task(
            self.hass, self.async_run_job("update_grid_prices", include_prices=True), name=f"battmind_{self.prefix}_prices"
        )

    async def _async_hour_end(self) -> None:
        timestamp = self._local_now().replace(minute=0, second=0, microsecond=0)
        delay = 2
        for attempt in range(1, 4):
            result = await self.async_run_job("hour_end", timestamp, include_forecast=True)
            if result.error is None:
                return
            if attempt < 3:
                _LOGGER.warning("Hour end job failed (%s/3): %s. Retrying in %ss", attempt, result.error, delay)
                await asyncio.sleep(delay)
                delay *= 2
        persistent_notification.async_create(
            self.hass,
            f"Charging history failed with\nerror: {result.error}",
            title=f"BattMind ({self.prefix}) error",
            notification_id=f"{self.prefix}_charging_history_failed",
        )

    @callback
    def async_request_charge(self, force: bool = False) -> None:
        """Request a planning run. Requests while one is running are coalesced."""
        if not self.started:
            return
        if self._charge_task and not self._charge_task.done():
            self._charge_rerun = True
            self._charge_rerun_force = self._charge_rerun_force or force
            return
        self._charge_task = self.entry.async_create_background_task(
            self.hass, self._async_charge_loop(force), name=f"battmind_{self.prefix}_charge_if_needed"
        )

    async def _async_charge_loop(self, force: bool) -> None:
        while True:
            await self._async_charge_if_needed(force)
            if not self._charge_rerun:
                return
            force = self._charge_rerun_force
            self._charge_rerun = False
            self._charge_rerun_force = False

    async def _async_charge_if_needed(self, force: bool = False) -> None:
        await self.async_run_job("charge_if_needed", force, include_forecast=True)

    # ------------------------------------------------------------------ #
    # Settings entities
    # ------------------------------------------------------------------ #

    @callback
    def async_set_setting(self, key: str, value: Any, initial: bool = False) -> None:
        old = self.settings.get(key)
        self.settings[key] = value
        if initial or old == value or not self.started:
            return
        if key == "deactivate_script":
            self.async_request_charge()
            return
        if self._settings_debounce:
            self._settings_debounce()
        self._settings_debounce = async_call_later(self.hass, SETTINGS_DEBOUNCE_SECONDS, self._settings_changed)

    @callback
    def _settings_changed(self, _now) -> None:
        self._settings_debounce = None
        self.async_request_charge()

    # ------------------------------------------------------------------ #
    # State publishing
    # ------------------------------------------------------------------ #

    @callback
    def set_charging_rule(self, text: str) -> None:
        """Progress text during startup (the engine sets the rule afterwards)."""
        self.data["current_charging_rule"] = format_charging_rule(text, self.testing)
        async_dispatcher_send(self.hass, self.signal, "current_charging_rule")

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
            _LOGGER.debug("Job %s ran in %s for %.3fs", job_name, result.worker_thread, result.duration)
            return result

    def _local_now(self) -> datetime.datetime:
        return dt_util.now().replace(tzinfo=None, microsecond=0)

    async def _async_snapshot(self, include_prices: bool = False, include_forecast: bool = False) -> Snapshot:
        await self._async_refresh_statistics()
        now = self._local_now()
        tz = dt_util.get_default_time_zone()

        entity_states = {}
        units = {}
        for entity_id in self.configured_entities:
            state = self.hass.states.get(entity_id)
            entity_states[entity_id] = state.state if state else None
            units[entity_id] = state.attributes.get("unit_of_measurement") if state else None

        self._prune_buffers()
        snapshot = Snapshot(
            now=now,
            tz=tz,
            settings=dict(self.settings),
            entity_states=entity_states,
            entity_platforms=dict(self._entity_platforms),
            units=units,
            buffers={entity_id: list(rows) for entity_id, rows in self._buffers.items()},
            statistics={entity_id: list(rows) for entity_id, rows in self._statistics.items()},
            sun_events=self._sun_events(now),
        )
        if include_prices:
            snapshot.price_data = self._price_snapshot()
        if include_forecast:
            snapshot.forecast_dict = await self._async_weather_forecasts()
            snapshot.solcast = self._solcast_snapshot()
        return snapshot

    def _price_snapshot(self) -> dict:
        entity_ids = self.config["prices"]["entity_ids"]
        attributes: dict[str, dict] = {}
        values: dict[str, float] = {}
        for key, entity_id in entity_ids.items():
            if not entity_id or entity_id in attributes:
                continue
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            attributes[entity_id] = dict(state.attributes)
            if key in TARIFF_STATE_KEYS:
                value = float_or_none(state.state)
                values[entity_id] = value if value is not None else 0.0
        return {"attributes": attributes, "values": values}

    def _solcast_snapshot(self) -> dict[str, list]:
        forecast_entity = self.config["solar"]["entity_ids"]["forecast_entity_id"]
        if not forecast_entity or self._entity_platforms.get(forecast_entity) != "solcast_solar":
            return {}
        base = _strip_forecast_suffix(forecast_entity)
        result = {}
        for ending in SOLCAST_ENDINGS:
            state = self.hass.states.get(f"{base}_{ending}")
            if state is not None:
                result[state.entity_id] = list(state.attributes.get("detailedHourly") or [])
        return result

    async def _async_weather_forecasts(self) -> dict:
        result: dict[str, list] = {"hourly": [], "daily": []}
        if not self.hass.services.has_service("weather", "get_forecasts"):
            return result
        for forecast_type, key in (("hourly", "hourly_service_entity_id"), ("daily", "daily_service_entity_id")):
            entity_id = self.config["forecast"]["entity_ids"].get(key)
            if not entity_id or self.hass.states.get(entity_id) is None:
                continue
            try:
                async with asyncio.timeout(FORECAST_TIMEOUT):
                    response = await self.hass.services.async_call(
                        "weather",
                        "get_forecasts",
                        {"entity_id": entity_id, "type": forecast_type},
                        blocking=True,
                        return_response=True,
                    )
                result[forecast_type] = list((response or {}).get(entity_id, {}).get("forecast", []))
            except (HomeAssistantError, TimeoutError) as err:
                _LOGGER.warning("Cant get %s forecast from %s: %s", forecast_type, entity_id, err)
        return result

    def _sun_events(self, now: datetime.datetime) -> dict:
        """Sunrise/sunset (naive local) for yesterday .. +5 days, cached per date."""
        dates = [(now + datetime.timedelta(days=day)).date() for day in range(-1, 6)]
        for date in dates:
            if date not in self._sun_cache:
                sunrise = get_astral_event_date(self.hass, SUN_EVENT_SUNRISE, date)
                sunset = get_astral_event_date(self.hass, SUN_EVENT_SUNSET, date)
                self._sun_cache[date] = {
                    "sunrise": dt_util.as_local(sunrise).replace(tzinfo=None) if sunrise else None,
                    "sunset": dt_util.as_local(sunset).replace(tzinfo=None) if sunset else None,
                }
        self._sun_cache = {date: self._sun_cache[date] for date in dates}
        return dict(self._sun_cache)

    # ------------------------------------------------------------------ #
    # Recorder buffers
    # ------------------------------------------------------------------ #

    async def _async_setup_buffers(self) -> None:
        entities = self.buffered_entities
        if not entities:
            return
        for entity_id in entities:
            self._buffers[entity_id] = []

        @callback
        def _state_listener(event: Event) -> None:
            new_state = event.data.get("new_state")
            if new_state is None:
                return
            rows = self._buffers.setdefault(event.data["entity_id"], [])
            row = (new_state.last_updated.timestamp(), float_or_none(new_state.state))
            if rows and row[0] <= rows[-1][0]:
                if row[0] == rows[-1][0]:
                    rows[-1] = row
                return
            rows.append(row)

        self._unsubs.append(async_track_state_change_event(self.hass, entities, _state_listener))

        if "recorder" not in self.hass.config.components:
            _LOGGER.warning("Recorder is not loaded, BattMind starts without history")
            return

        from homeassistant.components.recorder import get_instance, history

        end = dt_util.utcnow()
        start = end - datetime.timedelta(hours=HISTORY_BUFFER_HOURS)
        try:
            states = await get_instance(self.hass).async_add_executor_job(
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
            _LOGGER.error("Could not load history for BattMind: %s", err)
            states = {}

        for entity_id in entities:
            loaded = []
            for state in states.get(entity_id, []):
                try:
                    loaded.append((state.last_updated.timestamp(), float_or_none(state.state)))
                except AttributeError:
                    continue
            self._buffers[entity_id] = merge_rows(loaded, self._buffers.get(entity_id, []))
        await self._async_refresh_statistics(force=True)

    async def _async_refresh_statistics(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._statistics_refreshed < STATISTICS_REFRESH_SECONDS:
            return
        if "recorder" not in self.hass.config.components:
            return
        entities = self.statistics_entities
        if not entities:
            return
        self._statistics_refreshed = time.monotonic()

        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period

        end = dt_util.utcnow()
        hours = HISTORY_BUFFER_HOURS if force or not self._statistics else 4
        start = (end - datetime.timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
        try:
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period, self.hass, start, end, set(entities), "hour", None, {"mean"}
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not load statistics for BattMind: %s", err)
            return

        for entity_id in entities:
            rows = []
            for row in stats.get(entity_id, []):
                start_value = row.get("start")
                if isinstance(start_value, datetime.datetime):
                    start_value = start_value.timestamp()
                mean = row.get("mean")
                if start_value is None or mean is None:
                    continue
                rows.append((float(start_value), float(mean)))
            self._statistics[entity_id] = merge_rows(self._statistics.get(entity_id, []), rows)

    def _prune_buffers(self) -> None:
        """Drop rows older than HISTORY_BUFFER_HOURS (keeping the state at the cutoff)."""
        cutoff = time.time() - HISTORY_BUFFER_HOURS * 3600
        for rows in self._buffers.values():
            index = bisect.bisect_left(rows, (cutoff,))
            if index > 1:
                del rows[: index - 1]
        for rows in self._statistics.values():
            del rows[: bisect.bisect_left(rows, (cutoff - 3600,))]

    # ------------------------------------------------------------------ #
    # Services
    # ------------------------------------------------------------------ #

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
