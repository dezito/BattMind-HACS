"""Persistent storage for the BattMind databases.

The PyScript version stored its databases as YAML files with datetime and int keys.
The integration uses Home Assistant's Store (JSON in .storage). ``encode``/``decode``
convert the structures losslessly. Existing YAML databases are imported once.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import yaml

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .codec import decode
from .const import DOMAIN, LEGACY_DB_FILES, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

# "analysis": economy and planning quality (engine_analysis.py), no PyScript version
DB_NAMES = (*LEGACY_DB_FILES, "analysis")


class BattMindStorage:
    """One Store per database for a config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self._stores = {
            name: Store(
                hass,
                STORAGE_VERSION,
                f"{DOMAIN}.{entry_id}.{name}",
                private=True,
                atomic_writes=True,
                serialize_in_event_loop=False,
            )
            for name in DB_NAMES
        }

    async def async_load(self) -> dict[str, Any | None]:
        result: dict[str, Any | None] = {}
        for name, store in self._stores.items():
            raw = await store.async_load()
            if raw is None:
                result[name] = None
                continue
            result[name] = await self.hass.async_add_executor_job(decode, raw)
        return result

    async def async_load_raw(self) -> dict[str, Any]:
        """The stored (encoded) databases, for a backup."""
        result: dict[str, Any] = {}
        for name, store in self._stores.items():
            if (raw := await store.async_load()) is not None:
                result[name] = raw
        return result

    async def async_save(self, name: str, encoded: Any) -> None:
        """Save already encoded data (encoding happens in the worker thread)."""
        await self._stores[name].async_save(encoded)

    async def async_remove(self) -> None:
        for store in self._stores.values():
            await store.async_remove()


def find_legacy_files(config_dir: str, prefix: str) -> list[str]:
    """Names of the PyScript files for prefix in the config dir (blocking)."""
    names = [f"{prefix}_config.yaml"] + [template.format(prefix=prefix) for template in LEGACY_DB_FILES.values()]
    return [name for name in names if os.path.isfile(os.path.join(config_dir, name))]


def find_legacy_sources(config_dir: str) -> dict[str, list[str]]:
    """Find every PyScript BattMind installation in the config dir (blocking).

    A source is a <prefix>_config.yaml with BattMind's home battery settings. Configs with
    charger/ev_car sections belong to Cable Juice Planner, which uses the same file naming.
    """
    sources: dict[str, list[str]] = {}
    for name in sorted(os.listdir(config_dir)):
        if not name.endswith("_config.yaml"):
            continue
        prefix = name[: -len("_config.yaml")]
        config = load_legacy_config(config_dir, prefix)
        home_entities = (config.get("home") or {}).get("entity_ids") or {}
        solar = config.get("solar") or {}
        if (
            "powerwall_battery_level_entity_id" not in home_entities
            or "powerwall_battery_level_min" not in solar
            or "charger" in config
            or "ev_car" in config
        ):
            continue
        sources[prefix] = find_legacy_files(config_dir, prefix)
    return sources


def load_legacy_databases(config_dir: str, prefix: str) -> dict[str, Any]:
    """Read the PyScript YAML databases (blocking, run in executor)."""
    result: dict[str, Any] = {}
    for name, template in LEGACY_DB_FILES.items():
        path = os.path.join(config_dir, template.format(prefix=prefix))
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as file:
                data = yaml.safe_load(file)
            if isinstance(data, dict) and data:
                result[name] = data
                _LOGGER.info("Imported legacy BattMind database %s", path)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not import legacy BattMind database %s: %s", path, err)
    return result


def load_legacy_config(config_dir: str, prefix: str) -> dict[str, Any]:
    """Read <prefix>_config.yaml from the PyScript version (blocking)."""
    path = os.path.join(config_dir, f"{prefix}_config.yaml")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as file:
            data = yaml.safe_load(file)
        return data if isinstance(data, dict) else {}
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not read legacy config %s: %s", path, err)
        return {}
