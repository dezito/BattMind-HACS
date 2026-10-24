"""BattMind text settings (replaces input_text.<prefix>_exclude_sell_hours)."""
from __future__ import annotations

import re
from typing import Any

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import TEXT_SETTINGS
from .coordinator import BattMindCoordinator
from .entity import BattMindEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: BattMindCoordinator = entry.runtime_data
    async_add_entities(BattMindText(coordinator, key, info) for key, info in TEXT_SETTINGS.items())


class BattMindText(BattMindEntity, TextEntity, RestoreEntity):
    """A text planner setting stored in the coordinator."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = TextMode.TEXT
    _attr_native_max = 100

    def __init__(self, coordinator: BattMindCoordinator, key: str, info: dict[str, Any]) -> None:
        super().__init__(coordinator, "text", key)
        self._attr_pattern = info["pattern"]
        self._default = info["default"]

    def _valid(self, value: str | None) -> str | None:
        if value is None:
            return None
        if value in ("unknown", "unavailable"):
            return ""
        return value if re.fullmatch(self._attr_pattern, value) else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        value = None
        if (last := await self.async_get_restored_state()) is not None:
            value = self._valid(last.state)
        if value is None and (legacy := self.legacy_state()) is not None:
            value = self._valid(legacy.state)
        self.coordinator.async_set_setting(self._key, value if value is not None else self._default, initial=True)

    @property
    def native_value(self) -> str | None:
        return self.coordinator.settings.get(self._key, self._default)

    async def async_set_value(self, value: str) -> None:
        self.coordinator.async_set_setting(self._key, value.replace(" ", ""))
        self.async_write_ha_state()
