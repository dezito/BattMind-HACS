"""Power measurements, weather/solar prediction and the hourly databases (engine mixin)."""
from __future__ import annotations

import datetime
import logging
from copy import deepcopy

from .const import WEATHER_CONDITION_DICT
from .helpers import (
    average,
    day_of_week,
    get_closest_key,
    get_specific_values,
    reset_time_to_hour,
    start_of_day,
    to_datetime,
)

_LOGGER = logging.getLogger(__name__)

# Weather scores used as keys in the solar database (0, 20, ..., 100)
SOLAR_SCORES = sorted(set(WEATHER_CONDITION_DICT.values()))

POWER_VALUE_KEYS = (
    "power_consumption",
    "ignored_consumption",
    "powerwall_charging_consumption",
    "powerwall_discharging_consumption",
    "solar_production",
    "power_consumption_without_ignored",
    "power_consumption_without_all_exclusion",
)


class EnergyMixin:
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

            if not home["power_consumption_entity_id_include_powerwall_discharging"]:
                consumption += discharging
            if home["power_consumption_entity_id_include_powerwall_charging"]:
                consumption -= charging
            without_ignored = round(consumption - ignored, 2)

            result.update(
                power_consumption=consumption,
                ignored_consumption=ignored,
                powerwall_charging_consumption=charging,
                powerwall_discharging_consumption=discharging,
                solar_production=solar,
                power_consumption_without_ignored=without_ignored,
                power_consumption_without_all_exclusion=max(round(without_ignored - charging, 2), 0.0),
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to get power values from %s to %s: %s", from_timestamp, to_timestamp, err)
            self.notify(
                f"Failed to get power values from {from_timestamp} to {to_timestamp}: {err} {type(err)}",
                title=f"{self.title} error",
                notification_id=f"{self.name}_power_values",
            )
        return result

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

    # ------------------------------------------------------------------ #
    # Weather forecast
    # ------------------------------------------------------------------ #

    def _forecast_index(self) -> tuple[dict, dict]:
        """Hourly forecasts by hour and daily forecasts by date (parsed once per job)."""
        if "forecast_index" not in self._job_cache:
            hourly: dict = {}
            daily: dict = {}
            forecast_dict = self._snap.forecast_dict if self._snap else {}
            for data in forecast_dict.get("hourly") or []:
                if data:
                    hourly.setdefault(reset_time_to_hour(to_datetime(data["datetime"])), data)
            for data in forecast_dict.get("daily") or []:
                if data:
                    daily.setdefault(to_datetime(data["datetime"]).date(), data)
            self._job_cache["forecast_index"] = (hourly, daily)
        return self._job_cache["forecast_index"]

    def get_forecast(self, date: datetime.datetime):
        """Hourly forecast for the hour of date, else the daily forecast for the day."""
        hourly, daily = self._forecast_index()
        return hourly.get(reset_time_to_hour(date)) or daily.get(date.date())

    @staticmethod
    def forecast_score(data) -> float:
        """0-100 score of how good the weather is for solar production."""
        try:
            if "cloud_coverage" not in data or "uv_index" not in data or "temperature" not in data:
                return WEATHER_CONDITION_DICT[data["condition"]]
            cloud = (100 - data["cloud_coverage"]) / 100
            uv = min(data["uv_index"] / 11, 1)
            temperature = data["temperature"]
            temperature_score = temperature / 20 if temperature <= 20 else 20 / temperature
            if data["cloud_coverage"] >= 90:
                cloud *= 0.2
                uv *= 0.1
                temperature_score *= 0.5
            return (cloud * 0.5 + uv * 0.3 + temperature_score * 0.2) * 100
        except (KeyError, TypeError, ZeroDivisionError):
            return 50.0

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

    # ------------------------------------------------------------------ #
    # Price history: price per hour and weekday (newest first) + daily max/mean/min
    # ------------------------------------------------------------------ #

    def load_kwh_prices(self, database) -> None:
        if database is not None:
            database = deepcopy(database) if isinstance(database, dict) else {}
            database.pop("version", None)
            self.kwh_avg_prices_db = database
        db = self.kwh_avg_prices_db
        for name in ("history", "history_sell"):
            for hour in range(24):
                for dow in range(7):
                    db.setdefault(name, {}).setdefault(hour, {}).setdefault(dow, [])
        for name in ("max", "mean", "min"):
            db.setdefault(name, [])

    def append_kwh_prices(self) -> None:
        """Store today's prices once a day (needs at least 23 hours)."""
        if not self.kwh_avg_prices_db:
            self.load_kwh_prices({})
        db = self.kwh_avg_prices_db
        today = self.now().date()
        if isinstance(db.get("last_save"), datetime.datetime) and db["last_save"].date() == today:
            return
        solar = self.is_solar_configured()
        refund = self.price_provider.refund if self.price_provider else 0.0
        grid = {ts.hour: price for ts, price in self.get_grid_prices().items() if ts.date() == today}
        sell = {ts.hour: price for ts, price in self.get_sell_prices().items() if ts.date() == today} if solar else {}
        if len(grid) < 23 or (solar and len(sell) < 23):
            _LOGGER.warning("Missing hours in today's prices (buy %s, sell %s), not saving the price history", len(grid), len(sell))
            return

        keep = self.config["database"]["kwh_avg_prices_db_data_to_save"]
        prices = [price - refund for price in grid.values()]
        db["max"] = [max(prices), *db["max"]][:keep]
        db["mean"] = [round(average(prices), 3), *db["mean"]][:keep]
        db["min"] = [min(prices), *db["min"]][:keep]
        dow = day_of_week(self.now())
        for hour, price in grid.items():
            db["history"][hour][dow] = [price - refund, *db["history"][hour][dow]][:keep]
            if hour in sell:
                db["history_sell"][hour][dow] = [round(sell[hour], 3), *db["history_sell"][hour][dow]][:keep]
        db["last_save"] = self.now()
        self.mark_dirty("kwh_avg_prices")

    # ------------------------------------------------------------------ #
    # Solar prediction
    # ------------------------------------------------------------------ #

    def get_solar_kwh_forecast(self) -> dict:
        """{hour: (kWh surplus, sell price)} from Solcast minus the predicted consumption."""
        if not self.config["solar"]["entity_ids"]["forecast_entity_id"]:
            return {}
        sell_prices = self.get_sell_prices()
        forecast = {}
        for entity_id, rows in (self._snap.solcast if self._snap else {}).items():
            try:
                for data in rows:
                    date = to_datetime(data["period_start"])
                    if date not in sell_prices:
                        continue
                    consumption = self.get_forecast_value(
                        self.power_values_db[date.hour].get("power_consumption_without_all_exclusion", [0.0])
                    )
                    surplus_kwh = round(max(data["pv_estimate"] * 1000.0 - consumption, 0.0) / 1000.0, 3)
                    forecast[date] = (surplus_kwh, sell_prices[date])
            except (KeyError, TypeError, AttributeError) as err:
                _LOGGER.error("Invalid Solcast data in %s: %s", entity_id, err)
        return forecast

    def _database_solar(self, score: float, date: datetime.datetime, fixed_sell_price: float) -> tuple[float, float]:
        """(kWh surplus, sell price) for an hour from the stored solar history at a weather score."""
        scores = self.solar_available_db.get(date.hour)
        if not scores:
            return 0.0, 0.0
        power_list = get_closest_key(score, scores) or []
        forecast_value = self.get_forecast_value(power_list) if power_list else 0.0
        neighbours = []
        if len(power_list) <= 6 or forecast_value <= 1000.0:
            # little or low history: blend with the neighbouring weather scores
            if score >= 20 and (lower := get_closest_key(score - 20, scores)):
                neighbours.append(self.get_forecast_value(lower))
            if score <= 80 and (upper := get_closest_key(score + 20, scores)):
                neighbours.append(self.get_forecast_value(upper))
        values = ([forecast_value] * 3 if power_list else []) + neighbours
        sell_price = fixed_sell_price
        if sell_price == -1.0:
            sell_price = self.get_forecast_value(self.kwh_avg_prices_db["history_sell"][date.hour][day_of_week(date)])
        return max(average(values), 0.0) / 1000, sell_price

    def local_energy_prediction(self) -> None:
        """Predict the solar surplus per hour for today + the next days (self.solar_prediction_db)."""
        if not self.inverter_available():
            return
        fixed_sell_price = self.config["solar"]["production_price"]
        solcast = self.get_solar_kwh_forecast()
        now = self.now()
        today = start_of_day(now)

        for day in range(int(self.config["days_to_predict"]) + 1):
            prediction = {key: [] for key in ("total_database", "total_database_sell", "total_forecast", "total_forecast_sell", "total", "total_sell")}
            for hour in range(24):
                timestamp = today + datetime.timedelta(days=day, hours=hour)
                # only the remaining part of the current hour
                factor = 1.0 - now.minute / 60.0 if day == 0 and hour == now.hour else 1.0
                forecast = self.get_forecast(timestamp)
                hour_kwh = []
                hour_sell = []
                if forecast is not None:
                    kwh, sell = self._database_solar(self.forecast_score(forecast), timestamp, fixed_sell_price)
                    weight = (0.5 if timestamp in solcast else 1.0) * factor
                    hour_kwh.append(round(kwh * weight, 1))
                    hour_sell.append(round(sell * weight, 1))
                    prediction["total_database"].append(round(kwh, 1))
                    prediction["total_database_sell"].append(round(sell, 1))
                if timestamp in solcast:
                    kwh, sell = solcast[timestamp]
                    weight = (1.5 if forecast is not None else 1.0) * factor
                    hour_kwh.append(round(kwh * weight, 1))
                    hour_sell.append(round(sell * weight, 1))
                    prediction["total_forecast"].append(round(kwh, 1))
                    prediction["total_forecast_sell"].append(round(sell, 1))
                if hour_kwh:
                    prediction["total"].append(round(average(hour_kwh), 1))
                    prediction["total_sell"].append(round(average(hour_sell), 1))
            self.solar_prediction_db[day] = prediction
