"""Debug information of the engine."""
from __future__ import annotations

import datetime
from pprint import pformat

from ..utils.codec import encode


class Diagnostics:
    """Debug information of the engine."""

    @classmethod
    def _plain(cls, value):
        """Datetimes to strings so pformat output is readable."""
        if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
            return value.isoformat()
        if isinstance(value, dict):
            return {cls._plain(k): cls._plain(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._plain(v) for v in value]
        return value

    def debug_info(self, extra: dict | None = None) -> str:
        extra = extra or {}
        sections = {
            "Status": (
                {
                    "testing": self.testing,
                    "solar_configured": self.is_solar_configured(),
                    "forecast_type": self.forecast_type,
                    "error_count": self.error_count,
                    "last_plan_hash": self.last_plan_hash,
                    "history_footer_bytes": self.history_footer_bytes,
                    "integration_offline_since": self.integration_offline_since,
                    **extra.get("status", {}),
                },
                {"config": self.config},
            ),
            "Worker": (extra.get("worker", {}), None),
            "Charging plan & prices": (
                None,
                {
                    "plan": self.plan,
                    "charge_hours": self.charge_hours,
                    "grid_price_cache": self.grid_price_cache,
                    "sell_price_cache": self.sell_price_cache,
                },
            ),
            "Battery cost": (None, {"battery_expenses": self.battery_expenses, "powerwall_price_details": self.powerwall_price_details}),
            "Solar prediction": (None, {"solar_prediction_db": self.solar_prediction_db}),
            "Databases": (None, {"power_values_db": self.power_values_db, "solar_available_db": self.solar_available_db}),
            "Session rules": (None, {"session_rules": self.session_rules}),
        }
        lines = ["<center>\n", f"#### Debug info runtime {self.now()} ####\n", "</center>\n"]
        for section, (table, details) in sections.items():
            lines.append(f"<center>\n\n### {section}\n</center>\n")
            if table:
                lines += ["| Variable | Value |", "|---:|:---|"]
                lines += [f"| {key}: | {self._plain(value)} |" for key, value in table.items()]
            for key, value in (details or {}).items():
                lines += ["<details>", f"<summary>{key}: Show dictionary</summary>\n", f"```\n{pformat(self._plain(value))}\n```", "</details>\n"]
            lines.append("---")
        if self.overview_history:
            lines.append("<center>\n\n### Overview History\n</center>\n")
            for title, overview in sorted(self.overview_history.items(), reverse=True):
                lines += ["<details>", f"<summary>{title}: Show snapshot</summary>\n", *overview, "</details>\n"]
        output = "\n".join(lines)
        self.notify(output, title=f"{self.title} debug info", notification_id=f"{self.name}_debug_info")
        return output

    def debug_state(self) -> dict:
        """The runtime state for the debug export (management/debug_export.py), encoded like the databases."""
        skip = {"config", "texts", "price_provider"}
        return {key: encode(value) for key, value in vars(self).items() if not key.startswith("_") and key not in skip}
