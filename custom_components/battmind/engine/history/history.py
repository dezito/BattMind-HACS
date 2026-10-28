"""Charging history and battery level expenses (engine part)."""
from __future__ import annotations

import datetime
import logging

from ...const import CHARGING_HISTORY_COMBINE_AFTER
from ...utils.formatting import emoji_parse, emoji_text_format
from ...utils.helpers import days_between, month_first_day

from .queries import HistoryQueries
from .storage import HistoryStorage

_LOGGER = logging.getLogger(__name__)


class History(HistoryStorage, HistoryQueries):
    """Charging history, combined markdown history and battery level expenses."""

    def charging_history_combine_and_set(self, get_ending_byte_size: bool = False) -> None:
        t = self.t
        history: list[str] = []
        combined_db: dict = {}

        if not self.charging_history_db:
            self.history_footer_bytes = 1000
            self.charging_history_total = {"charged_kwh": 0.0, "discharged_kwh": 0.0, "balance_kwh": 0.0}
            self._job_cache.pop("charging_loss", None)
            if not get_ending_byte_size:
                self.set_attr("charging_history", "history", self.t("ui.reason.no_charging_history"))
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
            self.set_attr("charging_history", "history", "\n".join(history) if history else self.t("ui.reason.no_charging_history"))
            if combined_db != self.charging_history_db:
                self.charging_history_db = combined_db
                self.mark_dirty("charging_history")
