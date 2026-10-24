"""BattMind number settings (replaces the input_number helpers)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import NUMBER_SETTINGS
from .coordinator import BattMindCoordinator
from .entity import BattMindEntity
from .helpers import float_or_none


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: BattMindCoordinator = entry.runtime_data
    async_add_entities(BattMindNumber(coordinator, key, info) for key, info in NUMBER_SETTINGS.items())


class BattMindNumber(BattMindEntity, NumberEntity, RestoreEntity):
    """A numeric planner setting stored in the coordinator."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: BattMindCoordinator, key: str, info: dict[str, Any]) -> None:
        super().__init__(coordinator, "number", key)
        self._attr_native_min_value = info["min"]
        self._attr_native_max_value = info["max"]
        self._attr_native_step = info["step"]
        self._attr_mode = NumberMode.BOX if info["mode"] == "box" else NumberMode.SLIDER
        self._attr_native_unit_of_measurement = info["unit"]
        initial = coordinator.entry.data.get("initial_settings", {})
        self._default = float_or_none(initial.get(key))
        if self._default is None:
            self._default = float(info["default"])

    def _valid(self, value) -> float | None:
        value = float_or_none(value)
        if value is None:
            return None
        return min(max(value, self.native_min_value), self.native_max_value)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        value = None
        if (last := await self.async_get_restored_state()) is not None:
            value = self._valid(last.state)
        if value is None and (legacy := self.legacy_state()) is not None:
            value = self._valid(legacy.state)
        if value is None:
            value = self._default
        self.coordinator.async_set_setting(self._key, value, initial=True)

    @property
    def native_value(self) -> float | None:
        return self.coordinator.settings.get(self._key, self._default)

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.async_set_setting(self._key, value)
        self.async_write_ha_state()
