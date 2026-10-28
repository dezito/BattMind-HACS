"""The snapshot of Home Assistant data, buffers and statistics."""
from __future__ import annotations

import asyncio
import bisect
import datetime
import logging
import math
import time
from functools import partial

from homeassistant.const import SUN_EVENT_SUNRISE, SUN_EVENT_SUNSET
from homeassistant.core import Event, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from ..const import HISTORY_BUFFER_HOURS
from ..engine import Snapshot
from ..engine.analysis.helpers import ANALYSIS_DAYS
from ..utils.helpers import float_or_none
from ..utils.power import merge_rows
from ..prices import TARIFF_STATE_KEYS
from .constants import FORECAST_TIMEOUT, STATISTICS_REFRESH_SECONDS

_LOGGER = logging.getLogger(__name__)


SOLCAST_ENDINGS = ["today", "tomorrow", "day_3", "day_4", "day_5", "day_6", "day_7"]


def _strip_forecast_suffix(entity_id: str) -> str:
    parts = entity_id.split("_")
    if parts[-1] in SOLCAST_ENDINGS:
        return "_".join(parts[:-1])
    if len(parts) >= 2 and parts[-2] == "day" and parts[-1].isdigit():
        return "_".join(parts[:-2])
    return entity_id


class CoordinatorData:
    """Collects the snapshot for the worker and keeps the history buffers."""

    @staticmethod
    def _recorder_tariff_history(
        states: dict[str, list[State]],
    ) -> dict[str, list[tuple[float, dict]]]:
        """Keep timestamped tariff attributes so simulations use the tariff in effect then."""
        result = {}
        for entity_id, entity_states in states.items():
            rows = []
            for state in entity_states:
                tariffs = (state.attributes or {}).get("tariffs")
                if isinstance(tariffs, dict):
                    rows.append((state.last_updated.timestamp(), tariffs))
            result[entity_id] = sorted(rows, key=lambda row: row[0])
        return result

    @staticmethod
    def _recorder_hourly_means(
        states: dict[str, list[State]], start: datetime.datetime, end: datetime.datetime
    ) -> dict[str, list[tuple[float, float]]]:
        """Calculate time-weighted hourly means from Recorder states with full-hour coverage."""
        start_ts = start.timestamp()
        end_ts = end.timestamp()
        result = {}
        for entity_id, entity_states in states.items():
            by_time = {}
            for state in entity_states:
                try:
                    by_time[state.last_updated.timestamp()] = float_or_none(state.state)
                except AttributeError:
                    continue
            events = sorted(by_time.items())
            times = [timestamp for timestamp, _ in events]
            values = [value for _, value in events]
            rows = []
            hour_start = start_ts
            while hour_start + 3600.0 <= end_ts:
                index = bisect.bisect_right(times, hour_start) - 1
                if index >= 0:
                    cursor = hour_start
                    weighted_value = 0.0
                    valid = True
                    while cursor < hour_start + 3600.0:
                        if values[index] is None or not math.isfinite(values[index]):
                            valid = False
                            break
                        next_timestamp = times[index + 1] if index + 1 < len(times) else hour_start + 3600.0
                        segment_end = min(next_timestamp, hour_start + 3600.0)
                        if segment_end <= cursor:
                            index += 1
                            continue
                        weighted_value += values[index] * (segment_end - cursor)
                        cursor = segment_end
                        if cursor < hour_start + 3600.0:
                            index += 1
                            if index >= len(values):
                                valid = False
                                break
                    if valid and cursor == hour_start + 3600.0:
                        rows.append((hour_start, weighted_value / 3600.0))
                hour_start += 3600.0
            result[entity_id] = rows
        return result

    @staticmethod
    def _combine_what_if_statistics(
        entities: set[str],
        stats: dict[str, list[dict]],
        recorder_hourly: dict[str, list[tuple[float, float]]],
    ) -> tuple[dict[str, list[tuple[float, float]]], dict[str, list[float]]]:
        """Prefer long-term statistics and fill only missing hours from Recorder states."""
        combined_stats = {}
        statistical_timestamps = {}
        for entity_id in entities:
            statistical_rows = []
            for row in stats.get(entity_id, []):
                value = row.get("mean") if row.get("mean") is not None else row.get("state")
                if isinstance(value, (int, float)) and math.isfinite(value):
                    timestamp = (
                        row["start"].timestamp()
                        if isinstance(row["start"], datetime.datetime)
                        else float(row["start"])
                    )
                    statistical_rows.append((timestamp, value))
            combined = {timestamp: value for timestamp, value in statistical_rows}
            statistical_timestamps[entity_id] = list(combined)
            fallback_rows = [
                (timestamp, value)
                for timestamp, value in recorder_hourly.get(entity_id, [])
                if timestamp not in combined
            ]
            combined.update(fallback_rows)
            combined_stats[entity_id] = sorted(combined.items())
        return combined_stats, statistical_timestamps

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

    async def _async_analysis_backfill(self) -> None:
        """The first time: the energy per hour of the last year from the long-term statistics (engine/analysis)."""
        if "recorder" not in self.hass.config.components:
            return
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period

        prices = self.config["prices"]["entity_ids"]
        price_id = prices["energidataservice_buy_entity_id"] or prices["stromligning_buy_current_price"]
        home = self.config["home"]["entity_ids"]
        entities = {e for e in (*self.buffered_entities, price_id) if e}
        end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        try:
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period, self.hass, end - datetime.timedelta(days=ANALYSIS_DAYS), end, entities, "hour", None, {"mean", "state"}
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Analysis: could not read the long-term statistics: %s", err)
            return
        data = {
            "price_entity_id": price_id,
            "stats": {
                # power and battery level: the mean of the hour; prices (state_class total, e.g.
                # Energi Data Service and Strømligning) only have the state at the end of the hour
                entity_id: [
                    (
                        row["start"].timestamp() if isinstance(row["start"], datetime.datetime) else float(row["start"]),
                        row["mean"] if row.get("mean") is not None else row["state"],
                    )
                    for row in rows if row.get("mean") is not None or row.get("state") is not None
                ]
                for entity_id, rows in stats.items()
            },
        }
        if not data["stats"].get(home["power_consumption_entity_id"]):
            _LOGGER.info("Analysis: no long-term statistics of %s, the analysis starts from now", home["power_consumption_entity_id"])
        await self.async_run_job("analysis_update", data)
