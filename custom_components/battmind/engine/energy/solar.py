"""Local (solar) energy and the solar database."""
from __future__ import annotations

import datetime
from copy import deepcopy

from ...const import WEATHER_CONDITION_DICT
from ...utils.helpers import get_closest_key
from .forecast import SOLAR_SCORES


class EnergySolar:
    """Local (solar) energy and the solar database."""

    def _local_energy(self, from_timestamp, to_timestamp, without_all_exclusion: bool):
        """(local energy surplus W, solar only surplus W, solar W, local energy W) in a window."""
        values = self.power_values(from_timestamp, to_timestamp)
        house = values["power_consumption_without_all_exclusion" if without_all_exclusion else "power_consumption_without_ignored"]
        solar = values["solar_production"]
        local = solar + values["powerwall_discharging_consumption"]
        surplus = round(min(max(local - house, 0.0), self.config["solar"]["inverter_discharging_power_limit"]), 2)
        return surplus, max(solar - house, 0.0), solar, local

    def local_energy_available(self, period=None, from_timestamp=None, to_timestamp=None, solar_only=False,
                               without_all_exclusion=False) -> float:
        """Power (W) available from solar (+ battery unless solar_only) after the house consumption."""
        if period is not None:
            period = max(period, self.config["cron_interval"])
        from_timestamp, to_timestamp = self._window(from_timestamp, to_timestamp, period)
        surplus, solar_surplus, _, _ = self._local_energy(from_timestamp, to_timestamp, without_all_exclusion)
        return solar_surplus if solar_only else surplus

    def calc_local_energy_kwh(self, from_timestamp, to_timestamp, kwh: float) -> tuple[float, float]:
        """(kWh charged from local energy, of which solar) for kwh charged in the window."""
        if not kwh:
            return 0.0, 0.0
        watt = kwh * 1000.0
        from_local, _, solar, local = self._local_energy(from_timestamp, to_timestamp, without_all_exclusion=True)
        solar_part = solar / local * from_local if from_local > 0.0 and local > 0.0 else 0.0
        if from_local > watt:
            solar_part *= from_local / watt
            from_local = watt
        return round(min(round(max(from_local, 0.0) / 1000, 3), kwh), 3), round(solar_part / 1000, 3)

    def kwh_charged_by_solar(self) -> None:
        """Add the solar energy charged into the battery the last hour to the kWh sensor."""
        if not self.is_solar_configured():
            return
        now = self.now()
        charging_watt = self._charging_watt(self.get_powerwall_values(now - datetime.timedelta(minutes=60), now))
        solar_watt = self.local_energy_available(period=60, without_all_exclusion=True, solar_only=True)
        if solar_watt and self._out is not None:
            self._out.solar_kwh_increment += round(min(round(max(solar_watt, 0.0), 3), charging_watt) / 1000, 3)

    def _power_consumption_available(self) -> bool:
        entity_id = self.config["home"]["entity_ids"]["power_consumption_entity_id"]
        return not entity_id or self.is_entity_available(entity_id)

    def inverter_available(self) -> bool:
        return self.is_solar_configured() and self._power_consumption_available()

    def load_solar_available_db(self, database) -> None:
        database = deepcopy(database) if isinstance(database, dict) else {}
        database.pop("version", None)
        for hour in range(24):
            scores = database.setdefault(hour, {})
            for score in SOLAR_SCORES:
                scores.setdefault(score, [])
        self.solar_available_db = database

    def solar_available_append_to_db(self, power: float) -> None:
        if not self.inverter_available():
            return
        if not self.solar_available_db:
            self.load_solar_available_db({})
        hour = self.now().hour
        forecast = self.get_forecast(self.now())
        if forecast is not None:
            score = get_closest_key(self.forecast_score(forecast), self.solar_available_db[hour], return_key=True)
        else:
            weather = self.config["forecast"]["entity_ids"]
            condition = self.entity_state(weather["hourly_service_entity_id"]) or self.entity_state(weather["daily_service_entity_id"])
            score = WEATHER_CONDITION_DICT.get(condition, 50.0)
            score = get_closest_key(score, self.solar_available_db[hour], return_key=True)
        keep = self.config["database"]["solar_available_db_data_to_save"]
        scores = self.solar_available_db[hour]
        scores[score] = [[self.now(), power], *scores.get(score, [])][:keep]
        self.mark_dirty("solar_available")
