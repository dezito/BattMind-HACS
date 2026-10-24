"""BattMind output sensors."""
from __future__ import annotations

from homeassistant.components.sensor import RestoreSensor, SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import MATCH_ALL, PERCENTAGE, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import POWERWALL_ACTION_STATES, SENSOR_KEYS
from .coordinator import BattMindCoordinator
from .entity import BattMindUpdatingEntity

MAX_STATE_LENGTH = 255


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: BattMindCoordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        BattMindSensor(coordinator, key) for key in SENSOR_KEYS if key != "kwh_charged_by_solar"
    ]
    if coordinator.config["solar"]["entity_ids"]["production_entity_id"]:
        entities.append(BattMindSolarKwhSensor(coordinator))
    async_add_entities(entities)


class BattMindSensor(BattMindUpdatingEntity, SensorEntity):
    """State and attributes published by the BattMind worker."""

    # the markdown/plan attributes are large; keep them out of the recorder database
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, coordinator: BattMindCoordinator, key: str) -> None:
        super().__init__(coordinator, "sensor", key)
        if key == "battery_level_needed":
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif key == "powerwall_action":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = list(POWERWALL_ACTION_STATES)

    @property
    def native_value(self):
        value = self.coordinator.data.get(self._key)
        if self._key == "powerwall_action" and value not in POWERWALL_ACTION_STATES:
            return None
        if isinstance(value, str) and len(value) > MAX_STATE_LENGTH:
            return value[: MAX_STATE_LENGTH - 1] + "…"
        return value

    @property
    def extra_state_attributes(self):
        return self.coordinator.attributes.get(self._key)


class BattMindSolarKwhSensor(BattMindUpdatingEntity, RestoreSensor):
    """kWh charged into the home battery from solar (accumulated, like the old input_number)."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: BattMindCoordinator) -> None:
        super().__init__(coordinator, "sensor", "kwh_charged_by_solar")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        value = self.coordinator.restore_settings.get("sensor.kwh_charged_by_solar")
        if value is None and (last := await self.async_get_last_sensor_data()) is not None:
            value = last.native_value
        if value is None and (legacy := self.legacy_state()) is not None:
            value = legacy.state
        try:
            self.coordinator.solar_kwh_total = float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            self.coordinator.solar_kwh_total = 0.0

    @property
    def native_value(self):
        return self.coordinator.solar_kwh_total
