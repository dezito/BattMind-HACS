"""Triggers, charge requests and settings changes."""
from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_change

from ..utils.formatting import format_charging_rule
from .constants import SETTINGS_DEBOUNCE_SECONDS

_LOGGER = logging.getLogger(__name__)


class CoordinatorEvents:
    """Cron, hour end and price triggers, charge requests and settings changes."""

    def _register_triggers(self) -> None:
        interval = int(self.config["cron_interval"])
        self._unsubs.append(async_track_time_change(self.hass, self._handle_cron, minute=f"/{interval}", second=0))
        self._unsubs.append(async_track_time_change(self.hass, self._handle_hour_end, minute=59, second=0))
        self._unsubs.append(async_track_time_change(self.hass, self._handle_price_update, minute=2, second=0))

    @callback
    def _handle_cron(self, _now: datetime.datetime) -> None:
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

    @callback
    def async_set_setting(self, key: str, value: Any, initial: bool = False) -> None:
        old = self.settings.get(key)
        self.settings[key] = value
        if initial or old == value or not self.started:
            return
        if self._settings_debounce:
            self._settings_debounce()
        self._settings_debounce = async_call_later(self.hass, SETTINGS_DEBOUNCE_SECONDS, self._settings_changed)

    @callback
    def _settings_changed(self, _now) -> None:
        self._settings_debounce = None
        self.async_request_charge()

    @callback
    def set_charging_rule(self, text: str) -> None:
        """Progress text during startup (the engine sets the rule afterwards)."""
        self.data["current_charging_rule"] = format_charging_rule(text, self.testing)
        async_dispatcher_send(self.hass, self.signal, "current_charging_rule")
