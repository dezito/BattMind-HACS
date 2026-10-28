"""Install the bundled automation blueprints into config/blueprints/automation/<domain>/.

Like Home Assistant's own blueprints they are copied at startup when they are missing,
so they show up in the blueprint list without importing a URL. An existing file is never
overwritten on its own: when it differs from the bundled version (a HACS update or the
user's own changes) a repair issue lets the user replace it. The files are compared the
way Home Assistant writes a blueprint (validated and dumped again), so re-importing it
from source_url is not a change.
"""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.blueprint import BLUEPRINT_SCHEMA, Blueprint
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util import yaml as yaml_util

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SOURCE = Path(__file__).parent.parent / "blueprints"
ISSUE_PREFIX = "blueprint_update_"


def _normalized(text: str) -> str:
    """The blueprint as Home Assistant saves it when it is re-imported."""
    try:
        data = yaml_util.parse_yaml(text)
        return Blueprint(data, expected_domain="automation", schema=BLUEPRINT_SCHEMA).yaml()
    except HomeAssistantError:
        return text


def _target(hass: HomeAssistant) -> Path:
    return Path(hass.config.path("blueprints", "automation", DOMAIN))


def _issue_id(name: str) -> str:
    return f"{ISSUE_PREFIX}{Path(name).stem}"


async def _async_reload(hass: HomeAssistant) -> None:
    # the blueprint integration caches the blueprints it has read, and automations that are
    # already set up still use the old version - reload them like Home Assistant does after
    # a re-import
    domain_blueprints = hass.data.get("blueprint", {}).get("automation")
    if domain_blueprints is not None:
        await domain_blueprints.async_reset_cache()
        if hass.services.has_service("automation", "reload"):
            await hass.services.async_call("automation", "reload", blocking=False)


async def async_install_blueprints(hass: HomeAssistant) -> None:
    target = _target(hass)

    def _install() -> tuple[list[str], dict[str, str]]:
        installed: list[str] = []
        outdated: dict[str, str] = {}
        for source in sorted(SOURCE.glob("*.yaml")):
            content = source.read_text(encoding="utf-8")
            destination = target / source.name
            if not destination.exists():
                target.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
                installed.append(source.name)
                _LOGGER.info("Installed blueprint %s", destination)
            elif _normalized(destination.read_text(encoding="utf-8")) != _normalized(content):
                outdated[source.name] = yaml_util.parse_yaml(content)["blueprint"]["name"]
        return installed, outdated

    # earlier versions kept the digests of the installed files here
    await Store(hass, 1, f"{DOMAIN}.blueprints").async_remove()
    try:
        installed, outdated = await hass.async_add_executor_job(_install)
    except (OSError, HomeAssistantError) as err:
        _LOGGER.error("Could not install the BattMind blueprints: %s", err)
        return
    for source in SOURCE.glob("*.yaml"):
        if source.name not in outdated:
            ir.async_delete_issue(hass, DOMAIN, _issue_id(source.name))
    for name, title in outdated.items():
        ir.async_create_issue(
            hass,
            DOMAIN,
            _issue_id(name),
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="blueprint_update",
            translation_placeholders={"name": title, "path": f"blueprints/automation/{DOMAIN}/{name}"},
            data={"file": name},
        )
    if installed:
        await _async_reload(hass)


async def async_update_blueprint(hass: HomeAssistant, name: str) -> None:
    """Replace the installed blueprint with the bundled version (repair fix flow)."""
    source = SOURCE / name
    destination = _target(hass) / name

    def _update() -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    await hass.async_add_executor_job(_update)
    _LOGGER.info("Updated blueprint %s", destination)
    await _async_reload(hass)
