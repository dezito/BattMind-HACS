"""Helpers of the charging plan and the overview."""
from __future__ import annotations

import datetime
import re

from ...utils.formatting import emoji_parse
from ...utils.helpers import reset_time_to_hour


def _optimize_solar_blocking(hours, delta, available, prices, candidates, start_level, minimum, maximum):
    """Pick the hours where solar charging is blocked (the surplus is sold instead).

    The battery has to end at least as high as without blocking and may not run
    empty more than without blocking, so the surplus is sold in the expensive hours
    and the battery is filled from the cheapest solar hours.
    hours: ordered hours of the day, delta: {hour: % change without solar},
    available: {hour: % solar surplus}, prices: {hour: sell price per kWh}.
    Returns (blocked hours, {hour: [level before, change]}, {hour: % charged from solar},
    {hour: % charged from solar without blocking}).
    """
    eps = 0.000001

    def simulate(blocked):
        level, flow, charged, shortage = start_level, {}, {}, 0.0
        for h in hours:
            before = level
            level += delta.get(h, 0.0)
            solar = 0.0 if h in blocked else available.get(h, 0.0)
            charged[h] = max(min(solar, maximum - level), 0.0)
            level = min(level + charged[h], maximum)
            if level < minimum:
                shortage += minimum - level
                level = minimum
            flow[h] = [before, level - before]
        return flow, charged, level, shortage

    original_flow, original_charged, target_level, target_shortage = simulate(set())

    def evaluate(blocked):
        flow, charged, level, shortage = simulate(blocked)
        if level < target_level - eps or shortage > target_shortage + eps:
            return None
        return sum(charged[h] * prices.get(h, 0.0) for h in charged), flow, charged

    def better(a, b):
        # cheaper charging wins, with equal cost the fewest blocked hours
        return a[0] < b[0] - eps or (abs(a[0] - b[0]) <= eps and len(a[1]) < len(b[1]))

    cost, flow, charged = evaluate(set())
    best = (cost, frozenset(), flow, charged)

    # Fill from the cheapest hours first: block everything, unblock from the cheapest
    # until the battery reaches the same level again
    blocked = set(candidates)
    for h in sorted(candidates, key=lambda h: (prices.get(h, 0.0), h)):
        if evaluate(blocked) is not None:
            break
        blocked.discard(h)
    if (result := evaluate(blocked)) is not None and better((result[0], blocked), best):
        best = (result[0], frozenset(blocked), result[1], result[2])

    # Local search: block/unblock one hour, swap two or block two at once
    for _ in range(100):
        current = best[1]
        free = [h for h in candidates if h not in current]
        moves = [current ^ {h} for h in candidates]
        moves += [(current - {a}) | {b} for a in current for b in free]
        moves += [current | {a, b} for i, a in enumerate(free) for b in free[i + 1:]]
        improved = None
        for test in moves:
            result = evaluate(test)
            if result is None:
                continue
            candidate = (result[0], frozenset(test), result[1], result[2])
            if better(candidate, improved or best):
                improved = candidate
        if improved is None:
            break
        best = improved

    if not best[1]:
        return set(), original_flow, original_charged, original_charged
    return set(best[1]), best[2], best[3], original_charged


_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


_TIME = re.compile(r"\d{2}:\d{2}")


_OTHER_DAY = re.compile(r"<br><center>.*?</center>", re.S)


def _summary(reason: str) -> tuple[str, list[str], str, str] | None:
    """(title without the values, the values in (), other day text, body) of a <details> reason."""
    match = re.search(r"<summary>(.*?)</summary>(.*)</details>\s*$", str(reason), re.S)
    if not match:
        return None
    summary, body = match.groups()
    other_day = "".join(_OTHER_DAY.findall(summary))
    summary = _OTHER_DAY.sub("", summary)
    values = re.findall(r"\(([^()]*)\)", summary)
    title = re.sub(r"\s+", " ", re.sub(r"\([^()]*\)|<br>", " ", summary)).strip()
    return title, values, other_day, body


def _group_hour_rules(rows: list[tuple[datetime.datetime, str]]) -> list[list[tuple[datetime.datetime, str]]]:
    """Consecutive hours with the same rule (title of the reason) grouped."""
    groups: list[list[tuple[datetime.datetime, str]]] = []
    for timestamp, reason in rows:
        parts = _summary(reason)
        if groups and parts:
            last_timestamp, last_reason = groups[-1][-1]
            last_parts = _summary(last_reason)
            consecutive = reset_time_to_hour(timestamp) - reset_time_to_hour(last_timestamp) == datetime.timedelta(hours=1)
            if consecutive and last_parts and last_parts[0] == parts[0] and last_parts[2] == parts[2]:
                groups[-1].append((timestamp, reason))
                continue
        groups.append([(timestamp, reason)])
    return groups


def _average_values(values: list[list[str]]) -> list[str]:
    """Average every number of the () values when all hours have the same layout, else the first hour's."""
    templates = {tuple(_NUMBER.sub("#", value) for value in hour) for hour in values}
    if len(templates) != 1:
        return values[0]
    averaged = []
    for position, text in enumerate(values[0]):
        column = [hour[position] for hour in values]
        if _TIME.search(text):
            # clock times are not averaged: the same time, else first-last
            averaged.append(text if len(set(column)) == 1 else f"{column[0]}-{column[-1]}")
            continue
        numbers = [[float(n) for n in _NUMBER.findall(hour[position])] for hour in values]
        decimals = [len(n.split(".")[1]) if "." in n else 0 for n in _NUMBER.findall(text)]
        means = iter(f"{sum(column) / len(column):.{decimal}f}" for column, decimal in zip(zip(*numbers), decimals))
        averaged.append(_NUMBER.sub(lambda _match: next(means), text))
    return averaged


def _joined_reason(group: list[tuple[datetime.datetime, str]]) -> str:
    """One reason for the hours of a group: the title with the average values, each hour inside."""
    parts = [_summary(reason) for _, reason in group]
    title, _, other_day, _ = parts[0]
    values = "".join(f" ({value})" for value in _average_values([part[1] for part in parts]))
    hours = "".join(
        f"<details><summary>{timestamp.strftime('%H:%M')}{''.join(f' ({value})' for value in part[1])}</summary>{part[3]}</details>"
        for (timestamp, _), part in zip(group, parts)
    )
    return f"<details><summary>{emoji_parse({'bundle': True})}{title}{values}{other_day}</summary>{hours}</details>"


def _join_timestamps_by_hour(timestamps: dict, valuta: str, total_label: str, periods_in_hour=None) -> dict:
    """Sell periods of the same hour and title in one reason (📦)."""
    grouped: dict = {}
    for timestamp, data in timestamps.items():
        timestamp_dt = datetime.datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else timestamp
        timestamp_dt = timestamp_dt.replace(tzinfo=None)
        title = data.get("title", "")
        grouped.setdefault((timestamp_dt.replace(minute=0, second=0, microsecond=0), title), []).append({
            "timestamp": timestamp_dt,
            "title": title,
            "emoji": data.get("emoji", ""),
            "kwh": data.get("kwh", 0.0),
            "profit": data.get("profit", 0.0),
            "reason": data.get("reason", ""),
        })
    joined = {}
    for periods in grouped.values():
        periods = sorted(periods, key=lambda p: p["timestamp"])
        first = periods[0]
        period_count = max(int(periods_in_hour(first["timestamp"])) if periods_in_hour else 1, 1)
        period_end = periods[-1]["timestamp"] + datetime.timedelta(minutes=60 / period_count)
        if len(periods) == 1:
            joined[first["timestamp"]] = {
                **{k: first[k] for k in ("title", "emoji", "kwh", "profit", "reason")},
                "period_end": period_end,
            }
            continue
        total_kwh = 0.0
        total_profit = 0.0
        period_details = []
        for period in periods:
            total_kwh += period["kwh"]
            total_profit += period["profit"]
            detail = period["reason"]
            summary_end = detail.find("</summary>")
            if summary_end != -1:
                detail = detail[summary_end + len("</summary>"):]
            if detail.endswith("</details>"):
                detail = detail[: -len("</details>")]
            period_details.append(
                f"<details><summary>{first['emoji']}**{period['timestamp'].strftime('%H:%M')}:** "
                f"{period['kwh']:.2f} kWh / {period['profit']:.2f} {valuta}</summary>{detail}</details>"
            )
        joined[first["timestamp"]] = {
            "title": first["title"],
            "kwh": total_kwh,
            "profit": total_profit,
            "period_end": period_end,
            "reason": (
                f"<details><summary>{emoji_parse({'bundle': True})}{first['emoji']}{first['title']}<br>"
                f"({total_kwh:.1f} kWh / {total_profit:.2f} {valuta})</summary>{''.join(period_details)}"
                f"<br>**{total_label}:** {total_kwh:.2f} kWh / {total_profit:.2f} {valuta}</details>"
            ),
        }
    return joined


def _running_battery_price(start_kwh: float, start_price: float, steps: list[tuple[float, float, float, float]]) -> float:
    """kWh price of the energy in the battery after the steps (weighted average cost, like a stock).

    steps: (change of the content in kWh, grid kWh charged, grid price, solar price) per hour. What comes
    in costs the grid price (the grid part) or the solar price (the rest); what goes out leaves at the
    average price, so the price of what is left doesn't change.
    """
    kwh, cost = start_kwh, start_kwh * start_price
    for change, grid_kwh, grid_price, solar_price in steps:
        if change < 0.0:
            removed = min(-change, kwh)
            if kwh > 0.0:
                cost -= cost / kwh * removed
            kwh -= removed
        elif change > 0.0:
            from_grid = min(grid_kwh, change)
            cost += from_grid * grid_price + (change - from_grid) * solar_price
            kwh += change
    return cost / kwh if kwh > 0.0 else start_price
