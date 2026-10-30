"""Energi Data Service price provider."""
from __future__ import annotations

import datetime
import logging

from ..utils.helpers import days_between, to_datetime
from .base import BasePriceProvider, _ZERO_TARIFFS, _naive

_LOGGER = logging.getLogger(__name__)


class EnergiDataServicePriceProvider(BasePriceProvider):
    """https://github.com/MTrab/energidataservice (raw_today / raw_tomorrow / forecast attributes)."""

    source_name = "energidataservice"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.buy_entity_id = self.entity_ids.get("energidataservice_buy_entity_id", "")
        self.sell_entity_id = self.entity_ids.get("energidataservice_sell_entity_id", "") or self.buy_entity_id

    def buy_price_entity_id(self) -> str:
        return self.buy_entity_id

    @staticmethod
    def _rows(rows):
        for raw in rows or []:
            timestamp = _naive(to_datetime(raw["hour"] if "hour" in raw else raw["time"]))
            price = raw["price"]
            if isinstance(timestamp, datetime.datetime) and isinstance(price, (int, float)):
                yield timestamp, price

    def _real(self, entity_id: str) -> dict:
        attr = self._attr(entity_id)
        prices = {ts: p for ts, p in self._rows(attr.get("raw_today")) if days_between(self.current_hour, ts) == 0}
        if attr.get("tomorrow_valid"):
            prices.update(self._rows(attr.get("raw_tomorrow")))
        return dict(sorted(prices.items()))

    def _forecast(self, entity_id: str) -> dict:
        attr = self._attr(entity_id)
        return {
            ts: p + self._forecast_adder(ts)
            for ts, p in self._rows(attr.get("forecast"))
            if days_between(self.current_hour, ts) > 0
        }

    def _load_buy_real_prices(self):
        return self._real(self.buy_entity_id)

    def _load_buy_forecast_prices(self):
        return self._forecast(self.buy_entity_id)

    def _load_sell_real_prices(self):
        return self._real(self.sell_entity_id)

    def _load_sell_forecast_prices(self):
        return self._forecast(self.sell_entity_id)

    def get_tariffs(self, timestamp):
        try:
            tariffs = self._attr(self.sell_entity_id).get("tariffs")
            if not tariffs:
                return dict(_ZERO_TARIFFS)
            additional = tariffs["additional_tariffs"]
            return {
                "transmissions_nettarif": additional["transmissions_nettarif"],
                "systemtarif": additional["systemtarif"],
                "elafgift": additional["elafgift"],
                "tariffs": tariffs["tariffs"][str(timestamp.hour)],
                "surcharge": 0.0,
            }
        except (KeyError, TypeError, ValueError) as err:
            _LOGGER.debug("No Energi Data Service tariffs for %s: %s", timestamp, err)
            return dict(_ZERO_TARIFFS)

    def get_sell_costs(self, timestamp):
        """A raw sell sensor (no tariffs attribute) includes no tariffs, but selling still costs
        Energinet's net and system tariff: those of the buy sensor."""
        if self._attr(self.sell_entity_id).get("tariffs"):
            return None
        return eds_sell_costs(self.attributes.get(self.buy_entity_id))


def eds_sell_costs(attributes: dict | None) -> dict[str, float] | None:
    """Energinet's net and system tariff from an Energi Data Service tariffs attribute, else None."""
    try:
        additional = (attributes or {})["tariffs"]["additional_tariffs"]
        return {
            "transmissions_nettarif": float(additional["transmissions_nettarif"]),
            "systemtarif": float(additional["systemtarif"]),
        }
    except (KeyError, TypeError, ValueError):
        return None
