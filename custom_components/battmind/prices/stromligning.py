"""Strømligning price provider."""
from __future__ import annotations

import datetime
import logging

from ..utils.helpers import to_datetime
from .base import BasePriceProvider, _naive, price_at

_LOGGER = logging.getLogger(__name__)


class StromligningPriceProvider(BasePriceProvider):
    """Strømligning ('prices' attribute with start/price, tariffs as separate sensors)."""

    source_name = "stromligning"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        get = self.entity_ids.get
        self.buy_current_price = get("stromligning_buy_current_price", "")
        self.buy_tomorrow_available = get("stromligning_buy_tomorrow_available", "")
        self.buy_forecasts = get("stromligning_buy_forecasts", "")
        self.sell_current_price = get("stromligning_sell_current_price", "")
        self.sell_tomorrow_available = get("stromligning_sell_tomorrow_available", "")
        self.sell_forecasts = get("stromligning_sell_forecasts", "")
        self.sell_distribution = get("stromligning_sell_distribution", "")
        self.sell_electricity_tax = get("stromligning_sell_electricity_tax", "")
        self.sell_nettariff = get("stromligning_sell_nettariff", "")
        self.sell_systemtariff = get("stromligning_sell_systemtariff", "")
        self.sell_surcharge = get("stromligning_sell_surcharge", "")
        self._distribution_prices: dict = {}

    def update(self, price_data: dict, now: datetime.datetime) -> None:
        self.attributes = price_data.get("attributes", {})
        self._distribution_prices = {}
        if self._has(self.sell_distribution):
            self._distribution_prices = dict(self._rows(self.attributes[self.sell_distribution].get("prices")))
        super().update(price_data, now)

    def buy_price_entity_id(self) -> str:
        return self.buy_current_price

    @staticmethod
    def _rows(rows):
        for raw in rows or []:
            timestamp = _naive(to_datetime(raw.get("start")))
            price = raw.get("price")
            if isinstance(timestamp, datetime.datetime) and isinstance(price, (int, float)):
                yield timestamp, price

    def _add(self, prices: dict, entity_id: str, forecast: bool) -> None:
        if not entity_id:
            return
        attr = self._attr(entity_id)
        if "prices" not in attr:
            raise ValueError(f"prices not in {entity_id} attributes")
        for timestamp, price in self._rows(attr["prices"]):
            if timestamp not in prices:
                prices[timestamp] = price + self._forecast_adder(timestamp) if forecast else price

    def _tomorrow_is_forecast(self, entity_id: str) -> bool | None:
        """None when the entity is missing, else whether its prices are forecasts."""
        if not self._has(entity_id):
            return None
        return bool(self.attributes[entity_id].get("forecast_data", False))

    def _real(self, current_entity_id: str, tomorrow_entity_id: str) -> dict:
        prices: dict = {}
        self._add(prices, current_entity_id, forecast=False)
        if self._tomorrow_is_forecast(tomorrow_entity_id) is False:
            self._add(prices, tomorrow_entity_id, forecast=False)
        return dict(sorted(prices.items()))

    def _forecast(self, forecast_entity_id: str, tomorrow_entity_id: str) -> dict:
        prices: dict = {}
        if self._tomorrow_is_forecast(tomorrow_entity_id):
            self._add(prices, tomorrow_entity_id, forecast=True)
        self._add(prices, forecast_entity_id, forecast=True)
        return dict(sorted(prices.items()))

    def _load_buy_real_prices(self):
        return self._real(self.buy_current_price, self.buy_tomorrow_available)

    def _load_buy_forecast_prices(self):
        return self._forecast(self.buy_forecasts, self.buy_tomorrow_available)

    def _load_sell_real_prices(self):
        return self._real(self.sell_current_price, self.sell_tomorrow_available)

    def _load_sell_forecast_prices(self):
        return self._forecast(self.sell_forecasts, self.sell_tomorrow_available)

    def _state(self, entity_id: str) -> float:
        value = self.values.get(entity_id, 0.0) if self._has(entity_id) else 0.0
        return value if isinstance(value, (int, float)) else 0.0

    def get_tariffs(self, timestamp):
        distribution = price_at(self._distribution_prices, timestamp)
        return {
            "transmissions_nettarif": distribution[1] if distribution else 0.0,
            "systemtarif": self._state(self.sell_systemtariff),
            "elafgift": self._state(self.sell_electricity_tax),
            "tariffs": self._state(self.sell_nettariff),
            "surcharge": self._state(self.sell_surcharge),
        }
