"""BattMind buttons (replaces the input_button helpers)."""
from __future__ import annotations

from homeassistant.components import persistent_notification
from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .backup import BACKUP_DIR, async_export
from .const import DOMAIN
from .coordinator import BattMindCoordinator
from .dashboard import async_reset_dashboard
from .entity import BattMindEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: BattMindCoordinator = entry.runtime_data
    # removed button: an integration is reloaded from Settings > Devices & services
    registry = er.async_get(hass)
    if entity_id := registry.async_get_entity_id("button", DOMAIN, f"{entry.entry_id}_restart_script"):
        registry.async_remove(entity_id)
    entities = [
        BattMindEnforcePlanningButton(coordinator, "button", "enforce_planning"),
        BattMindExportButton(coordinator, "button", "export_data"),
    ]
    if coordinator.config.get("dashboard"):
        entities.append(BattMindResetDashboardButton(coordinator, "button", "reset_dashboard"))
    async_add_entities(entities)


class BattMindEnforcePlanningButton(BattMindEntity, ButtonEntity):
    """Recalculate the charging plan now, ignoring the unchanged-input shortcut."""

    async def async_press(self) -> None:
        self.coordinator.async_request_charge(force=True)


class BattMindExportButton(BattMindEntity, ButtonEntity):
    """Write a backup of the databases and settings to battmind_backups."""

    _attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        name = await async_export(self.hass, self.coordinator.entry)
        prefix = self.coordinator.prefix
        persistent_notification.async_create(
            self.hass,
            self.coordinator.t("ui.backup.exported", file=f"{BACKUP_DIR}/{name}"),
            title=f"📟BattMind ({prefix})",
            notification_id=f"{prefix}_backup",
        )


class BattMindResetDashboardButton(BattMindEntity, ButtonEntity):
    """Throw away the edits made to the sidebar dashboard in the UI."""

    _attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        await async_reset_dashboard(self.hass, self.coordinator.prefix)
