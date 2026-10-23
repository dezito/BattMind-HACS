"""Install the bundled automation blueprints into config/blueprints/automation/<domain>/.

Like Home Assistant's own blueprints they are copied at startup, so they show up in
the blueprint list without importing a URL. A newer version (HACS update) replaces the
file only while it is still the version this integration installed; a file the user
changed is left alone.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SOURCE = Path(__file__).parent / "blueprints"
STORE_VERSION = 1


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def async_install_blueprints(hass: HomeAssistant) -> None:
    store: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.blueprints")
    installed: dict[str, str] = await store.async_load() or {}
    target = Path(hass.config.path("blueprints", "automation", DOMAIN))

    def _install() -> dict[str, str]:
        result = dict(installed)
        for source in sorted(SOURCE.glob("*.yaml")):
            content = source.read_text(encoding="utf-8")
            digest = _digest(content)
            destination = target / source.name
            if destination.exists():
                current = _digest(destination.read_text(encoding="utf-8"))
                if current == digest:
                    result[source.name] = digest
                    continue
                if current != installed.get(source.name):
                    _LOGGER.info("Blueprint %s was changed by the user, not updating it", destination)
                    continue
            target.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
            result[source.name] = digest
            _LOGGER.info("Installed blueprint %s", destination)
        return result

    try:
        result = await hass.async_add_executor_job(_install)
    except OSError as err:
        _LOGGER.error("Could not install the BattMind blueprints: %s", err)
        return
    if result != installed:
        await store.async_save(result)
        # the blueprint integration caches the blueprints it has read
        domain_blueprints = hass.data.get("blueprint", {}).get("automation")
        if domain_blueprints is not None and hasattr(domain_blueprints, "async_reset_cache"):
            domain_blueprints.async_reset_cache()
