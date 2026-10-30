"""Shared price helpers, the base provider and the combined provider."""
from __future__ import annotations

import datetime
import logging

from ..const import PRICE_ADDER_DAY_BETWEEN_DIVIDER
from ..utils.helpers import days_between, reset_time_to_hour

_LOGGER = logging.getLogger(__name__)


TARIFF_STATE_KEYS = (
    "stromligning_sell_electricity_tax",
    "stromligning_sell_nettariff",
    "stromligning_sell_systemtariff",
    "stromligning_sell_surcharge",
)

_ZERO_TARIFFS = {
    "transmissions_nettarif": 0.0,
    "systemtarif": 0.0,
    "elafgift": 0.0,
    "tariffs": 0.0,
    "surcharge": 0.0,
}


def calculate_sell_price(
    price: float, tariffs: dict[str, float], sell_fee: float, sell_costs: dict[str, float] | None = None
) -> float:
    """Sell price: the tariffs, taxes and surcharge in the price are subtracted (spot price), and
    then what selling a kWh costs: the sell fee plus Energinet's net and system tariff.

    tariffs: the tariffs included in price. sell_costs: the net and system tariff paid when
    selling (default: those in tariffs), for a raw sell price that includes no tariffs.
    """
    costs = tariffs if sell_costs is None else sell_costs
    return round(
        price
        - sum(tariffs.values())
        - abs(sell_fee)
        - costs.get("transmissions_nettarif", 0.0)
        - costs.get("systemtarif", 0.0),
        2,
    )


def _naive(timestamp):
    return timestamp.replace(tzinfo=None) if isinstance(timestamp, datetime.datetime) else timestamp


def price_at(prices: dict, timestamp: datetime.datetime):
    """(price timestamp, price) of the period containing timestamp (same day), else None."""
    best = None
    for price_timestamp in prices:
        if price_timestamp <= timestamp and price_timestamp.date() == timestamp.date():
            if best is None or price_timestamp > best:
                best = price_timestamp
    return None if best is None else (best, prices[best])


def periods_in_hour(prices: dict, timestamp: datetime.datetime) -> int:
    """Number of price periods in the hour of timestamp (4 for 15 min prices). At least 1."""
    hour_start = timestamp.replace(minute=0, second=0, microsecond=0)
    hour_end = hour_start + datetime.timedelta(hours=1)
    return sum(1 for ts in prices if hour_start <= ts < hour_end) or 1


class BasePriceProvider:
    """Prices from one integration."""

    source_name = "base"

    def __init__(self, config: dict) -> None:
        prices_config = config["prices"]
        self.entity_ids: dict[str, str] = dict(prices_config["entity_ids"])
        self.sell_fee = abs(prices_config.get("sell_fee", 0.0))
        self.attributes: dict[str, dict] = {}
        self.values: dict[str, float] = {}
        self.current_hour = reset_time_to_hour(datetime.datetime.now())
        self.buy_real_prices: dict = {}
        self.buy_forecast_prices: dict = {}
        self.sell_real_prices: dict = {}
        self.sell_forecast_prices: dict = {}

    def update(self, price_data: dict, now: datetime.datetime) -> None:
        """Recalculate all prices. On error the previous prices are kept."""
        self.attributes = price_data.get("attributes", {})
        self.values = price_data.get("values", {})
        self.current_hour = reset_time_to_hour(now)
        buy_real = self._load_buy_real_prices()
        buy_forecast = self._load_buy_forecast_prices()
        sell_real = {ts: self.calculate_sell_price(ts, p) for ts, p in self._load_sell_real_prices().items()}
        sell_forecast = {ts: self.calculate_sell_price(ts, p) for ts, p in self._load_sell_forecast_prices().items()}
        self.buy_real_prices, self.buy_forecast_prices = buy_real, buy_forecast
        self.sell_real_prices, self.sell_forecast_prices = sell_real, sell_forecast

    def _attr(self, entity_id: str) -> dict:
        if not entity_id or entity_id not in self.attributes:
            raise ValueError(f"{entity_id} not loaded")
        return self.attributes[entity_id] or {}

    def _has(self, entity_id: str) -> bool:
        return bool(entity_id) and entity_id in self.attributes

    def _forecast_adder(self, timestamp: datetime.datetime) -> float:
        """Forecast prices get a small penalty per day ahead to prefer known prices."""
        return days_between(self.current_hour, timestamp) / PRICE_ADDER_DAY_BETWEEN_DIVIDER

    def calculate_sell_price(self, timestamp: datetime.datetime, price: float) -> float:
        tariff = self.get_tariffs(timestamp)
        return calculate_sell_price(price, tariff, self.sell_fee, self.get_sell_costs(timestamp))

    def buy_price_entity_id(self) -> str:
        return ""

    def get_tariffs(self, timestamp: datetime.datetime) -> dict:
        """The tariffs included in the sell price."""
        return dict(_ZERO_TARIFFS)

    def get_sell_costs(self, timestamp: datetime.datetime) -> dict | None:
        """Net and system tariff paid when selling, None = those of get_tariffs()."""
        return None

    def _load_buy_real_prices(self) -> dict:
        raise NotImplementedError

    def _load_buy_forecast_prices(self) -> dict:
        return {}

    def _load_sell_real_prices(self) -> dict:
        raise NotImplementedError

    def _load_sell_forecast_prices(self) -> dict:
        return {}


class CombinedPriceProvider:
    """Merges the configured providers."""

    def __init__(self, config: dict, providers: list[BasePriceProvider]) -> None:
        self.providers = providers
        self.refund = abs(config["prices"].get("refund", 0.0))
        self.buy_prices: dict = {}
        self.sell_prices: dict = {}
        self.buy_real_prices: dict = {}
        self.sell_real_prices: dict = {}
        self.buy_price_sources: dict = {}
        self.sell_price_sources: dict = {}

    def update_prices(self, price_data: dict | None, now: datetime.datetime) -> None:
        for provider in self.providers:
            try:
                provider.update(price_data or {}, now)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Can't update prices from %s: %s", provider.source_name, err)
        self.buy_prices, self.buy_real_prices, self.buy_price_sources = self._combine("buy")
        self.sell_prices, self.sell_real_prices, self.sell_price_sources = self._combine("sell")

    def _combine(self, kind: str) -> tuple[dict, dict, dict]:
        prices: dict = {}
        real: dict = {}
        sources: dict = {}
        for provider in self.providers:
            for timestamp, price in getattr(provider, f"{kind}_real_prices").items():
                if timestamp not in prices:
                    prices[timestamp] = real[timestamp] = round(price, 2)
                    sources[timestamp] = f"{provider.source_name}:real"
        for provider in self.providers:
            for timestamp, price in getattr(provider, f"{kind}_forecast_prices").items():
                if timestamp not in prices:
                    prices[timestamp] = round(price, 2)
                    sources[timestamp] = f"{provider.source_name}:forecast"
        return dict(sorted(prices.items())), dict(sorted(real.items())), dict(sorted(sources.items()))

    def buy_prices_available(self) -> bool:
        return bool(self.buy_prices)

    def buy_price_entity_id(self) -> str:
        return ", ".join(p.buy_price_entity_id() for p in self.providers if p.buy_price_entity_id())

    def sell_periods_in_hour(self, timestamp: datetime.datetime) -> int:
        return periods_in_hour(self.sell_real_prices, timestamp)

    def current_price_timestamp(self, timestamp: datetime.datetime, sell_prices: bool = False):
        result = price_at(self.sell_real_prices if sell_prices else self.buy_real_prices, timestamp)
        return None if result is None else result[0]
