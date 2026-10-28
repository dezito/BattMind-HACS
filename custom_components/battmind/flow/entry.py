"""The config flow (first setup, restore of a backup, import of legacy data)."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from ..management.backup import BACKUP_DIR, async_load_backup, backup_dir, list_backups
from ..const import CONF_ENTITY_PREFIX, DEFAULT_ENTITY_PREFIX, DOMAIN, SENSOR_KEYS, default_options, normalize_language
from ..management.storage import find_legacy_sources, load_legacy_config
from .pages import BattMindFlowPages
from .options import BattMindOptionsFlow
from .schema import CONFIG_FLOW_STEPS, IMPORT_NONE, PREFIX_RE, SELECTORS, options_from_legacy, providers_from_options


class BattMindConfigFlow(BattMindFlowPages, ConfigFlow, domain=DOMAIN):
    """Handle the BattMind config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._prefix = DEFAULT_ENTITY_PREFIX
        self._options: dict[str, Any] = default_options()
        self._initial_settings: dict[str, Any] = {}
        self._legacy_sources: dict[str, list[str]] = {}
        self._legacy_prefix: str | None = None
        self._backups: dict[str, str] = {}
        self._restore_backup: str | None = None
        self._queue: list[str] = []
        self._providers: list[str] = ["energidataservice"]

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return BattMindOptionsFlow()

    def _placeholders(self) -> dict[str, str]:
        return {"prefix": self._prefix}

    def _foreign_entities(self, prefix: str) -> list[str]:
        """Entity ids this prefix would create that already belong to something else (e.g. PyScript)."""
        ent_reg = er.async_get(self.hass)
        found = []
        for key in SENSOR_KEYS:
            entity_id = f"sensor.{prefix}_{key}"
            entry = ent_reg.async_get(entity_id)
            if (entry is not None and entry.platform != DOMAIN) or (entry is None and self.hass.states.get(entity_id)):
                found.append(entity_id)
        return found

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            prefix = user_input[CONF_ENTITY_PREFIX].strip().lower()
            used = {entry.data.get(CONF_ENTITY_PREFIX) for entry in self._async_current_entries()}
            if not PREFIX_RE.match(prefix):
                errors[CONF_ENTITY_PREFIX] = "invalid_prefix"
            elif prefix in used:
                errors[CONF_ENTITY_PREFIX] = "prefix_in_use"
            elif self._foreign_entities(prefix):
                errors[CONF_ENTITY_PREFIX] = "prefix_entities_exist"
            else:
                self._prefix = prefix
                self._options["language"] = user_input.get("language", self._options["language"])
                self._queue = list(CONFIG_FLOW_STEPS)
                self._backups = await self.hass.async_add_executor_job(list_backups, backup_dir(self.hass))
                if self._backups:
                    return await self.async_step_restore_backup()
                return await self._async_legacy_or_next()

        language = normalize_language(self.hass.config.language)
        schema = vol.Schema(
            {
                vol.Required(CONF_ENTITY_PREFIX, default=DEFAULT_ENTITY_PREFIX): selector.TextSelector(),
                vol.Required("language", default=language): SELECTORS["language"],
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def _async_legacy_or_next(self) -> ConfigFlowResult:
        self._legacy_sources = await self.hass.async_add_executor_job(find_legacy_sources, self.hass.config.config_dir)
        if self._legacy_sources:
            return await self.async_step_import_legacy()
        return await self._async_next()

    async def async_step_restore_backup(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask which exported or deleted BattMind entry (if any) should be restored."""
        if user_input is not None:
            name = user_input.get("backup", IMPORT_NONE)
            if name not in self._backups:
                return await self._async_legacy_or_next()
            backup = await async_load_backup(self.hass, name)
            self._restore_backup = name
            language = self._options["language"]
            self._options = {**default_options(), **(backup.get("options") or {}), "language": language}
            self._providers = providers_from_options(self._options)
            return await self._async_next()

        schema = vol.Schema(
            {
                vol.Required("backup", default=next(iter(self._backups))): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            *(selector.SelectOptionDict(value=name, label=label) for name, label in self._backups.items()),
                            selector.SelectOptionDict(value=IMPORT_NONE, label=IMPORT_NONE),
                        ],
                        mode=selector.SelectSelectorMode.LIST,
                        translation_key="restore_backup",
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="restore_backup",
            data_schema=schema,
            description_placeholders={"prefix": self._prefix, "folder": BACKUP_DIR},
        )

    async def async_step_import_legacy(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask which PyScript installation (if any) should be imported."""
        if user_input is not None:
            source = user_input.get("import_source", IMPORT_NONE)
            if source in self._legacy_sources:
                self._legacy_prefix = source
                language = self._options["language"]
                legacy = await self.hass.async_add_executor_job(load_legacy_config, self.hass.config.config_dir, source)
                if legacy:
                    self._options, self._initial_settings = options_from_legacy(legacy)
                    self._options["language"] = language
                    self._providers = providers_from_options(self._options)
                # PyScript used the dashboard input_number, the yaml value was only its initial value
                if (state := self.hass.states.get(f"input_number.{source}_solar_sell_fixed_price")) is not None:
                    try:
                        self._options["production_price"] = float(state.state)
                    except ValueError:
                        pass
            return await self._async_next()

        sources = list(self._legacy_sources)
        default = self._prefix if self._prefix in sources else sources[0]
        schema = vol.Schema(
            {
                vol.Required("import_source", default=default): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[*sources, IMPORT_NONE],
                        mode=selector.SelectSelectorMode.LIST,
                        translation_key="import_source",
                    )
                )
            }
        )
        files = "\n".join(f"- **{source}**: {', '.join(names)}" for source, names in self._legacy_sources.items())
        return self.async_show_form(
            step_id="import_legacy",
            data_schema=schema,
            description_placeholders={"prefix": self._prefix, "files": files},
        )

    async def _async_finish(self) -> ConfigFlowResult:
        title = "BattMind" if self._prefix == DEFAULT_ENTITY_PREFIX else f"BattMind ({self._prefix})"
        return self.async_create_entry(
            title=title,
            data={
                CONF_ENTITY_PREFIX: self._prefix,
                "legacy_prefix": self._legacy_prefix,
                "initial_settings": self._initial_settings,
                "restore_backup": self._restore_backup,
            },
            options=self._options,
        )
