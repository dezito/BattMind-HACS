"""Base entity for BattMind."""
from __future__ import annotations

from typing import Any

from homeassistant.core import State, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, MANUFACTURER
from .coordinator import BattMindCoordinator

# Platform -> helper domain used by the PyScript version (values are migrated once)
LEGACY_DOMAINS = {
    "switch": "input_boolean",
    "number": "input_number",
    "select": "input_select",
    "text": "input_text",
    "sensor": "input_number",
}


class BattMindEntity(Entity):
    """Common BattMind entity."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: BattMindCoordinator, platform: str, key: str) -> None:
        self.coordinator = coordinator
        self._key = key
        self._platform_domain = platform
        prefix = coordinator.prefix
        self.entity_id = f"{platform}.{prefix}_{key}"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        # name from translations/<language>.json and icon from icons.json (entity.<platform>.<key>)
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name="BattMind" if prefix == "battmind" else f"BattMind ({prefix})",
            manufacturer=MANUFACTURER,
            model="BattMind",
            entry_type=DeviceEntryType.SERVICE,
        )

    def legacy_state(self) -> State | None:
        """State of the matching PyScript helper (input_boolean.battmind_x, ...)."""
        domain = LEGACY_DOMAINS.get(self._platform_domain)
        legacy_prefix = self.coordinator.legacy_prefix
        if domain is None or legacy_prefix is None:
            return None
        state = self.hass.states.get(f"{domain}.{legacy_prefix}_{self._key}")
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        return state

    async def async_get_restored_state(self) -> State | None:
        """The value of an imported backup (backup.py) before the entity's own last state."""
        if (value := self.coordinator.restore_settings.get(f"{self._platform_domain}.{self._key}")) is not None:
            return State(self.entity_id, value)
        return await self.async_get_last_state()  # type: ignore[attr-defined]


class BattMindUpdatingEntity(BattMindEntity):
    """Entity that writes its state when the coordinator publishes its key."""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, self.coordinator.signal, self._handle_update))

    @callback
    def _handle_update(self, key: Any) -> None:
        if key == self._key:
            self.async_write_ha_state()
