"""Export and import of a BattMind entry (databases, settings and configuration).

A backup is one JSON file in <config>/battmind_backups. It is written by the export_data
service/button and automatically when an entry is deleted, and it is restored by the
import_data service, the options flow or (for a new entry) the config flow.

The settings are stored as entity states (the strings the entities restore from), so the
entities validate an imported value exactly like their own last state.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.json import save_json
from homeassistant.helpers.restore_state import async_get as async_get_restore_data
from homeassistant.util import dt as dt_util
from homeassistant.util.json import load_json

from .const import CONF_ENTITY_PREFIX, DOMAIN
from .storage import DB_NAMES, BattMindStorage

_LOGGER = logging.getLogger(__name__)

BACKUP_DIR = f"{DOMAIN}_backups"
BACKUP_FORMAT = 1
# entities whose state is part of the backup: the settings and the accumulated solar kWh
SETTINGS_PLATFORMS = ("number", "switch", "select", "text")
EXTRA_SETTINGS = ("sensor.kwh_charged_by_solar",)
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.json$")


def backup_dir(hass: HomeAssistant) -> str:
    return hass.config.path(BACKUP_DIR)


def _collect_settings(hass: HomeAssistant, entry_id: str) -> dict[str, str]:
    """platform.key -> state of the entry's settings entities (last stored state when unloaded)."""
    ent_reg = er.async_get(hass)
    last_states = async_get_restore_data(hass).last_states
    settings: dict[str, str] = {}
    for reg_entry in er.async_entries_for_config_entry(ent_reg, entry_id):
        key = f"{reg_entry.domain}.{reg_entry.unique_id.removeprefix(f'{entry_id}_')}"
        if reg_entry.domain not in SETTINGS_PLATFORMS and key not in EXTRA_SETTINGS:
            continue
        state = hass.states.get(reg_entry.entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            stored = last_states.get(reg_entry.entity_id)
            state = stored.state if stored else None
        if state is not None and state.state not in ("unknown", "unavailable"):
            settings[key] = state.state
    return settings


async def async_export(hass: HomeAssistant, entry: ConfigEntry, reason: str = "export") -> str:
    """Write the backup of entry, returns the file name (in backup_dir)."""
    if entry.state is ConfigEntryState.LOADED:
        storage = entry.runtime_data.storage
    else:
        storage = BattMindStorage(hass, entry.entry_id)
    now = dt_util.now()
    prefix = entry.data.get(CONF_ENTITY_PREFIX, DOMAIN)
    backup = {
        "integration": DOMAIN,
        "format": BACKUP_FORMAT,
        "created": now.isoformat(timespec="seconds"),
        "reason": reason,
        "title": entry.title,
        CONF_ENTITY_PREFIX: prefix,
        "options": dict(entry.options),
        "settings": _collect_settings(hass, entry.entry_id),
        "databases": await storage.async_load_raw(),
    }
    suffix = "" if reason == "export" else f"_{reason}"
    name = f"{prefix}_{now.strftime('%Y%m%d_%H%M%S')}{suffix}.json"
    folder = backup_dir(hass)

    def _write() -> None:
        os.makedirs(folder, exist_ok=True)
        save_json(os.path.join(folder, name), backup, atomic_writes=True)

    await hass.async_add_executor_job(_write)
    _LOGGER.info("Exported BattMind %s to %s", prefix, os.path.join(BACKUP_DIR, name))
    return name


def list_backups(folder: str) -> dict[str, str]:
    """File name -> label of the BattMind backups, newest first (blocking)."""
    if not os.path.isdir(folder):
        return {}
    found = []
    for name in os.listdir(folder):
        if not _NAME_RE.match(name):
            continue
        try:
            backup = load_backup(folder, name)
        except HomeAssistantError:
            continue
        databases = ", ".join(sorted(backup.get("databases") or {})) or "-"
        label = f"{backup.get('title', name)} - {backup.get('created', '?')[:16].replace('T', ' ')} ({databases})"
        found.append((backup.get("created", ""), name, label))
    return {name: label for _created, name, label in sorted(found, reverse=True)}


def load_backup(folder: str, name: str) -> dict[str, Any]:
    """Read and check a backup file (blocking)."""
    if not _NAME_RE.match(name):
        raise HomeAssistantError(f"Invalid backup file name {name!r}")
    path = os.path.join(folder, name)
    if not os.path.isfile(path):
        raise HomeAssistantError(f"Backup {os.path.join(BACKUP_DIR, name)} not found")
    try:
        backup = load_json(path)
    except Exception as err:  # noqa: BLE001
        raise HomeAssistantError(f"Could not read backup {name}: {err}") from err
    if not isinstance(backup, dict) or backup.get("integration") != DOMAIN:
        raise HomeAssistantError(f"{name} is not a BattMind backup")
    return backup


async def async_load_backup(hass: HomeAssistant, name: str) -> dict[str, Any]:
    return await hass.async_add_executor_job(load_backup, backup_dir(hass), name)


def pending_restores(hass: HomeAssistant) -> dict[str, str]:
    """entry_id -> backup file, applied by the coordinator on the next setup."""
    return hass.data.setdefault(DOMAIN, {}).setdefault("pending_restore", {})


async def async_restore_databases(storage: BattMindStorage, backup: dict[str, Any]) -> list[str]:
    """Write the databases of the backup into the stores, returns the restored names."""
    restored = []
    for name, raw in (backup.get("databases") or {}).items():
        if name in DB_NAMES and raw is not None:
            await storage.async_save(name, raw)
            restored.append(name)
    return sorted(restored)


async def async_import(hass: HomeAssistant, entry: ConfigEntry, name: str, restore_configuration: bool = False) -> None:
    """Replace the data of entry with the backup (the entry is reloaded)."""
    backup = await async_load_backup(hass, name)
    # the current data is kept as well, an import by mistake can be undone
    await async_export(hass, entry, "before_import")
    await hass.config_entries.async_unload(entry.entry_id)
    pending_restores(hass)[entry.entry_id] = name
    if restore_configuration and isinstance(backup.get("options"), dict):
        # while unloaded, so the update listener does not schedule another reload
        hass.config_entries.async_update_entry(entry, options={**entry.options, **backup["options"]})
    await hass.config_entries.async_setup(entry.entry_id)
