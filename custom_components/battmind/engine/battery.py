"""Battery level, charging loss and efficiency."""
from __future__ import annotations

import logging
import math

from ..utils.efficiency import EfficiencyCurve, efficiency_at_power, parse_efficiency_curve
from ..utils.helpers import float_or_none

_LOGGER = logging.getLogger(__name__)


class Battery:
    """Battery level, charging loss and efficiency."""

    def get_battery_level(self) -> float:
        entity_id = self.config["home"]["entity_ids"]["powerwall_battery_level_entity_id"]
        value = float_or_none(self.entity_state(entity_id))
        if value is None and self._wc is not None:
            value = self._wc.last_numeric(entity_id)
        if value is None:
            _LOGGER.warning("Cant get the battery level from %s, using 100", entity_id)
            return 100.0
        return value

    def kwh_to_percentage(self, kwh: float) -> float:
        return kwh / self.config["solar"]["powerwall_battery_size"] * 100

    def percentage_to_kwh(self, percentage: float) -> float:
        return percentage * self.config["solar"]["powerwall_battery_size"] / 100

    def kwh_needed_for_charging(self, target_level=None, battery=None) -> float:
        target_level = self.config["solar"]["powerwall_battery_level_min"] if target_level is None else target_level
        battery = self.get_battery_level() if battery is None else battery
        return max(round(self.percentage_to_kwh(target_level - battery), 2), 0.0)

    def get_charging_loss(self) -> float:
        """Round trip loss: configured, or measured after one battery capacity of charging."""
        if "charging_loss" in self._job_cache:
            return self._job_cache["charging_loss"]
        solar = self.config["solar"]
        loss = solar["powerwall_charge_discharge_loss"]
        charged_kwh = self.charging_history_total.get("charged_kwh", 0.0)
        if solar["use_charge_discharge_loss_from_history"] and charged_kwh >= solar["powerwall_battery_size"]:
            in_battery = self.percentage_to_kwh(self.get_battery_level() - solar["powerwall_battery_level_min"])
            discharged_kwh = self.charging_history_total.get("discharged_kwh", 0.0) + in_battery
            loss = 1 - discharged_kwh / charged_kwh
        self._job_cache["charging_loss"] = loss
        return loss

    def _fallback_efficiency(self) -> float:
        return math.sqrt(1.0 - min(abs(self.get_charging_loss()), 0.9))

    def charging_efficiency_curve(self) -> EfficiencyCurve:
        """Configured charging curve, or none while enhanced efficiency is disabled."""
        if not self.enabled("enhance_battery_efficiency"):
            return ()
        return parse_efficiency_curve(self.config["solar"].get("battery_charging_efficiency_curve"))

    def discharging_efficiency_curve(self) -> EfficiencyCurve:
        """Configured discharging curve, or none while enhanced efficiency is disabled."""
        if not self.enabled("enhance_battery_efficiency"):
            return ()
        return parse_efficiency_curve(self.config["solar"].get("battery_discharging_efficiency_curve"))

    def charging_efficiency(self, power_kw: float | None = None) -> float:
        """Charging efficiency at AC power, falling back to half the round-trip loss."""
        power_kw = self.max_kwh_charging if power_kw is None else power_kw
        return efficiency_at_power(self.charging_efficiency_curve(), power_kw * 1000.0, self._fallback_efficiency())

    def discharging_efficiency(self, power_kw: float | None = None) -> float:
        """Discharging efficiency at AC power, falling back to half the round-trip loss."""
        power_kw = abs(self.max_kwh_discharging) if power_kw is None else power_kw
        return efficiency_at_power(self.discharging_efficiency_curve(), power_kw * 1000.0, self._fallback_efficiency())

    def battery_efficiency(self) -> float:
        """Equivalent one-way efficiency used by the legacy rule planner."""
        return math.sqrt(1.0 - self.effective_round_trip_loss())

    def effective_round_trip_loss(self) -> float:
        """Round-trip loss at configured maximum power, including optional curves."""
        return 1.0 - self.charging_efficiency() * self.discharging_efficiency()

    def calc_battery_loss_cost(self, price: float) -> float:
        """Loss part of the price of 1 kWh out of the battery: it costs price / (1 - round trip loss)."""
        loss = self.effective_round_trip_loss()
        return price * loss / (1.0 - loss)
