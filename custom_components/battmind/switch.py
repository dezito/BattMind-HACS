"""BattMind setting switches (replaces the input_boolean helpers)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, SWITCH_SETTINGS
from .coordinator import BattMindCoordinator
from .entity import BattMindEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: BattMindCoordinator = entry.runtime_data
    # removed settings: deactivate_script (automations are switched off instead) and
    # optimizer_shadow_plan (the optimisation is always calculated)
    registry = er.async_get(hass)
    for key in ("deactivate_script", "optimizer_shadow_plan"):
        if entity_id := registry.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_{key}"):
            registry.async_remove(entity_id)
    async_add_entities(BattMindSwitch(coordinator, key, info) for key, info in SWITCH_SETTINGS.items())


class BattMindSwitch(BattMindEntity, SwitchEntity, RestoreEntity):
    """A planner setting stored in the coordinator."""

    def __init__(self, coordinator: BattMindCoordinator, key: str, info: dict[str, Any]) -> None:
        super().__init__(coordinator, "switch", key)
        self._default = info["default"]
        self._attr_entity_category = EntityCategory.CONFIG

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        value = self._default
        if (last := await self.async_get_restored_state()) is not None and last.state in ("on", "off"):
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
