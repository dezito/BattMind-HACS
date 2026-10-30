"""Sell prices from the price providers."""
from __future__ import annotations

import datetime

import pytest

from custom_components.battmind.const import build_config, default_options
from custom_components.battmind.prices.base import calculate_sell_price
from custom_components.battmind.prices.energidataservice import EnergiDataServicePriceProvider
from custom_components.battmind.prices.stromligning import StromligningPriceProvider

TODAY = datetime.date(2026, 10, 30)
TOMORROW = TODAY + datetime.timedelta(days=1)
# Strømligning tariffs ex VAT (a real export): distribution per period, the rest flat
DISTRIBUTION = {hour: 0.9358 if 17 <= hour <= 20 else 0.3119 for hour in range(24)}
FLAT_TARIFFS = {
    "sensor.sell_tax": 0.008,
    "sensor.sell_nettariff": 0.043,
    "sensor.sell_systemtariff": 0.072,
    "sensor.sell_surcharge": 0.04,
}
SPOT = {TODAY: 2.611, TOMORROW: 2.21}


def _rows(day: datetime.date, price) -> list[dict]:
    return [
        {"start": datetime.datetime.combine(day, datetime.time(hour)).isoformat() + "+01:00", "price": price(hour)}
        for hour in range(24)
    ]


def _sell_with_tariffs(day: datetime.date):
    """Strømligning's sell sensor is the consumer price: spot plus every tariff."""
    return lambda hour: SPOT[day] + DISTRIBUTION[hour] + sum(FLAT_TARIFFS.values())


def _provider() -> StromligningPriceProvider:
    config = build_config({
        **default_options(),
        "stromligning_buy_current_price": "sensor.buy",
        "stromligning_sell_current_price": "sensor.sell",
        "stromligning_sell_tomorrow_available": "binary_sensor.sell_tomorrow",
        "stromligning_sell_distribution": "sensor.sell_distribution",
        "stromligning_sell_electricity_tax": "sensor.sell_tax",
        "stromligning_sell_nettariff": "sensor.sell_nettariff",
        "stromligning_sell_systemtariff": "sensor.sell_systemtariff",
        "stromligning_sell_surcharge": "sensor.sell_surcharge",
        "sell_fee": 0.03,
    })
    provider = StromligningPriceProvider(config)
    provider.update(
        {
            "attributes": {
                "sensor.buy": {"prices": _rows(TODAY, lambda hour: 4.0)},
                "sensor.sell": {"prices": _rows(TODAY, _sell_with_tariffs(TODAY))},
                "binary_sensor.sell_tomorrow": {"forecast_data": False, "prices": _rows(TOMORROW, _sell_with_tariffs(TOMORROW))},
                # like the real sensor: only today's distribution tariffs
                "sensor.sell_distribution": {"prices": _rows(TODAY, DISTRIBUTION.get)},
                **{entity_id: {} for entity_id in FLAT_TARIFFS},
            },
            "values": dict(FLAT_TARIFFS),
        },
        datetime.datetime.combine(TODAY, datetime.time(16, 40)),
    )
    return provider


@pytest.mark.parametrize("day", [TODAY, TOMORROW])
@pytest.mark.parametrize("hour", [3, 17, 20, 21])
def test_stromligning_sell_price_is_spot_minus_the_selling_costs(day: datetime.date, hour: int) -> None:
    """Every tariff in the sell sensor is subtracted, also the distribution tariff of tomorrow,
    and selling costs the sell fee plus Energinet's net and system tariff."""
    provider = _provider()
    timestamp = datetime.datetime.combine(day, datetime.time(hour))
    assert provider.sell_real_prices[timestamp] == pytest.approx(round(SPOT[day] - 0.03 - 0.043 - 0.072, 2))


def test_selling_costs_lower_the_sell_price() -> None:
    tariffs = {"transmissions_nettarif": 0.1, "systemtarif": 0.1, "elafgift": 0.5, "tariffs": 0.2, "surcharge": 0.0}
    # 2.0 - 0.9 tariffs in the price - (0.03 fee + 0.1 net + 0.1 system tariff)
    assert calculate_sell_price(2.0, tariffs, 0.03) == pytest.approx(0.87)
    assert calculate_sell_price(2.0, tariffs, -0.03) == pytest.approx(0.87)
    # a raw price without tariffs: only the selling costs
    raw = {key: 0.0 for key in tariffs}
    assert calculate_sell_price(2.0, raw, 0.03, {"transmissions_nettarif": 0.05, "systemtarif": 0.09}) == pytest.approx(1.83)


def _eds_provider(sell_tariffs: bool) -> EnergiDataServicePriceProvider:
    config = build_config({
        **default_options(),
        "energidataservice_buy_entity_id": "sensor.eds_buy",
        "energidataservice_sell_entity_id": "sensor.eds_sell",
        "sell_fee": 0.03,
    })
    tariffs = {
        "additional_tariffs": {"transmissions_nettarif": 0.054, "systemtarif": 0.09, "elafgift": 0.01},
        "tariffs": {str(hour): 1.17 if 17 <= hour <= 20 else 0.39 for hour in range(24)},
    }
    rows = [{"hour": datetime.datetime.combine(TODAY, datetime.time(hour)).isoformat() + "+01:00", "price": 2.5} for hour in range(24)]
    sell = {"raw_today": rows, "tomorrow_valid": False}
    if sell_tariffs:
        sell["tariffs"] = tariffs
    provider = EnergiDataServicePriceProvider(config)
    provider.update(
        {"attributes": {"sensor.eds_buy": {"raw_today": rows, "tomorrow_valid": False, "tariffs": tariffs}, "sensor.eds_sell": sell}, "values": {}},
        datetime.datetime.combine(TODAY, datetime.time(16, 40)),
    )
    return provider


def test_raw_energi_data_service_sell_price_gets_the_selling_costs_from_the_buy_sensor() -> None:
    """The sell sensor without tariffs is the spot price: only fee, net and system tariff are subtracted."""
    timestamp = datetime.datetime.combine(TODAY, datetime.time(17))
    assert _eds_provider(False).sell_real_prices[timestamp] == pytest.approx(round(2.5 - 0.03 - 0.054 - 0.09, 2))
    # with tariffs in the sell sensor they are subtracted, plus the selling costs
    assert _eds_provider(True).sell_real_prices[timestamp] == pytest.approx(round(2.5 - 1.324 - 0.03 - 0.054 - 0.09, 2))
