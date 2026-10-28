"""Load, save and recalculate the charging history."""
from __future__ import annotations

import datetime
import logging
from copy import deepcopy

from ...utils.helpers import months_between, reset_time_to_hour

_LOGGER = logging.getLogger(__name__)


class HistoryStorage:
    """Load, save and recalculate the charging history."""

    def load_charging_history(self, database) -> None:
        database = database if isinstance(database, dict) else {}
        self.charging_history_db = {key: deepcopy(value) for key, value in database.items() if isinstance(key, datetime.datetime)}
        self.set_state("charging_history", self.t("ui.load_charging_history.default_state", name=self.name))
        # first pass only measures the size of the totals table at the bottom (see history_footer_bytes)
        self.charging_history_combine_and_set(get_ending_byte_size=True)
        self.charging_history_combine_and_set()

    def save_charging_history(self) -> None:
        if not self.charging_history_db:
            return
        now = self.now()
        for key in list(self.charging_history_db.keys()):
            if months_between(key, now) > self.config["database"]["charging_history_db_data_to_save"] + 1:
                _LOGGER.info("Removing %s from charging_history_db", key)
                del self.charging_history_db[key]
        self.charging_history_db = dict(sorted(self.charging_history_db.items(), key=lambda item: item[0], reverse=True))
        self.mark_dirty("charging_history")

    def recalc_charging_history_today(self) -> int:
        now_hour = self.now().hour
        timestamp = reset_time_to_hour(self.now())
        for ts in list(self.charging_history_db.keys()):
            if isinstance(ts, datetime.datetime) and ts.date() == timestamp.date():
                self.charging_history_db.pop(ts, None)
        failures = 0
        for hour in range(0, now_hour + 1):
            try:
                self.charging_history(timestamp=timestamp.replace(hour=hour), save_db=hour == now_hour)
            except Exception as err:  # noqa: BLE001
                failures += 1
                _LOGGER.error("Charging history recalculation for hour %s failed: %s", hour, err)
        if failures:
            self.notify(
                f"⚠️ Finished {self.title} recalculation of today's charging history with {failures} failures. Check logs for details.",
                title=f"{self.title} recalculation complete with errors",
                notification_id=f"{self.name}_recalc_charging_history_today",
            )
        else:
            self.set_charging_rule("📟Finished recalculation of today's charging history")
            self.notify(
                f"✅ Finished {self.title} recalculation of today's charging history (hourly blocks) without errors.",
                title=f"{self.title} recalculation complete",
                notification_id=f"{self.name}_recalc_charging_history_today",
            )
        return failures
