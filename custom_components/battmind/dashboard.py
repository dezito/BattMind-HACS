"""Generated sidebar dashboard: the bundled template mapped onto one BattMind instance.

The template (dashboard/battmind.yaml) is written for the prefix ``battmind`` and the
author's price/solar entities. At setup it is rewritten for this config entry:

* own entities ``<platform>.battmind_*`` -> ``<platform>.<prefix>_*``
* external entities (Energi Data Service, Strømligning, Solcast) -> the configured ones
* cards bound to an external entity that is not configured are removed
* ``@@key@@`` placeholders -> ``dashboard.key`` of texts/<language>.json (BattMind's language),
  ``@@forecast_span@@`` -> days_to_predict - 0.5 days, ``@@valuta@@`` / ``@@valuta_kwh@@`` -> the
  currency of Home Assistant

The result is written to .storage and served as a Lovelace dashboard that can be edited in
the UI: once the user saves it, the saved copy is shown instead of the generated one until
it is reset (reset_dashboard button, or deleting the config in the raw configuration editor).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from homeassistant.components import frontend
from homeassistant.components.lovelace import dashboard as lovelace_dashboard
from homeassistant.components.lovelace.const import LOVELACE_DATA, MODE_STORAGE
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.json import cached_json_fragment, json_fragment
from homeassistant.helpers.storage import Store

from .const import DOMAIN, normalize_language
from .formatting import load_texts

_LOGGER = logging.getLogger(__name__)

TEMPLATE = Path(__file__).parent / "dashboard" / "battmind.yaml"
ICON = "mdi:home-battery"

_OWN_ENTITY = re.compile(r"\b(sensor|binary_sensor|switch|number|select|text|button)\.battmind_")
_PLACEHOLDER = re.compile(r"@@(\w+)@@")
_SOLCAST_DAYS = ("tomorrow", "day_3", "day_4", "day_5", "day_6", "day_7")
_DROP = object()


def dashboard_url(prefix: str) -> str:
    """Frontend url of the dashboard (Lovelace requires a '-' in it)."""
    return f"{prefix.replace('_', '-')}-dashboard"


def dashboard_file(hass: HomeAssistant, entry_id: str) -> Path:
    return Path(hass.config.path(".storage", f"{DOMAIN}.{entry_id}.dashboard.yaml"))


def custom_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """The user's edited copy of the dashboard."""
    return Store(hass, 1, f"{DOMAIN}.{entry_id}.dashboard_custom")


class BattMindDashboard(lovelace_dashboard.LovelaceYAML):
    """The generated YAML file, but editable: the frontend only offers editing in storage mode."""

    def __init__(self, hass: HomeAssistant, url_path: str, config: dict[str, Any], store: Store[dict[str, Any]], custom: dict[str, Any] | None) -> None:
        super().__init__(hass, url_path, config)
        self._store = store
        self._custom = custom
        self._custom_json: json_fragment | None = None

    @property
    def mode(self) -> str:
        return MODE_STORAGE

    @property
    def customized(self) -> bool:
        return self._custom is not None

    async def async_get_info(self) -> dict[str, Any]:
        info = await super().async_get_info()
        return {**info, "mode": self.mode}

    async def async_load(self, force: bool) -> dict[str, Any]:
        if self._custom is not None:
            return self._custom
        return await super().async_load(force)

    async def async_json(self, force: bool) -> json_fragment:
        if self._custom is not None:
            if self._custom_json is None:
                self._custom_json = cached_json_fragment(self._custom)
            return self._custom_json
        return await super().async_json(force)

    async def async_save(self, config: dict[str, Any]) -> None:
        self._custom = config
        self._custom_json = None
        await self._store.async_save({"config": config})
        self._config_updated()

    async def async_delete(self) -> None:
        """Back to the generated dashboard."""
        self._custom = None
        self._custom_json = None
        await self._store.async_remove()
        self._config_updated()


def _derive(entity_id: str, old: str, new: str) -> str:
    return entity_id.replace(old, new) if old in entity_id else ""


def external_entities(config: dict[str, Any]) -> dict[str, str]:
    """Template entity id -> configured entity id ("" = not configured)."""
    prices = config["prices"]["entity_ids"]
    sl_buy = prices["stromligning_buy_current_price"]
    sl_sell = prices["stromligning_sell_current_price"]
    mapping = {
        "sensor.energi_data_service": prices["energidataservice_buy_entity_id"],
        "sensor.energi_data_service_salg": prices["energidataservice_sell_entity_id"],
        "sensor.stromligning_current_price_vat": sl_buy,
        "sensor.stromligning_forecasts_vat": prices["stromligning_buy_forecasts"],
        "binary_sensor.stromligning_tomorrow_available_vat": prices["stromligning_buy_tomorrow_available"],
        "sensor.stromligning_nettariff_vat": _derive(sl_buy, "current_price", "nettariff"),
        "sensor.stromligning_systemtariff_vat": _derive(sl_buy, "current_price", "systemtariff"),
        "sensor.stromligning_15min_spotprice_ex_vat": _derive(sl_sell, "current_price", "spotprice"),
        "sensor.stromligning_15min_forecasts_ex_vat": prices["stromligning_sell_forecasts"],
        "binary_sensor.stromligning_15min_tomorrow_available_ex_vat": prices["stromligning_sell_tomorrow_available"],
    }
    if sl_buy:
        # one price chart: Strømligning is preferred, the Energi Data Service chart is removed
        mapping["sensor.energi_data_service"] = ""
        mapping["sensor.energi_data_service_salg"] = ""
    # Solcast: forecast_today plus the per-day sensors next to it
    forecast = config["solar"]["entity_ids"]["forecast_entity_id"]
    base = forecast[: -len("today")] if forecast.endswith("_today") else ""
    mapping["sensor.solcast_pv_forecast_forecast_today"] = forecast
    for day in _SOLCAST_DAYS:
        mapping[f"sensor.solcast_pv_forecast_forecast_{day}"] = f"{base}{day}" if base else ""
    return mapping


DEBUG_BUTTON_SWITCH = "input_boolean.dezito_show_debug_button"


def _prune(node: Any, missing: set[str]) -> Any:
    """Remove cards/series/list items bound to a missing entity, and containers left empty."""
    if isinstance(node, list):
        items = []
        for item in node:
            if isinstance(item, str) and item in missing:
                continue
            item = _prune(item, missing)
            if item is not _DROP:
                items.append(item)
        return items
    if isinstance(node, dict):
        if node.get("entity") in missing:
            return _DROP
        # (also visibility: without its entity the card must not end up always visible)
        if any(isinstance(cond, dict) and cond.get("entity") in missing for key in ("conditions", "visibility") for cond in node.get(key) or ()):
            return _DROP
        result = {}
        for key, value in node.items():
            pruned = _prune(value, missing)
            if pruned is _DROP:  # e.g. the single `card:` of a conditional
                return _DROP
            if isinstance(value, list) and value and not pruned and key in ("cards", "series", "entities", "sections"):
                return _DROP
            result[key] = pruned
        return result
    return node


def _translate(node: Any, texts: dict[str, str]) -> Any:
    """Replace @@key@@ in every string with the dashboard text of the language (the key if missing)."""
    if isinstance(node, list):
        return [_translate(item, texts) for item in node]
    if isinstance(node, dict):
        return {key: _translate(value, texts) for key, value in node.items()}
    if isinstance(node, str) and "@@" in node:
        return _PLACEHOLDER.sub(lambda match: texts.get(f"dashboard.{match.group(1)}", match.group(1)), node)
    return node


class _Dumper(yaml.SafeDumper):
    """Keep the JavaScript templates readable: multi-line strings as literal blocks."""


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _represent_str)


def render_dashboard(
    config: dict[str, Any], prefix: str, existing: set[str] | None = None, currency: str | None = None
) -> str:
    """Build the dashboard YAML for one instance (blocking file I/O).

    existing: entity ids that exist in Home Assistant; the debug button is removed when its switch is not among them
    currency: hass.config.currency (None = the currency of the language's texts)
    """
    mapping = external_entities(config)
    template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    texts = load_texts(normalize_language(config.get("language")), currency)
    # the charts show the forecast period: days_to_predict minus the half day already passed
    texts["dashboard.forecast_span"] = f"{max(float(config['days_to_predict']) - 0.5, 0.5):g}d"
    template = _translate(template, texts)
    missing = {source for source, target in mapping.items() if not target}
    if existing is not None and DEBUG_BUTTON_SWITCH not in existing:
        missing.add(DEBUG_BUTTON_SWITCH)
    content = yaml.dump(_prune(template, missing), Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=1000)

    replace = {source: target for source, target in mapping.items() if target and source != target}
    if replace:
        pattern = re.compile(r"(?<![\w.])(" + "|".join(map(re.escape, sorted(replace, key=len, reverse=True))) + r")(?!\w)")
        content = pattern.sub(lambda match: replace[match.group(1)], content)
    if prefix != "battmind":
        content = _OWN_ENTITY.sub(lambda match: f"{match.group(1)}.{prefix}_", content)
    return content


def _write_if_changed(path: Path, content: str) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


async def async_setup_dashboard(hass: HomeAssistant, entry: ConfigEntry, config: dict[str, Any], prefix: str) -> None:
    """Render the dashboard and add it to the sidebar until the entry unloads."""
    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None:
        _LOGGER.warning("Lovelace is not loaded, the BattMind dashboard is not available")
        return
    url_path = dashboard_url(prefix)
    existing = lovelace.dashboards.get(url_path)
    if existing is not None and not isinstance(existing, BattMindDashboard):
        _LOGGER.warning("A dashboard already uses /%s, the BattMind dashboard is not added", url_path)
        return

    path = dashboard_file(hass, entry.entry_id)

    existing_ids = set(hass.states.async_entity_ids())

    def _render() -> bool:
        return _write_if_changed(path, render_dashboard(config, prefix, existing_ids, hass.config.currency))

    try:
        changed = await hass.async_add_executor_job(_render)
    except (OSError, yaml.YAMLError) as err:
        _LOGGER.error("Could not create the BattMind dashboard: %s", err)
        return

    store = custom_store(hass, entry.entry_id)
    stored = await store.async_load()
    title = "BattMind" if prefix == "battmind" else f"BattMind ({prefix})"
    battmind_dashboard = BattMindDashboard(
        hass,
        url_path,
        {"mode": MODE_STORAGE, "filename": str(path), "title": title, "icon": ICON, "show_in_sidebar": True, "require_admin": False},
        store,
        stored["config"] if stored else None,
    )
    lovelace.dashboards[url_path] = battmind_dashboard
    frontend.async_register_built_in_panel(
        hass,
        "lovelace",
        frontend_url_path=url_path,
        sidebar_title=title,
        sidebar_icon=ICON,
        require_admin=False,
        config={"mode": MODE_STORAGE},
        update=existing is not None,
    )
    _LOGGER.info("Dashboard available at /%s%s", url_path, " (edited in the UI)" if battmind_dashboard.customized else " (updated)" if changed else "")

    @callback
    def _remove() -> None:
        if lovelace.dashboards.get(url_path) is battmind_dashboard:
            lovelace.dashboards.pop(url_path)
            frontend.async_remove_panel(hass, url_path)

    entry.async_on_unload(_remove)


async def async_reset_dashboard(hass: HomeAssistant, prefix: str) -> None:
    """Drop the user's edits: the generated dashboard is shown again."""
    lovelace = hass.data.get(LOVELACE_DATA)
    dashboard = lovelace.dashboards.get(dashboard_url(prefix)) if lovelace else None
    if isinstance(dashboard, BattMindDashboard):
        await dashboard.async_delete()


async def async_remove_dashboard_file(hass: HomeAssistant, entry_id: str) -> None:
    await hass.async_add_executor_job(dashboard_file(hass, entry_id).unlink, True)
    await custom_store(hass, entry_id).async_remove()
