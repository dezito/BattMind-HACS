"""Buy and sell prices and the price of the energy in the battery."""
from __future__ import annotations

import datetime
import logging

from ..const import PRICE_ADDER_DAY_BETWEEN_DIVIDER
from ..utils.helpers import day_of_week, days_between, minutes_between, reset_time_to_hour, start_of_day
from ..prices import price_at

_LOGGER = logging.getLogger(__name__)


class Pricing:
    """Buy and sell prices and the price of the energy in the battery."""

    def update_grid_prices(self, initial_run: bool = False) -> None:
        if self.price_provider is None:
            return
        try:
            self.price_provider.update_prices(self._snap.price_data if self._snap else None, self.now())
            self.get_grid_prices(update_prices=True)
            self.get_sell_prices(update_prices=True)
            if not initial_run:
                self.append_kwh_prices()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error in update_grid_prices: %s", err)
            self.notify(
                f"Error in update_grid_prices: {err} {type(err)}",
                title=f"{self.title} error",
                notification_id=f"{self.name}_update_grid_prices_error",
            )

    def get_hour_prices(self, update_prices: bool = False, sell_prices: bool = False) -> dict:
        """Prices per period. Returned dicts are shared - do not modify them.

        Online prices are refreshed with update_prices=True (startup + minute 2). When the
        provider has no prices, the last successful prices are used for 2 hours and after
        that offline prices forecast from the price history database.
        """
        now = self.now()
        cache = self.sell_price_cache if sell_prices else self.grid_price_cache
        kind = "sell" if sell_prices else "grid"

        # forecasts corrected with their measured bias per day ahead (the analysis)
        corrections = self.price_forecast_corrections()
        if "prices" in cache and not cache["using_offline_prices"] and not update_prices and cache.get("corrections") == corrections:
            return cache["prices"]

        provider = self.price_provider
        prices = (provider.sell_prices if sell_prices else provider.buy_prices) if provider else {}
        if prices:
            sources = provider.sell_price_sources if sell_prices else provider.buy_price_sources
            if corrections:
                current_hour = reset_time_to_hour(now)
                prices = {
                    timestamp: round(price + corrections.get(days_between(current_hour, timestamp), 0.0), 2)
                    if str(sources.get(timestamp, "")).endswith(":forecast") else price
                    for timestamp, price in prices.items()
                }
            # hours of the plan the provider has no price for are filled from the price history
            missing_hours = self._database_prices(
                "history_sell" if sell_prices else "history", prices, int(self.config["days_to_predict"])
            )
            if missing_hours:
                _LOGGER.debug("No %s price for %s hours, using the price history", kind, len(missing_hours))
                prices = dict(sorted({**prices, **missing_hours}.items()))
                sources = {**sources, **{timestamp: "database" for timestamp in missing_hours}}
            cache.update(
                prices=prices,
                last_update=now,
                using_offline_prices=False,
                sources=sources,
                missing_hours=missing_hours,
                corrections=corrections,
            )
            return prices

        if cache.get("prices") and "last_update" in cache and minutes_between(cache["last_update"], now) <= 120:
            _LOGGER.warning("No online %s prices, using the last successful prices", kind)
            return cache["prices"]

        _LOGGER.warning("No online %s prices, using offline prices from the price history", kind)
        try:
            prices, missing_hours = self._offline_prices("history_sell" if sell_prices else "history")
        except KeyError as err:
            message = f"Can't get offline prices: {err}"
            _LOGGER.error(message)
            self.notify(message, f"{self.title} error", f"{self.name}_get_hour_prices_offline_prices_error")
            raise
        cache.update(
            prices=prices,
            last_update=now,
            using_offline_prices=True,
            sources={timestamp: "offline" for timestamp in missing_hours},
            missing_hours=missing_hours,
        )
        return prices

    def _offline_prices(self, history_key: str) -> tuple[dict, dict]:
        """Prices for the next 7 days forecast from the price history per hour and weekday."""
        prices = self._database_prices(history_key, {}, 7)
        return prices, prices

    def _database_prices(self, history_key: str, prices: dict, days: int) -> dict:
        """Prices from the price history for the hours from now until the end of days that have none in prices.

        The forecast of the same hour and weekday, plus the same small daily adder as the
        provider forecasts. Hours without any history are left out.
        """
        if not self.kwh_avg_prices_db:
            self.load_kwh_prices(None)
        history = self.kwh_avg_prices_db[history_key]
        current_hour = reset_time_to_hour(self.now())
        end = start_of_day(self.now()) + datetime.timedelta(days=days)
        priced_hours = {reset_time_to_hour(timestamp) for timestamp in prices}
        missing = {}
        timestamp = current_hour
        while timestamp < end:
            values = history.get(timestamp.hour, {}).get(day_of_week(timestamp), [])
            if timestamp not in priced_hours and values:
                adder = days_between(current_hour, timestamp) / PRICE_ADDER_DAY_BETWEEN_DIVIDER
                missing[timestamp] = round(self.get_forecast_value(values) + adder, 2)
            timestamp += datetime.timedelta(hours=1)
        return missing

    def get_grid_prices(self, update_prices: bool = False) -> dict:
        return self.get_hour_prices(update_prices=update_prices)

    def get_sell_prices(self, update_prices: bool = False) -> dict:
        return self.get_hour_prices(update_prices=update_prices, sell_prices=True)

    def sell_value(self, price: float) -> float:
        """What a sold kWh is worth at this hourly sell price.

        With a fixed solar sell price (a production agreement that settles the difference to the
        spot price) it is the fixed price minus the hourly price - negative when the hourly price
        is above the fixed price. Without one (-1) it is the hourly price itself.
        """
        fixed = self.config["solar"]["production_price"]
        return price if fixed == -1.0 else fixed - price

    def _current_sell_price(self) -> float:
        """What a sold kWh is worth now (provider price or price history, see sell_value)."""
        if not self.is_solar_configured():
            return 0.0
        result = price_at(self.get_sell_prices(), self.now())
        if result is not None:
            return round(self.sell_value(result[1]), 3)
        try:
            return self.sell_value(self.get_forecast_value(self.kwh_avg_prices_db["history_sell"][self.now().hour][day_of_week(self.now())]))
        except KeyError:
            return 0.0

    def get_powerwall_kwh_price(self) -> float:
        """Price per kWh of the energy in the battery, estimated from what was charged the last 24 hours.

        Used when the charging history has no price yet. Cached for the job, as the planner
        asks for it for every hour it evaluates.
        """
        if "powerwall_kwh_price" in self._job_cache:
            return self._job_cache["powerwall_kwh_price"]
        solar = self.config["solar"]
        min_battery_kwh = self.percentage_to_kwh(solar["powerwall_battery_level_min"])
        kwh = max(self.percentage_to_kwh(self.get_battery_level()) - min_battery_kwh, min_battery_kwh)
        timestamp = reset_time_to_hour(self.now())
        prices = self.get_grid_prices()
        sell_prices = self.get_sell_prices()
        history = self.kwh_avg_prices_db

        charged_kwh: list[float] = []
        total_cost: list[float] = []
        kwh_prices: list[float] = []
        try:
            for hour in range(24):
                if sum(charged_kwh) > kwh:
                    break
                loop_timestamp = timestamp - datetime.timedelta(hours=hour)
                loop_end = loop_timestamp.replace(minute=59, second=59)
                days_ago = (timestamp - loop_timestamp).days
                dow = day_of_week(loop_timestamp)
                data = self.power_values(loop_end - datetime.timedelta(hours=1), loop_end)

                charged = data["powerwall_charging_consumption"] / 1000.0
                if charged <= 0.0:
                    continue
                consumption = data["power_consumption_without_all_exclusion"] / 1000.0
                solar_available = max(data["solar_production"] / 1000.0 - consumption, 0.0)
                solar_share = min(solar_available, charged) / charged
                grid_share = 1.0 - solar_share

                if loop_timestamp in prices:
                    grid_price = prices[loop_timestamp]
                else:
                    try:
                        grid_price = history["history"][hour][dow][days_ago]
                    except (KeyError, IndexError):
                        grid_price = self.get_forecast_value(history["history"][hour][dow])
                if loop_timestamp in sell_prices:
                    sell_price = sell_prices[loop_timestamp]
                else:
                    try:
                        sell_price = history["history_sell"][hour][dow][days_ago]
                    except (KeyError, IndexError):
                        sell_price = self.get_forecast_value(history["history_sell"][hour][dow])

                kwh_price = grid_price * grid_share + self.sell_value(sell_price) * solar_share
                kwh_prices.append(round(kwh_price, 3))
                charged_kwh.append(round(charged, 3))
                total_cost.append(round(charged * kwh_price, 3))
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error getting the battery kWh price: %s", err)
            price = self._current_sell_price()
            self._job_cache["powerwall_kwh_price"] = price
            return price

        if charged_kwh:
            self.powerwall_price_details = {
                "powerwall_kwh": charged_kwh,
                "powerwall_total_cost": total_cost,
                "kwh_price_list": kwh_prices,
                "kr_per_kwh": round(sum(total_cost) / sum(charged_kwh), 3) if sum(charged_kwh) > 0.0 else 0.0,
            }
        unit = sum(total_cost) / sum(charged_kwh) if sum(charged_kwh) > 0.0 else 0.0
        price = unit + self.calc_battery_loss_cost(unit) + abs(solar["powerwall_wear_cost_per_kwh"])
        self._job_cache["powerwall_kwh_price"] = price
        return price

    @staticmethod
    def find_nth_local_min(price_dict: dict, n: int = 1, include_edge_last: bool = True):
        """Timestamp of the n'th local price minimum (in time order)."""
        times = sorted(price_dict)
        values = [price_dict[t] for t in times]
        # values[i - 1] wraps around for i == 0, like the PyScript version
        minima = [times[i] for i in range(len(values) - 1) if values[i - 1] >= values[i] < values[i + 1]]
        if include_edge_last and len(values) >= 2 and values[-1] < values[-2]:
            minima.append(times[-1])
        return minima[n - 1] if n - 1 < len(minima) else None
