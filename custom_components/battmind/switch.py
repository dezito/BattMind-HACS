"""BattMind setting switches (replaces the input_boolean helpers)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import SWITCH_SETTINGS
from .coordinator import BattMindCoordinator
from .entity import BattMindEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: BattMindCoordinator = entry.runtime_data
    async_add_entities(BattMindSwitch(coordinator, key, info) for key, info in SWITCH_SETTINGS.items())


class BattMindSwitch(BattMindEntity, SwitchEntity, RestoreEntity):
    """A planner setting stored in the coordinator."""

    def __init__(self, coordinator: BattMindCoordinator, key: str, info: dict[str, Any]) -> None:
        super().__init__(coordinator, "switch", key)
        self._default = info["default"]
        if key != "deactivate_script":
            self._attr_entity_category = EntityCategory.CONFIG

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        value = self._default
        if (last := await self.async_get_last_state()) is not None and last.state in ("on", "off"):
            value = last.state == STATE_ON
        elif (legacy := self.legacy_state()) is not None and legacy.state in ("on", "off"):
            value = legacy.state == STATE_ON
        self.coordinator.async_set_setting(self._key, value, initial=True)

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.settings.get(self._key, self._default))

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.async_set_setting(self._key, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.async_set_setting(self._key, False)
        self.async_write_ha_state()
