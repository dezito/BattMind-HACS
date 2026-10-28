"""Price providers (Energi Data Service and Strømligning).

The coordinator collects a plain snapshot of the configured price entities on the
event loop::

    {"attributes": {entity_id: {...}}, "values": {entity_id: float}}

and the providers turn it into ``{naive local datetime: price}`` dicts in the worker.
With several providers the first one with a price for a timestamp wins; real prices
always win over forecasts.
"""
from .base import BasePriceProvider, CombinedPriceProvider, TARIFF_STATE_KEYS, periods_in_hour, price_at
from .energidataservice import EnergiDataServicePriceProvider
from .factory import create_price_provider
from .stromligning import StromligningPriceProvider

__all__ = [
    "BasePriceProvider",
    "CombinedPriceProvider",
    "EnergiDataServicePriceProvider",
    "StromligningPriceProvider",
    "TARIFF_STATE_KEYS",
    "create_price_provider",
    "periods_in_hour",
    "price_at",
]
