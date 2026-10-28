"""Weather and solar forecast and the local energy prediction."""
from __future__ import annotations

import datetime
import logging

from ...const import WEATHER_CONDITION_DICT
from ...utils.helpers import (
    average,
    day_of_week,
    get_closest_key,
    reset_time_to_hour,
    start_of_day,
    to_datetime,
)

_LOGGER = logging.getLogger(__name__)


# Weather scores used as keys in the solar database (0, 20, ..., 100)
SOLAR_SCORES = sorted(set(WEATHER_CONDITION_DICT.values()))


class EnergyForecast:
    """Weather and solar forecast and the local energy prediction."""

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
                    forecast[date] = (surplus_kwh, self.sell_value(sell_prices[date]))
            except (KeyError, TypeError, AttributeError) as err:
                _LOGGER.error("Invalid Solcast data in %s: %s", entity_id, err)
        return forecast

    def _database_solar(self, score: float, date: datetime.datetime) -> tuple[float, float]:
        """(kWh surplus, sell value) for an hour from the stored solar history at a weather score."""
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
        sell_price = self.sell_value(self.get_forecast_value(self.kwh_avg_prices_db["history_sell"][date.hour][day_of_week(date)]))
        return max(average(values), 0.0) / 1000, sell_price

    def local_energy_prediction(self) -> None:
        """Predict the solar surplus per hour for today + the next days (self.solar_prediction_db)."""
        if not self.inverter_available():
            return
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
                    kwh, sell = self._database_solar(self.forecast_score(forecast), timestamp)
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
