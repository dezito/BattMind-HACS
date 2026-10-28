"""The database of the price of the energy in the battery."""
from __future__ import annotations

import datetime
import logging
from copy import deepcopy

from ...utils.helpers import average, day_of_week

_LOGGER = logging.getLogger(__name__)


class EnergyPrice:
    """The database of the price of the energy in the battery."""

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
        provider = self.price_provider
        refund = provider.refund if provider else 0.0
        # only the provider's prices - the hours filled from the history must not be stored again
        grid = {ts.hour: price for ts, price in (provider.buy_prices if provider else {}).items() if ts.date() == today}
        sell = {ts.hour: price for ts, price in (provider.sell_prices if provider else {}).items() if ts.date() == today}
        if len(grid) < 23:
            _LOGGER.warning("Missing hours in today's prices (buy %s, sell %s), not saving the price history", len(grid), len(sell))
            return
        if len(sell) < 23:
            # a day with missing sell hours only stores the buy prices (sell prices are stored also
            # without solar panels - the battery sells too)
            sell = {}

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
