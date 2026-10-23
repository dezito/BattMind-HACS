"""BattMind buttons (replaces the input_button helpers)."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import BattMindCoordinator
from .entity import BattMindEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: BattMindCoordinator = entry.runtime_data
    async_add_entities(
        [
            BattMindEnforcePlanningButton(coordinator, "button", "enforce_planning"),
            BattMindRestartButton(coordinator, "button", "restart_script"),
        ]
    )


class BattMindEnforcePlanningButton(BattMindEntity, ButtonEntity):
    """Recalculate the charging plan now, ignoring the unchanged-input shortcut."""

    async def async_press(self) -> None:
        self.coordinator.async_request_charge(force=True)


class BattMindRestartButton(BattMindEntity, ButtonEntity):
    """Reload the BattMind config entry."""

    async def async_press(self) -> None:
        self.hass.config_entries.async_schedule_reload(self.coordinator.entry.entry_id)
