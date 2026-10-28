"""Builds the price provider for the configured price entities."""
from __future__ import annotations

from ..const import PRICE_PROVIDER_PRIORITY
from .base import BasePriceProvider, CombinedPriceProvider
from .energidataservice import EnergiDataServicePriceProvider
from .stromligning import StromligningPriceProvider

PROVIDER_CLASSES: dict[str, tuple[str, type[BasePriceProvider]]] = {
    "stromligning": ("stromligning_buy_current_price", StromligningPriceProvider),
    "energidataservice": ("energidataservice_buy_entity_id", EnergiDataServicePriceProvider),
}


def create_price_provider(config: dict) -> CombinedPriceProvider:
    entity_ids = config["prices"]["entity_ids"]
    providers: list[BasePriceProvider] = [
        provider_class(config)
        for buy_key, provider_class in (PROVIDER_CLASSES[name] for name in PRICE_PROVIDER_PRIORITY)
        if entity_ids.get(buy_key)
    ]
    if not providers:
        raise ValueError("No supported price provider configured")
    return CombinedPriceProvider(config, providers)
