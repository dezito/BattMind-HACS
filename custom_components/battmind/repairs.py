"""Repair flow: replace an installed blueprint with the version bundled with BattMind."""
from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant

from .management.blueprint_install import async_update_blueprint


class BlueprintUpdateFlow(ConfirmRepairFlow):
    def __init__(self, name: str) -> None:
        self._name = name

    async def async_step_confirm(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        if user_input is not None:
            await async_update_blueprint(self.hass, self._name)
        return await super().async_step_confirm(user_input)


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None) -> RepairsFlow:
    return BlueprintUpdateFlow(data["file"])
