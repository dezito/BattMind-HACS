"""Pages shared by the config flow and the options flow."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers import selector

from ..const import FLOW_STEPS, PRICE_PROVIDER_KEYS
from .schema import ENERGIDATASERVICE_URL, PRICE_PROVIDERS, build_schema, normalize_input, validate_step


class BattMindFlowPages:
    """Pages shared by the config flow and the options flow.

    The subclass sets ``self._options`` (flat option dict), ``self._queue`` (remaining
    page ids) and implements ``_async_finish``.
    """

    _options: dict[str, Any]
    _queue: list[str]
    _providers: list[str]

    def _page_enabled(self, step: str) -> bool:
        if step == "prices_energidataservice":
            return "energidataservice" in self._providers
        if step == "prices_stromligning":
            return "stromligning" in self._providers
        if step == "prices_stromligning_tariffs":
            return "stromligning" in self._providers and bool(self._options.get("stromligning_sell_current_price"))
        if step == "weather":
            return bool(self._options.get("production_entity_id"))
        return True

    async def _async_next(self) -> ConfigFlowResult:
        while self._queue:
            step = self._queue.pop(0)
            if self._page_enabled(step):
                return await getattr(self, f"async_step_{step}")()
        return await self._async_finish()

    async def _async_finish(self) -> ConfigFlowResult:
        raise NotImplementedError

    def _placeholders(self) -> dict[str, str]:
        return {}

    async def _async_page(self, step: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        keys = FLOW_STEPS[step]
        errors: dict[str, str] = {}
        values_for_form = dict(self._options)
        if user_input is not None:
            values = normalize_input(keys, user_input)
            errors = validate_step(step, values)
            if not errors:
                self._options.update(values)
                return await self._async_next()
            values_for_form.update(values)
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id=step,
            data_schema=build_schema(keys, values_for_form, self.hass.config.currency),  # type: ignore[attr-defined]
            errors=errors,
            description_placeholders={**self._placeholders(), "eds_url": ENERGIDATASERVICE_URL},
        )

    async def async_step_price_provider(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            providers = [p for p in user_input.get("price_providers", []) if p in PRICE_PROVIDERS]
            if not providers:
                errors["base"] = "no_price_provider"
            else:
                self._providers = providers
                for provider, keys in PRICE_PROVIDER_KEYS.items():
                    if provider not in providers:
                        for key in keys:
                            self._options[key] = ""
                return await self._async_next()
        schema = vol.Schema(
            {
                vol.Required("price_providers", default=self._providers): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=PRICE_PROVIDERS,
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                        translation_key="price_providers",
                    )
                )
            }
        )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="price_provider", data_schema=schema, errors=errors, description_placeholders=self._placeholders()
        )

    async def async_step_general(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("general", user_input)

    async def async_step_battery_entities(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("battery_entities", user_input)

    async def async_step_battery_specs(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("battery_specs", user_input)

    async def async_step_consumption(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("consumption", user_input)

    async def async_step_prices_energidataservice(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("prices_energidataservice", user_input)

    async def async_step_prices_stromligning(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("prices_stromligning", user_input)

    async def async_step_prices_stromligning_tariffs(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("prices_stromligning_tariffs", user_input)

    async def async_step_price_fees(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("price_fees", user_input)

    async def async_step_solar(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("solar", user_input)

    async def async_step_weather(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("weather", user_input)

    async def async_step_advanced(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("advanced", user_input)

    async def async_step_database(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("database", user_input)
