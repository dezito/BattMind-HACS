"""Power measurements, weather/solar prediction and the hourly databases (engine part)."""
from __future__ import annotations

import datetime
import logging
from copy import deepcopy

from ...utils.helpers import average, get_specific_values

from .forecast import EnergyForecast
from .price import EnergyPrice
from .solar import EnergySolar

_LOGGER = logging.getLogger(__name__)


POWER_VALUE_KEYS = (
    "power_consumption",
    "ignored_consumption",
    "powerwall_charging_consumption",
    "powerwall_discharging_consumption",
    "solar_production",
    "power_consumption_without_ignored",
    "power_consumption_without_all_exclusion",
)


def derive_power_values(home: dict, consumption: float, ignored: float, charging: float, discharging: float, solar: float) -> dict:
    """POWER_VALUE_KEYS from the measured W (house consumption corrected for the battery like configured)."""
    if not home["power_consumption_entity_id_include_powerwall_discharging"]:
        consumption += discharging
    if home["power_consumption_entity_id_include_powerwall_charging"]:
        consumption -= charging
    without_ignored = round(consumption - ignored, 2)
    return {
        "power_consumption": consumption,
        "ignored_consumption": ignored,
        "powerwall_charging_consumption": charging,
        "powerwall_discharging_consumption": discharging,
        "solar_production": solar,
        "power_consumption_without_ignored": without_ignored,
        "power_consumption_without_all_exclusion": max(round(without_ignored - charging, 2), 0.0),
    }


class Energy(EnergyForecast, EnergySolar, EnergyPrice):
    """Power measurements, solar prediction and the databases filled every hour."""

    # ------------------------------------------------------------------ #
    # Power values (W, averaged over a window)
    # ------------------------------------------------------------------ #

    def _window(self, from_timestamp, to_timestamp, period):
        if period is not None:
            to_timestamp = self.now()
            from_timestamp = to_timestamp - datetime.timedelta(minutes=period)
        return from_timestamp, to_timestamp

    def get_powerwall_values(self, from_timestamp, to_timestamp) -> list[float]:
        """Interpolated battery power flow in W (100 points)."""
        entity_id = self.config["home"]["entity_ids"]["powerwall_watt_flow_entity_id"]
        return self._wc.short_values(entity_id, from_timestamp, to_timestamp) or [0.0]

    def _charging_watt(self, powerwall_values) -> float:
        positive_is_charging = self.config["home"]["invert_powerwall_watt_flow_entity_id"]
        values = get_specific_values(powerwall_values, positive_only=positive_is_charging, negative_only=not positive_is_charging)
        return abs(round(average(values), 0))

    def _discharging_watt(self, powerwall_values) -> float:
        positive_is_charging = self.config["home"]["invert_powerwall_watt_flow_entity_id"]
        values = get_specific_values(powerwall_values, positive_only=not positive_is_charging, negative_only=positive_is_charging)
        return abs(round(average(values), 0))

    def power_values(self, from_timestamp=None, to_timestamp=None, period=None) -> dict:
        """Average power (W) of consumption, battery and solar in a window. Memoised."""
        from_timestamp, to_timestamp = self._window(from_timestamp, to_timestamp, period)
        key = (from_timestamp, to_timestamp)
        if key not in self._power_cache:
            self._power_cache[key] = self._calculate_power_values(from_timestamp, to_timestamp)
        return self._power_cache[key]

    def _calculate_power_values(self, from_timestamp, to_timestamp) -> dict:
        home = self.config["home"]
        average_w = self._wc.average
        result = dict.fromkeys(POWER_VALUE_KEYS, 0.0)
        try:
            consumption = abs(round(float(average_w(home["entity_ids"]["power_consumption_entity_id"], from_timestamp, to_timestamp)), 2))
            ignored = abs(round(sum(
                float(average_w(entity_id, from_timestamp, to_timestamp))
                for entity_id in home["entity_ids"]["ignore_consumption_from_entity_ids"] or []
                if entity_id
            ), 2))
            powerwall_values = self.get_powerwall_values(from_timestamp, to_timestamp)
            charging = self._charging_watt(powerwall_values)
            discharging = self._discharging_watt(powerwall_values)
            solar = abs(round(float(average_w(self.config["solar"]["entity_ids"]["production_entity_id"], from_timestamp, to_timestamp)), 2))
            result.update(derive_power_values(home, consumption, ignored, charging, discharging, solar))
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to get power values from %s to %s: %s", from_timestamp, to_timestamp, err)
            self.notify(
                f"Failed to get power values from {from_timestamp} to {to_timestamp}: {err} {type(err)}",
                title=f"{self.title} error",
                notification_id=f"{self.name}_power_values",
            )
        return result

    # ------------------------------------------------------------------ #
    # Weather forecast
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Power values database: power per hour of the day (newest first)
    # ------------------------------------------------------------------ #

    def load_power_values_db(self, database) -> None:
        database = deepcopy(database) if isinstance(database, dict) else {}
        database.pop("version", None)
        for hour in range(24):
            values = database.setdefault(hour, {})
            for key in [k for k in values if k not in POWER_VALUE_KEYS]:
                del values[key]
        self.power_values_db = database

    def power_values_to_db(self, data: dict) -> None:
        # Recording only needs the consumption entity (the PyScript version also required solar).
        if not data or not self._power_consumption_available():
            return
        if not self.power_values_db:
            self.load_power_values_db({})
        keep = self.config["database"]["power_values_db_data_to_save"]
        hour_values = self.power_values_db[self.now().hour]
        for key, value in data.items():
            hour_values[key] = [[self.now(), value], *hour_values.get(key, [])][:keep]
        self.mark_dirty("power_values")

    # ------------------------------------------------------------------ #
    # Solar database: solar surplus per hour and weather score (newest first)
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Price history: price per hour and weekday (newest first) + daily max/mean/min
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Solar prediction
    # ------------------------------------------------------------------ #
