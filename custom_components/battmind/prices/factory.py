"""Builds the price provider for the configured price entities."""
from __future__ import annotations

from .base import BasePriceProvider, CombinedPriceProvider
from .energidataservice import EnergiDataServicePriceProvider
from .stromligning import StromligningPriceProvider


def create_price_provider(config: dict) -> CombinedPriceProvider:
    entity_ids = config["prices"]["entity_ids"]
    providers: list[BasePriceProvider] = []
    if entity_ids.get("stromligning_buy_current_price"):
        providers.append(StromligningPriceProvider(config))
    if entity_ids.get("energidataservice_buy_entity_id"):
        providers.append(EnergiDataServicePriceProvider(config))
    if not providers:
        raise ValueError("No supported price provider configured")
    return CombinedPriceProvider(config, providers)
