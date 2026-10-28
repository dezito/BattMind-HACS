"""Setup, startup and unload of the coordinator."""
from __future__ import annotations

import asyncio
import logging
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from queue import SimpleQueue

from homeassistant.components import persistent_notification
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from ..const import ENTITY_UNAVAILABLE_STATES, normalize_language
from ..engine import BattMindEngine
from ..utils.formatting import load_texts
from ..management.backup import async_load_backup, async_restore_databases, pending_restores
from ..management.storage import DB_NAMES, load_legacy_databases

_LOGGER = logging.getLogger(__name__)


class CoordinatorLifecycle:
    """Setup, startup, restore of a backup, file log and unload."""

    async def async_setup(self) -> None:
        # texts for the overview, history, charging rule and notifications (texts/<language>.json,
        # English is the fallback for missing keys) with the currency of Home Assistant
        language = normalize_language(self.config.get("language"), normalize_language(self.hass.config.language))
        self.texts = await self.hass.async_add_executor_job(load_texts, language, self.hass.config.currency)
        self.engine = BattMindEngine(self.config, self.prefix, self.texts)

        if self.config.get("enable_diagnostic_file_log"):
            await self._async_enable_file_log()

        ent_reg = er.async_get(self.hass)
        for entity_id in self.configured_entities:
            entry = ent_reg.async_get(entity_id)
            self._entity_platforms[entity_id] = entry.platform if entry else None

        await self._async_apply_restore()
        self.set_charging_rule(f"📟{self.t('ui.init.script_starting')}")

    async def _async_apply_restore(self) -> None:
        """Databases and settings of a backup imported or chosen in the config flow (management/backup.py)."""
        name = pending_restores(self.hass).pop(self.entry.entry_id, None)
        if name is None and (name := self.entry.data.get("restore_backup")):
            # only once, the restored databases are saved from now on
            data = {key: value for key, value in self.entry.data.items() if key != "restore_backup"}
            self.hass.config_entries.async_update_entry(self.entry, data=data)
        if not name:
            return
        try:
            backup = await async_load_backup(self.hass, name)
        except HomeAssistantError as err:
            _LOGGER.error("Could not restore the BattMind backup: %s", err)
            return
        restored = await async_restore_databases(self.storage, backup)
        self.restore_settings = dict(backup.get("settings") or {})
        _LOGGER.info(
            "Restored backup %s: databases %s, %s settings", name, ", ".join(restored) or "none", len(self.restore_settings)
        )
        persistent_notification.async_create(
            self.hass,
            self.t("ui.backup.imported", file=name, databases=", ".join(restored) or "-"),
            title=f"📟BattMind ({self.prefix})",
            notification_id=f"{self.prefix}_backup",
        )

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
        package_logger = logging.getLogger(__package__.rpartition(".")[0])
        package_logger.addHandler(self._log_handler)
        if package_logger.getEffectiveLevel() > logging.DEBUG:
            package_logger.setLevel(logging.DEBUG)

    async def _async_disable_file_log(self) -> None:
        if self._log_handler:
            logging.getLogger(__package__.rpartition(".")[0]).removeHandler(self._log_handler)
            self._log_handler = None
        if self._log_listener:
            listener = self._log_listener
            self._log_listener = None
            await self.hass.async_add_executor_job(listener.stop)

    async def _async_startup(self) -> None:
        # Let every integration's homeassistant_started listener get its turn
        # before the first calculation reads their entity states.
        await asyncio.sleep(0)
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
            if not self.engine.analysis_backfilled():
                self.entry.async_create_background_task(
                    self.hass, self._async_analysis_backfill(), name=f"battmind_{self.prefix}_analysis_backfill"
                )
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
