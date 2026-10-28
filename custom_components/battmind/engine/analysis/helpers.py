"""Constants and pure functions of the analysis."""
from __future__ import annotations

import datetime

from ...utils.helpers import reset_time_to_hour


ANALYSIS_DAYS = 400  # days kept in the database


# raised when the backfill reads more: the days from the statistics are read again (2: prices with state_class total)
ANALYSIS_BACKFILL_VERSION = 2


ANALYSIS_MONTHS = 13  # months published


ANALYSIS_LIST_DAYS = 31  # days published for the daily charts


ANALYSIS_TABLE_DAYS = 14  # days published for the daily tables


EXPENSIVE_HOURS = 4  # the most expensive hours of a day that are checked for an empty battery


EMPTY_MARGIN = 2.0  # % above the minimum battery level that counts as empty


SCORE_STEPS = 60  # battery levels in the hindsight optimisation


FORECAST_TYPES = ("ema", "trend", "average")


# rules that charge from the grid / that discharge (sell) the battery, see CHARGING_TYPES
CHARGE_RULES = (
    "cheapest_hour_fill_planner", "most_expensive_planner", "needed_before_max_level_planner",
    "negative_price_planner", "grid_charge_to_sell",
)


SALE_RULES = (
    "most_profit", "sell_excess_kwh_available", "force_powerwall_max_hours_old_discharge",
    "discharge_before_solar", "grid_charge_to_sell",
)


DAY_SUMS = (
    "home", "ignored", "solar", "charged", "discharged", "solar_home", "battery_home", "grid_home",
    "solar_battery", "grid_battery", "export_solar", "export_battery", "solar_surplus",
)


PRICE_FORECAST_DAYS = 7  # days ahead a price forecast is recorded for


PRICE_HOURS_DAYS = 7  # days the hourly forecasts and real prices are kept


PRICE_CHART_DAYS = 2  # past days published for the hourly price chart


PRICE_CHART_LEADS = tuple(range(1, PRICE_FORECAST_DAYS + 1))  # days ahead shown in the charts


PRICE_DAY_MIN_HOURS = 20  # forecast hours needed to judge a day


PRICE_RANK_HOURS = 4  # the cheapest / most expensive hours of a day the forecast should find


PRICE_CORRECTION_DAYS = 30  # days the bias of the price forecast is measured over


PRICE_CORRECTION_MIN_DAYS = 7  # days needed per days ahead before the forecast is corrected


PRICE_CORRECTION_MAX = 0.5  # currency/kWh the forecast is corrected by at most


MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)


def _num(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _r(value, decimals: int = 3):
    return round(float(value), decimals) if _is_num(value) else None


def split_flows(home: float, solar: float, charge: float, discharge: float) -> dict[str, float]:
    """kWh of an hour by source: solar first to the house, then the battery, the rest from the grid."""
    solar_home = min(solar, home)
    battery_home = min(discharge, home - solar_home)
    solar_left = solar - solar_home
    solar_battery = min(charge, solar_left)
    return {
        "solar_home": solar_home,
        "battery_home": battery_home,
        "grid_home": max(home - solar_home - battery_home, 0.0),
        "solar_battery": solar_battery,
        "grid_battery": charge - solar_battery,
        "export_solar": solar_left - solar_battery,
        "export_battery": discharge - battery_home,
    }


def grid_cost(kwh: float, buy: float, sell: float) -> float:
    """Cost of kWh from the grid (negative kWh: sold at the sell price)."""
    return kwh * buy if kwh > 0 else kwh * sell


def hindsight_cost(rows: list[dict], start: float, end: float, low: float, high: float,
                   max_charge: float, max_discharge: float, efficiency: float, wear: float) -> float | None:
    """Lowest grid cost of a day with perfect knowledge of consumption, solar and prices.

    rows: per hour {"load": kWh house - solar (negative = surplus), "buy", "sell"}. The battery
    starts with start kWh, ends with at least end kWh and stays within low..high; charging and
    discharging are limited per hour and lose efficiency each way, wear is paid per kWh charged.
    """
    if high - low <= 0.01 or efficiency <= 0.0:
        return None
    step = (high - low) / SCORE_STEPS
    up = int(max_charge * efficiency / step + 1e-9)
    down = int(max_discharge / efficiency / step + 1e-9)
    inf = float("inf")
    costs = [inf] * (SCORE_STEPS + 1)
    costs[min(max(round((start - low) / step), 0), SCORE_STEPS)] = 0.0
    for row in rows:
        new = [inf] * (SCORE_STEPS + 1)
        for i, cost in enumerate(costs):
            if cost == inf:
                continue
            for j in range(max(i - down, 0), min(i + up, SCORE_STEPS) + 1):
                delta = (j - i) * step
                if delta > 0:
                    ac = delta / efficiency
                    total = cost + grid_cost(row["load"] + ac, row["buy"], row["sell"]) + wear * ac
                else:
                    total = cost + grid_cost(row["load"] + delta * efficiency, row["buy"], row["sell"])
                if total < new[j]:
                    new[j] = total
        costs = new
    target = min(max(round((end - low) / step), 0), SCORE_STEPS)
    best = min(costs[target:])
    return None if best == inf else best


def summarize_day(rows: list[dict], soc_start: float | None, minimum: float) -> dict:
    """The hours of a day (sorted) added up."""
    day = dict.fromkeys(DAY_SUMS, 0.0)
    price_sum = price_hours = 0.0
    errors = {kind: [0.0, 0.0] for kind in FORECAST_TYPES}  # [sum |error|, sum error]
    predicted_hours = 0
    predicted_use = 0.0
    if soc_start is None and rows and _is_num(rows[0].get("soc")):
        # no previous day: the level at the end of the first hour
        soc_start = rows[0]["soc"]
    level = soc_start
    expensive: list[tuple[float, float | None, dict, dict]] = []
    for row in rows:
        home, solar = _num(row.get("home")), _num(row.get("solar"))
        charge, discharge = _num(row.get("charge")), _num(row.get("discharge"))
        flows = split_flows(home, solar, charge, discharge)
        for key, value in flows.items():
            day[key] += value
        day["home"] += home
        day["ignored"] += _num(row.get("ignored"))
        day["solar"] += solar
        day["charged"] += charge
        day["discharged"] += discharge
        day["solar_surplus"] += max(solar - _num(row.get("use")), 0.0)
        if _is_num(row.get("buy")):
            price_sum += row["buy"]
            price_hours += 1
            expensive.append((row["buy"], level, flows, row))
        prediction = row.get("pred") or {}
        if all(_is_num(prediction.get(kind)) for kind in FORECAST_TYPES):
            predicted_hours += 1
            predicted_use += _num(row.get("use"))
            for kind in FORECAST_TYPES:
                error = prediction[kind] - _num(row.get("use"))
                errors[kind][0] += abs(error)
                errors[kind][1] += error
        if _is_num(row.get("soc")):
            level = row["soc"]

    result = {key: round(value, 3) for key, value in day.items()}
    result["hours"] = len(rows)
    result["price_sum"] = round(price_sum, 3)
    result["price_hours"] = int(price_hours)
    result["soc_start"] = _r(soc_start, 1)
    result["soc_end"] = _r(level, 1)
    if predicted_hours:
        result["forecast"] = {
            "hours": predicted_hours,
            "use": round(predicted_use, 3),
            **{kind: [round(errors[kind][0], 3), round(errors[kind][1], 3)] for kind in FORECAST_TYPES},
        }
    # the most expensive hours: was the battery empty while the house used grid power?
    empty = [
        (price, flows["grid_home"]) for price, start_level, flows, _ in sorted(expensive, key=lambda item: item[0], reverse=True)[:EXPENSIVE_HOURS]
        if start_level is not None and start_level <= minimum + EMPTY_MARGIN and flows["grid_home"] >= 0.05
    ]
    if expensive:
        result["expensive"] = {
            "hours": len(empty),
            "kwh": round(sum(kwh for _, kwh in empty), 3),
            "cost": round(sum(price * kwh for price, kwh in empty), 3),
        }
    return result


def hourly_prices(prices: dict) -> dict[datetime.datetime, float]:
    """Prices per hour (15 minute prices averaged)."""
    sums: dict[datetime.datetime, list[float]] = {}
    for timestamp, price in prices.items():
        if isinstance(timestamp, datetime.datetime) and _is_num(price):
            bucket = sums.setdefault(reset_time_to_hour(timestamp), [0.0, 0.0])
            bucket[0] += price
            bucket[1] += 1
    return {hour: total / count for hour, (total, count) in sums.items()}


def evaluate_price_day(rows: list[dict]) -> dict[str, dict]:
    """The forecast of a day's hours against the real prices, per days ahead.

    rows: per hour {"real": price, "<days ahead>": forecast}. n hours, abs (sum |error|),
    bias (sum error, > 0: forecast too high), max (worst hour); cheap/dear: how many of the
    PRICE_RANK_HOURS cheapest/most expensive hours by the forecast really were, cheap_miss/
    dear_miss: currency/kWh lost on average by picking those hours instead of the real ones.
    """
    result: dict[str, dict] = {}
    rows = [row for row in rows if _is_num(row.get("real"))]
    for lead in range(1, PRICE_FORECAST_DAYS + 1):
        pairs = [(row[str(lead)], row["real"]) for row in rows if _is_num(row.get(str(lead)))]
        if len(pairs) < PRICE_DAY_MIN_HOURS:
            continue
        errors = [forecast - real for forecast, real in pairs]
        count = min(PRICE_RANK_HOURS, len(pairs))

        def ranked(by_forecast: bool, reverse: bool) -> list[int]:
            return sorted(range(len(pairs)), key=lambda i: pairs[i][0 if by_forecast else 1], reverse=reverse)[:count]

        def real_sum(indexes: list[int]) -> float:
            return sum(pairs[i][1] for i in indexes)

        cheap, cheapest = ranked(True, False), ranked(False, False)
        dear, dearest = ranked(True, True), ranked(False, True)
        result[str(lead)] = {
            "n": len(pairs),
            "abs": round(sum(abs(error) for error in errors), 3),
            "bias": round(sum(errors), 3),
            "max": round(max(abs(error) for error in errors), 3),
            "cheap": len(set(cheap) & set(cheapest)),
            "cheap_miss": round((real_sum(cheap) - real_sum(cheapest)) / count, 3),
            "dear": len(set(dear) & set(dearest)),
            "dear_miss": round((real_sum(dearest) - real_sum(dear)) / count, 3),
        }
    return result


def rules_of_entries(entries: list[dict]) -> dict[str, dict[str, float]]:
    """Charging history entries of a day per rule: charged kWh (c) and cost (cc), discharged kWh (d),
    their value (v: what the house did not buy + sales) and the savings (s)."""
    result: dict[str, dict[str, float]] = {}

    def add(rule: str, share: float, **values: float) -> None:
        bucket = result.setdefault(rule, {"c": 0.0, "cc": 0.0, "d": 0.0, "v": 0.0, "s": 0.0})
        for key, value in values.items():
            bucket[key] += value * share

    for entry in entries:
        rules = {key for key, value in (entry.get("rules") or {}).items() if value is True}
        charged = entry.get("charged") if isinstance(entry.get("charged"), dict) else {}
        discharged = entry.get("discharged") if isinstance(entry.get("discharged"), dict) else {}
        prices = entry.get("prices") if isinstance(entry.get("prices"), dict) else {}
        c_kwh = _num(charged.get("kWh"))
        if c_kwh > 0.0:
            cost = _num(charged.get("cost"))
            local = min(_num(charged.get("kWh_from_local_energy")), c_kwh)
            grid = c_kwh - local
            # the cost split like the charge price: solar at the sell price, the rest at the buy price
            weight_local = local * max(_num(prices.get("sell_price")), 0.0)
            weight_grid = grid * max(_num(prices.get("buy_price")), 0.0)
            local_cost = cost * weight_local / (weight_local + weight_grid) if weight_local + weight_grid > 0 else cost * local / c_kwh
            if local > 0.0:
                add("solar", 1.0, c=local, cc=local_cost)
            if grid > 0.0:
                charge_rules = [rule for rule in CHARGE_RULES if rule in rules] or ["no_rule"]
                for rule in charge_rules:
                    add(rule, 1 / len(charge_rules), c=grid, cc=cost - local_cost)
        d_kwh = _num(discharged.get("kWh"))
        if d_kwh > 0.0:
            savings = _num(discharged.get("savings"))
            value = savings + _num(discharged.get("cost"))
            sale_rules = [rule for rule in SALE_RULES if rule in rules] or (["force_discharge"] if "force_discharge" in rules else ["discharging"])
            for rule in sale_rules:
                add(rule, 1 / len(sale_rules), d=d_kwh, v=value, s=savings)
    return {rule: {key: round(value, 3) for key, value in bucket.items()} for rule, bucket in result.items()}
