"""Charging history and battery level expenses (engine mixin)."""
from __future__ import annotations

import datetime
import logging
from copy import deepcopy

from .const import CHARGING_HISTORY_COMBINE_AFTER
from .formatting import emoji_parse, emoji_text_format
from .helpers import days_between, month_first_day, months_between, reset_time_to_hour

_LOGGER = logging.getLogger(__name__)


class HistoryMixin:
    """Charging history, combined markdown history and battery level expenses."""

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

    def charging_history_combine_and_set(self, get_ending_byte_size: bool = False) -> None:
        t = self.t
        history: list[str] = []
        combined_db: dict = {}

        if not self.charging_history_db:
            self.history_footer_bytes = 1000
            self.charging_history_total = {"charged_kwh": 0.0, "discharged_kwh": 0.0, "balance_kwh": 0.0}
            self._job_cache.pop("charging_loss", None)
            if not get_ending_byte_size:
                self.set_attr("charging_history", "history", "**Ingen lade historik**")
            return

        def _num(value, default=0.0) -> float:
            return float(value) if isinstance(value, (int, float)) else default

        def _round_or_none(value, decimals=3):
            return round(float(value), decimals) if isinstance(value, (int, float)) else None

        def _fmt(value, decimals=2, default="") -> str:
            return f"{value:.{decimals}f}" if isinstance(value, (int, float)) else default

        def _buy_color(text: str) -> str:
            return f"<font color='white'>{text}</font>"

        def _charge_color(text: str) -> str:
            return f"<font color='lightgreen'>{text}</font>"

        def _solar_color(text: str) -> str:
            return f"<font color='yellow'>{text}</font>"

        def _discharge_color(text: str) -> str:
            return f"<font color='darkorange'>{text}</font>"

        max_entity_attributes_bytes_size = 16384
        history_bytes_buffer = 0 if self.history_footer_bytes is None else self.history_footer_bytes
        max_history_bytes_size = max_entity_attributes_bytes_size - history_bytes_buffer
        current_history_bytes_size = 0

        def history_loop_append(line_or_lines):
            nonlocal current_history_bytes_size
            if isinstance(line_or_lines, list):
                lines = [str(x) for x in line_or_lines]
                history.extend(lines)
                current_history_bytes_size += len("\n".join(lines).encode("utf-8"))
            else:
                text = str(line_or_lines)
                history.append(text)
                current_history_bytes_size += len(text.encode("utf-8"))

        sorted_db = sorted(self.charging_history_db.items(), key=lambda item: item[0], reverse=True)
        sorted_db_len = len(sorted_db)

        length = 10
        sub_length = 50
        sub_details_count = 0
        details = False
        add_header = False
        history_too_big_announced = False
        current_history_counter = 0
        combined_block_counter = 0

        charge_label = f"{emoji_parse({'charging': True})}{t('ui.common.price')}<br>{t('ui.common.valuta_kwh')}"
        discharge_label = f"{emoji_parse({'discharging': True})}{t('ui.common.price')}<br>{t('ui.common.valuta_kwh')}"
        savings_label = f"{emoji_parse({'profit': True})}<br>{t('ui.common.valuta_kwh')}"
        header = (
            f"| {t('ui.common.time')} | %<br>({emoji_parse({'solar': True})}) | kWh<br>({emoji_parse({'solar': True})}) | "
            f"{_charge_color(charge_label)} | {_discharge_color(discharge_label)} | {savings_label} |"
        )
        align = "|:---:|---:|---:|:---:|:---:|:---:|"

        history_loop_append("<center>\n")
        history_loop_append(header)
        history_loop_append(align)

        total_keys = (
            "home_consumption_kwh", "discharge_kwh", "solar_production_kwh", "home_consumption_without_solar_kwh",
            "grid_consumption_kwh", "charged_kwh", "charged_cost", "charged_local_kwh", "charged_grid_kwh",
            "charged_pct", "discharged_kwh", "discharged_cost", "discharged_pct", "savings",
        )
        total = {key: {"total": 0.0} for key in total_keys}

        def _ensure_month(month):
            for key in total:
                total[key].setdefault(month, 0.0)

        def _read_session(session: dict):
            energy_summary = session.get("energy_summary", {}) if isinstance(session.get("energy_summary", {}), dict) else {}
            prices = session.get("prices", {}) if isinstance(session.get("prices", {}), dict) else {}
            charged = session.get("charged", {}) if isinstance(session.get("charged", {}), dict) else {}
            discharged = session.get("discharged", {}) if isinstance(session.get("discharged", {}), dict) else {}
            c_kwh = _num(charged.get("kWh", 0.0))
            c_local = _num(charged.get("kWh_from_local_energy", 0.0))
            savings = discharged.get("savings")
            return (
                prices,
                c_kwh,
                _num(charged.get("percentage", 0.0)),
                _num(charged.get("cost", 0.0)),
                c_local,
                _num(charged.get("kWh_from_grid", max(c_kwh - c_local, 0.0))),
                _num(discharged.get("kWh", 0.0)),
                _num(discharged.get("percentage", 0.0)),
                _num(discharged.get("cost", 0.0)),
                float(savings) if isinstance(savings, (int, float)) else 0.0,
                dict(session.get("rules", {})) if isinstance(session.get("rules", {}), dict) else {},
                _num(energy_summary.get("home_consumption_kwh", 0.0)),
                _num(energy_summary.get("solar_production_kwh", 0.0)),
                _num(energy_summary.get("home_consumption_without_solar_kwh", 0.0)),
                _num(energy_summary.get("grid_consumption_kwh", 0.0)),
            )

        idx = 0
        while idx < sorted_db_len:
            when, session = sorted_db[idx]
            if not isinstance(when, datetime.datetime) or not isinstance(session, dict):
                idx += 1
                continue

            (prices, c_kwh, c_pct, c_cost, c_local, c_grid, d_kwh, d_pct, d_cost, savings_num, rules,
             e_home, e_solar, e_home_without_solar, e_grid) = _read_session(session)

            if c_kwh <= 0.0 and d_kwh <= 0.0 and c_pct <= 0.0 and d_pct <= 0.0 and savings_num == 0.0:
                idx += 1
                continue

            started = when
            used_prices = prices
            total_prices = {k: [] for k in ("buy_price", "sell_price", "powerwall_kwh_price", "kwh_savings", "charge_price", "discharge_price")}

            if combined_block_counter >= CHARGING_HISTORY_COMBINE_AFTER:
                j = idx + 1
                while j < sorted_db_len:
                    next_when, next_session = sorted_db[j]
                    if not isinstance(next_when, datetime.datetime) or not isinstance(next_session, dict):
                        break
                    if days_between(when, next_when) != 0:
                        break
                    (n_prices, nc_kwh, nc_pct, nc_cost, nc_local, nc_grid, nd_kwh, nd_pct, nd_cost, nsavings, n_rules,
                     ne_home, ne_solar, ne_home_without_solar, ne_grid) = _read_session(next_session)
                    for key in total_prices:
                        if n_prices.get(key):
                            total_prices[key].append(n_prices.get(key))
                    c_kwh += nc_kwh
                    c_pct += nc_pct
                    c_cost += nc_cost
                    c_local += nc_local
                    c_grid += nc_grid
                    d_kwh += nd_kwh
                    d_pct += nd_pct
                    d_cost += nd_cost
                    savings_num += nsavings
                    e_home += ne_home
                    e_solar += ne_solar
                    e_home_without_solar += ne_home_without_solar
                    e_grid += ne_grid
                    started = next_when
                    rules.update(n_rules)
                    j += 1
                idx = j
            else:
                idx += 1

            combined_block_counter += 1

            def _avg_price(key):
                values = total_prices[key]
                return _round_or_none(sum(values) / len(values) if values else used_prices.get(key), 3)

            combined_db[started] = {
                "energy_summary": {
                    "home_consumption_kwh": round(e_home, 3),
                    "solar_production_kwh": round(e_solar, 3),
                    "home_consumption_without_solar_kwh": round(e_home_without_solar, 3),
                    "grid_consumption_kwh": round(e_grid, 3),
                },
                "prices": {key: _avg_price(key) for key in total_prices},
                "charged": {
                    "percentage": round(c_pct, 1),
                    "kWh": round(c_kwh, 3),
                    "kWh_from_grid": round(c_grid, 3),
                    "kWh_from_local_energy": round(c_local, 3),
                    "cost": round(c_cost, 3),
                    "price": _round_or_none(c_cost / c_kwh if c_kwh else None, 3),
                },
                "discharged": {
                    "percentage": round(d_pct, 1),
                    "kWh": round(d_kwh, 3),
                    "cost": round(d_cost, 3),
                    "price": _round_or_none(d_cost / d_kwh if d_kwh else None, 3),
                    "savings": _round_or_none(savings_num, 3),
                },
                "rules": rules,
            }

            month = month_first_day(started)
            _ensure_month(month)
            for key, value in (
                ("home_consumption_kwh", e_home), ("solar_production_kwh", e_solar),
                ("home_consumption_without_solar_kwh", e_home_without_solar), ("grid_consumption_kwh", e_grid),
                ("charged_kwh", c_kwh), ("charged_cost", c_cost), ("charged_local_kwh", c_local),
                ("charged_grid_kwh", c_grid), ("charged_pct", c_pct), ("discharged_kwh", d_kwh),
                ("discharged_cost", d_cost), ("discharged_pct", d_pct), ("savings", savings_num),
            ):
                total[key][month] += value
                total[key]["total"] += value

            c_local_pct = c_pct * (c_local / c_kwh) if c_kwh > 0 else 0.0
            c_kwh = round(c_kwh, 1)
            c_local = round(c_local, 1)
            c_local_pct = round(c_local_pct, 0)
            c_pct = round(c_pct, 0)
            d_kwh = round(d_kwh, 1)
            d_pct = round(d_pct, 0)

            if not history_too_big_announced:
                if current_history_bytes_size >= max_history_bytes_size:
                    history_too_big_announced = True
                    history_loop_append(f"##### {t('ui.charging_history_combine_and_set.too_big_warning')}\n")
                elif current_history_counter == length and not details:
                    details = True
                    history_loop_append(["<details>", f"<summary><b>{t('ui.charging_history_combine_and_set.more_history')}</b></summary>\n"])
                    add_header = True
                elif (
                    current_history_bytes_size < max_history_bytes_size - 1
                    and current_history_counter > length
                    and current_history_counter >= sub_length
                    and current_history_counter % sub_length == 0
                    and (sorted_db_len - idx) >= sub_length
                ):
                    history_loop_append(["\n", "<details>", f"<summary><b>{t('ui.charging_history_combine_and_set.more_history')}</b></summary>\n"])
                    sub_details_count += 1
                    add_header = True

            if current_history_bytes_size < max_history_bytes_size:
                if add_header:
                    add_header = False
                    history_loop_append([header, align])

                if combined_block_counter > CHARGING_HISTORY_COMBINE_AFTER:
                    time_str = f"**{started.strftime('%d/%m')}**"
                else:
                    time_str = f"**{started.strftime('%d/%m %H:%M')}**"

                rules_without_solar = dict(rules)
                rules_without_solar.pop("solar", None)
                emoji_str = f"<br>{emoji_text_format(emoji_parse(rules_without_solar), group_size=3)}" if rules else ""

                pct_lines = []
                if c_kwh > 0.0 or c_pct > 0.0:
                    pct_lines = [_charge_color(f"{int(c_pct)}")]
                    if c_local_pct > 0.0 and int(c_local_pct) == int(c_pct):
                        pct_lines = [_solar_color(f"{int(c_local_pct)}")]
                    elif c_local_pct > 0.0:
                        pct_lines.append(_solar_color(f"({int(c_local_pct)})"))
                if d_kwh > 0.0 or d_pct > 0.0:
                    pct_lines.append(_discharge_color(f"-{int(d_pct)}"))
                pct_str = "<br>".join(pct_lines)

                kwh_lines = []
                if c_kwh > 0.0:
                    c_line = _charge_color(_fmt(c_kwh, 1, "0.0"))
                    if c_local == c_kwh:
                        c_line = _solar_color(_fmt(c_local, 1, "0.0"))
                    elif c_local > 0.0:
                        c_line += f"<br>({_solar_color(_fmt(c_local, 1, '0.0'))})"
                    kwh_lines.append(c_line)
                if d_kwh > 0.0:
                    kwh_lines.append(_discharge_color(f"-{_fmt(d_kwh, 1, '0.0')}"))
                kwh_str = "<br>".join(kwh_lines)

                buy_price = used_prices.get("buy_price")
                charge_price_unit = (c_cost / c_kwh) if c_kwh > 0 else None
                charge_price_lines = []
                if combined_block_counter < CHARGING_HISTORY_COMBINE_AFTER and d_kwh > 0.0 and buy_price is not None:
                    charge_price_lines.append(_buy_color(f"🔌{_fmt(buy_price * d_kwh, 2, '0.00')}<br>({_fmt(buy_price, 2, '')})"))
                if c_kwh > 0.0:
                    charge_price_lines.append(f"{_fmt(c_cost, 2, '0.00')}<br>({_fmt(charge_price_unit, 2, '')})")
                charge_price_str = "<br>".join(charge_price_lines)

                discharge_price_unit = (d_cost / d_kwh) if d_kwh > 0 else None
                discharge_price_str = (
                    f"{_fmt(d_cost, 2, '0.00')}<br>({_fmt(discharge_price_unit, 2, '')})" if d_kwh > 0.0 else ""
                )

                sav_lines = []
                if savings_num != 0.0:
                    sav_lines.append(_fmt(savings_num, 2, "0.00"))
                    if (
                        combined_block_counter < CHARGING_HISTORY_COMBINE_AFTER
                        and d_kwh > 0.0
                        and buy_price is not None
                        and discharge_price_unit is not None
                    ):
                        sav_lines.append(f"({_fmt(buy_price - discharge_price_unit, 2, '0.00')})")
                sav_str = "<br>".join(sav_lines) if round(savings_num, 2) != 0.0 else ""

                if pct_str or kwh_str or charge_price_str or discharge_price_str:
                    history_loop_append(
                        f"| {time_str}{emoji_str} | {pct_str} | {kwh_str} | {_charge_color(charge_price_str)} | "
                        f"{_discharge_color(discharge_price_str)} | {sav_str} |"
                    )
                current_history_counter += 1

            if current_history_counter > length and (sorted_db_len - idx) == 0:
                history_loop_append(["</details>", "\n"] * (sub_details_count + 1))

        if details:
            history.extend(["</details>", "\n"])

        ck_tot = total["charged_kwh"]["total"]
        dk_tot = total["discharged_kwh"]["total"]
        cc_tot = total["charged_cost"]["total"]
        dc_tot = total["discharged_cost"]["total"]
        sav_tot = total["savings"]["total"]

        total_charge_price_str = f"<br>({_fmt(cc_tot / ck_tot, 2, '')})" if ck_tot > 0 else ""
        total_discharge_price_str = f"<br>({_fmt(dc_tot / dk_tot, 2, '')})" if dk_tot > 0 else ""
        total_savings_price_str = f"<br>({_fmt(sav_tot / dk_tot, 2, '')})" if dk_tot > 0 else ""

        net_kwh = ck_tot - dk_tot
        net_cost = cc_tot - dc_tot
        self.charging_history_total = {"charged_kwh": ck_tot, "discharged_kwh": dk_tot, "balance_kwh": net_kwh}
        self._job_cache.pop("charging_loss", None)  # the measured loss depends on the totals

        spacing_style = "display:inline-block; text-align:center;"
        col_charge = (
            f"<span style='{spacing_style}'>{emoji_parse({'charging': True})}{_fmt(ck_tot, 1, '0.0')}kWh&nbsp;&nbsp;&nbsp;"
            f"{cc_tot:.2f}{t('ui.common.valuta')}<br></span>"
        )
        col_discharge = (
            f"<span style='{spacing_style}'>{emoji_parse({'discharging': True})}{_fmt(dk_tot, 1, '0.0')}kWh&nbsp;&nbsp;&nbsp;"
            f"{dc_tot:.2f}{t('ui.common.valuta')}<br></span>"
        )
        col_net = (
            f"<span style='{spacing_style}'>{emoji_parse({'balance': True})}{_fmt(net_kwh, 1, '0.0')}kWh&nbsp;&nbsp;&nbsp;"
            f"{net_cost:.2f}{t('ui.common.valuta')}<br></span>"
        )
        first_line = f"{t('ui.common.total')}: {_charge_color(col_charge)}{_discharge_color(col_discharge)}{col_net}"

        history.append("---")
        history.append("<details>")
        history.append(f"\n<summary><b>{first_line}{emoji_parse({'profit': True})} {sav_tot:.2f}{t('ui.common.valuta')}</b></summary>\n")
        history.extend([
            f"| {t('ui.charging_history_combine_and_set.month')} | kWh<br>{emoji_parse({'charging': True})} | "
            f"kWh<br>{emoji_parse({'discharging': True})} | kWh<br>{emoji_parse({'balance': True})} | "
            f"{t('ui.common.price')}<br>{emoji_parse({'charging': True})} | {t('ui.common.price')}<br>{emoji_parse({'discharging': True})} | "
            f"{emoji_parse({'profit': True})} |",
            "|:---:|:---:|:---:|:---:|:---:|:---:|:---:|",
        ])

        month_keys = [k for k in total["charged_kwh"] if isinstance(k, datetime.datetime)]
        for idx_m, month in enumerate(sorted(month_keys)):
            ck = total["charged_kwh"][month]
            dk = total["discharged_kwh"][month]
            cc = total["charged_cost"][month]
            dc = total["discharged_cost"][month]
            sav = total["savings"][month]
            charge_str = f"<br>({_fmt(cc / ck, 2, '')})" if ck > 0 else ""
            discharge_str = f"<br>({_fmt(dc / dk, 2, '')})" if dk > 0 else ""
            savings_str = f"<br>({_fmt(sav / dk, 2, '')})" if dk > 0 else ""
            bg_s = "<font color=grey>" if idx_m % 2 == 0 else ""
            bg_e = "</font>" if idx_m % 2 == 0 else ""
            month_name = t(f"ui.calendar.month_names.{month.strftime('%B').lower()}")
            history.append(
                f"| {bg_s}{month_name}<br>{month.strftime('%Y')}{bg_e} | "
                f"{bg_s}{ck:.1f}{bg_e} | {bg_s}-{dk:.1f}{bg_e} | {bg_s}{ck - dk:.1f}{bg_e} | "
                f"{bg_s}{cc:.2f}{charge_str}{bg_e} | {bg_s}{dc:.2f}{discharge_str}{bg_e} | {bg_s}{sav:.2f}{savings_str}{bg_e} |"
            )

        history.append(
            f"| **{t('ui.common.total')}** | **{ck_tot:.1f}** | **-{dk_tot:.1f}** | **{net_kwh:.1f}** | "
            f"**{cc_tot:.2f}{total_charge_price_str}** | **{dc_tot:.2f}{total_discharge_price_str}** | "
            f"**{sav_tot:.2f}{total_savings_price_str}** |"
        )
        history.append("\n</details>\n")
        history.append("</center>")

        total_history_bytes_size = len("\n".join(history).encode("utf-8"))
        self.history_footer_bytes = total_history_bytes_size - current_history_bytes_size + 1000

        if not get_ending_byte_size:
            self.set_attr("charging_history", "history", "\n".join(history) if history else "**Ingen lade historik**")
            if combined_db != self.charging_history_db:
                self.charging_history_db = combined_db
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

    def charging_history(self, timestamp=None, save_db=True) -> dict:
        if not self.battery_expenses or save_db:
            self.current_battery_level_expenses()

        start = reset_time_to_hour(timestamp if timestamp is not None else self.now())
        end = start.replace(minute=59, second=59, microsecond=0)
        _LOGGER.debug("Calculating charging history for period: %s-%s", start, end.strftime("%H:%M:%S"))

        data = self.power_values(start, end)
        home_consumption_kwh = data["power_consumption"] / 1000.0
        charge_kwh = data["powerwall_charging_consumption"] / 1000.0
        discharge_kwh = data["powerwall_discharging_consumption"] / 1000.0
        solar_production_kwh = data["solar_production"] / 1000.0

        home_consumption_without_solar_kwh = max(home_consumption_kwh - solar_production_kwh, 0.0)
        grid_consumption_kwh = home_consumption_kwh - (discharge_kwh + solar_production_kwh)

        charge_grid_share_pct = max(grid_consumption_kwh / charge_kwh if charge_kwh > 0.0 else 0.0, 0.0)
        solar_charge_share_pct = max(solar_production_kwh / charge_kwh if charge_kwh > 0.0 else 0.0, 0.0)
        discharge_grid_share_pct = max(grid_consumption_kwh / discharge_kwh if discharge_kwh > 0.0 else 0.0, 0.0)
        solar_discharge_share_pct = max(
            solar_production_kwh / (discharge_kwh + home_consumption_kwh) if discharge_kwh > 0.0 else 0.0, 0.0
        )
        powerwall_discharge_share_pct = max(
            discharge_kwh / home_consumption_without_solar_kwh if home_consumption_without_solar_kwh > 0.0 else 0.0, 0.0
        )

        charge_share_pct_sum = charge_grid_share_pct + solar_charge_share_pct
        discharge_share_pct_sum = discharge_grid_share_pct + solar_discharge_share_pct + powerwall_discharge_share_pct
        charge_normalization_factor = charge_share_pct_sum if charge_share_pct_sum > 0.0 else 1.0
        charge_grid_share_pct /= charge_normalization_factor
        solar_charge_share_pct /= charge_normalization_factor
        discharge_normalization_factor = discharge_share_pct_sum if discharge_share_pct_sum > 0.0 else 1.0
        discharge_grid_share_pct /= discharge_normalization_factor
        solar_discharge_share_pct /= discharge_normalization_factor
        powerwall_discharge_share_pct /= discharge_normalization_factor

        buy_price = self.get_grid_prices().get(start)
        sell_price = self.get_sell_prices().get(start)
        powerwall_kwh_price = self.battery_expenses.get("unit_with_loss_and_wear")

        if buy_price is None or sell_price is None:
            raise ValueError(f"Buy or sell price not found for timestamp {start}. buy_price: {buy_price}, sell_price: {sell_price}")
        sell_price = self.sell_value(sell_price)
        if not isinstance(powerwall_kwh_price, (int, float)):
            powerwall_kwh_price = self.get_powerwall_kwh_price()

        charge_price = buy_price * charge_grid_share_pct + sell_price * solar_charge_share_pct
        discharge_price = (
            sell_price * discharge_grid_share_pct
            + sell_price * solar_discharge_share_pct
            + powerwall_kwh_price * powerwall_discharge_share_pct
        )
        charge_cost = charge_price * charge_kwh
        discharge_cost = discharge_price * discharge_kwh
        home_consumption_cost = home_consumption_without_solar_kwh * buy_price if discharge_kwh > 0.0 else 0.0
        sell_consumption_revenue = (discharge_kwh - home_consumption_without_solar_kwh) * sell_price if discharge_kwh > 0.0 else 0.0
        savings = (home_consumption_cost + sell_consumption_revenue) - (discharge_kwh * discharge_price)
        kwh_savings = savings / discharge_kwh if discharge_kwh != 0 else None

        kwh_from_local_energy = 0.0
        if charge_kwh > 0.0:
            try:
                _, kwh_from_local_energy = self.calc_local_energy_kwh(start, end, charge_kwh)
                kwh_from_local_energy = min(kwh_from_local_energy, charge_kwh)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Error calculating local energy kWh: %s", err)
                self.notify(
                    f"Charging history failed to calculate local energy kWh or price: {err} {type(err)}",
                    f"{self.title} failed",
                    f"{self.name}_charging_history_calc_local_energy_kwh_failed",
                )

        def _r(value, decimals=3):
            return round(value, decimals) if isinstance(value, (int, float)) else None

        session_dict = {
            "rules": dict(self.session_rules),
            "energy_summary": {
                "home_consumption_kwh": _r(home_consumption_kwh),
                "solar_production_kwh": _r(solar_production_kwh),
                "home_consumption_without_solar_kwh": _r(home_consumption_without_solar_kwh),
                "grid_consumption_kwh": _r(grid_consumption_kwh),
            },
            "prices": {
                "buy_price": _r(buy_price),
                "sell_price": _r(sell_price),
                "powerwall_kwh_price": _r(powerwall_kwh_price),
                "kwh_savings": _r(kwh_savings),
                "charge_price": _r(charge_price),
                "discharge_price": _r(discharge_price),
            },
            "charged": {
                "percentage": round(self.kwh_to_percentage(charge_kwh), 1),
                "kWh": round(charge_kwh, 3),
                "kWh_from_grid": round(max(charge_kwh - kwh_from_local_energy, 0.0), 3),
                "kWh_from_local_energy": round(kwh_from_local_energy, 3),
                "cost": _r(charge_cost),
                "price": _r(charge_price),
            },
            "discharged": {
                "percentage": round(self.kwh_to_percentage(discharge_kwh), 1),
                "kWh": round(discharge_kwh, 3),
                "cost": _r(discharge_cost),
                "price": _r(discharge_price),
                "savings": _r(savings),
            },
        }

        if charge_kwh > 0.0 or discharge_kwh > 0.0:
            _LOGGER.info(
                "Charging history %s: charged %.3f kWh, discharged %.3f kWh, charge cost %.2f kr, discharge cost %.2f kr",
                start, charge_kwh, discharge_kwh, charge_cost, discharge_cost,
            )
            self.charging_history_db[start] = session_dict
            self.mark_dirty("charging_history")

        if not save_db:
            return session_dict

        self.session_rules = {}
        try:
            self.charging_history_combine_and_set()
            self.save_charging_history()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error combining and setting charging history: %s", err)
            self.notify(
                f"Charging history failed to combine and set after adding new session: {err} {type(err)}",
                f"{self.title} failed",
                f"{self.name}_charging_history_combine_and_set_failed",
            )
        return session_dict

    def current_battery_level_expenses(self) -> dict:
        cfg = self.config
        self.battery_expenses = expenses = {
            "kwh": 0.0,
            "percentage": 0.0,
            "solar_percentage": 0.0,
            "cost": 0.0,
            "unit": None,
            "cost_loss": 0.0,
            "cost_wear": 0.0,
            "cost_with_loss_and_wear": 0.0,
            "unit_with_loss_and_wear": None,
            "loss_percentage": self.get_charging_loss(),
        }
        try:
            current_battery_level = self.get_battery_level()
            if self.charging_history_db:
                for key in sorted(self.charging_history_db.keys(), reverse=True):
                    if round(expenses["percentage"]) >= round(current_battery_level):
                        break
                    charged = self.charging_history_db[key].get("charged", {})
                    if any(k not in charged for k in ("price", "cost", "kWh", "kWh_from_local_energy", "percentage")):
                        continue
                    if not isinstance(charged["price"], (int, float)):
                        continue
                    cost = charged["cost"] or 0.0
                    kwh = charged["kWh"]
                    percentage = charged["percentage"]
                    solar_percentage = self.kwh_to_percentage(charged["kWh_from_local_energy"])
                    cost_loss = self.calc_battery_loss_cost(cost)
                    total_cost = (cost + cost_loss) + abs(cfg["solar"]["powerwall_wear_cost_per_kwh"])

                    new_battery_level = percentage + expenses["percentage"]
                    if new_battery_level > current_battery_level and percentage > 0.0:
                        diff = (percentage - (new_battery_level - current_battery_level)) / percentage
                        cost *= diff
                        cost_loss *= diff
                        total_cost *= diff
                        kwh *= diff
                        percentage *= diff
                        solar_percentage *= diff

                    expenses[key] = charged
                    expenses["kwh"] += kwh
                    expenses["percentage"] += percentage
                    expenses["solar_percentage"] += solar_percentage
                    expenses["cost"] += cost
                    expenses["cost_loss"] += cost_loss
                    expenses["cost_wear"] += abs(cfg["solar"]["powerwall_wear_cost_per_kwh"]) * kwh
                    expenses["cost_with_loss_and_wear"] += total_cost

                expenses["kwh"] = round(expenses["kwh"], 3)
                expenses["percentage"] = round(expenses["percentage"], 1)
                expenses["solar_percentage"] = round(expenses["solar_percentage"], 1)
                expenses["cost"] = round(expenses["cost"], 3)
                expenses["cost_with_loss_and_wear"] = round(expenses["cost_with_loss_and_wear"], 3)
                if expenses["kwh"] > 0.0:
                    expenses["unit"] = round(expenses["cost"] / expenses["kwh"], 3)
                    expenses["unit_with_loss_and_wear"] = round(expenses["cost_with_loss_and_wear"] / expenses["kwh"], 3)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Error in battery level cost calculation: %s", err)
        return expenses
