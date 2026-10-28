"""Small pure helpers (time, statistics, dict paths). Safe to use in the worker thread.

All datetimes in the engine are naive local time.
"""
from __future__ import annotations

import calendar
import datetime
import logging
import math

from dateutil import parser as dateutil_parser

_LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #

def to_datetime(value):
    """Parse an ISO string (or pass a datetime through) and drop the timezone."""
    if isinstance(value, datetime.datetime):
        return value.replace(tzinfo=None)
    try:
        return dateutil_parser.isoparse(value).replace(tzinfo=None)
    except (TypeError, ValueError):
        return value


def reset_time_to_hour(date: datetime.datetime) -> datetime.datetime:
    return date.replace(minute=0, second=0, microsecond=0, tzinfo=None)


def start_of_day(date: datetime.datetime) -> datetime.datetime:
    return datetime.datetime(date.year, date.month, date.day)


def end_of_day(date: datetime.datetime) -> datetime.datetime:
    return datetime.datetime(date.year, date.month, date.day, 23, 59, 59)


def month_first_day(date: datetime.datetime) -> datetime.datetime:
    return datetime.datetime(date.year, date.month, 1)


def day_of_week(date: datetime.datetime) -> int:
    return date.weekday()


def day_of_week_text(date: datetime.datetime) -> str:
    """'monday' ... 'sunday' (used as translation key)."""
    return calendar.day_name[date.weekday()].lower()


def date_to_string(date: datetime.datetime, fmt: str) -> str:
    return date.strftime(fmt)


def months_between(start: datetime.datetime, end: datetime.datetime) -> int:
    """Calendar months between the months of start and end."""
    return (end.year - start.year) * 12 + (end.month - start.month)


def days_between(start: datetime.datetime, end: datetime.datetime) -> int:
    """Calendar days between the dates of start and end."""
    return (end.date() - start.date()).days


def hours_between(start: datetime.datetime, end: datetime.datetime) -> int:
    """Whole hours between the hours of start and end."""
    return int((reset_time_to_hour(end) - reset_time_to_hour(start)).total_seconds() // 3600)


def minutes_between(start: datetime.datetime, end: datetime.datetime) -> int:
    """Whole minutes between the minutes of start and end."""
    start = start.replace(second=0, microsecond=0, tzinfo=None)
    end = end.replace(second=0, microsecond=0, tzinfo=None)
    return int((end - start).total_seconds() // 60)


def hour_in(timestamp: datetime.datetime, timestamps) -> bool:
    """True if any of timestamps (iterable or dict keys) has the same hour of day."""
    hour = timestamp.hour
    return any(item.hour == hour for item in timestamps)


def in_between(check, start, end) -> bool:
    """start <= check < end. For datetimes end < start means the range wraps midnight."""
    if isinstance(check, datetime.datetime) and end < start:
        return start <= check or check < end
    return start <= check < end


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #

def round_up(n: float, decimals: int = 0) -> float:
    multiplier = 10**decimals
    return float(math.ceil(max(n, 0.0) * multiplier) / multiplier)


def average(data) -> float:
    if not isinstance(data, list):
        data = [data]
    return sum(data) / len(data) if data else 0.0


def calculate_ema(data: list[float], span: int = 5) -> float:
    """Exponential moving average, oldest value first."""
    if not data:
        return 0.0
    alpha = 2 / (span + 1)
    ema = data[0]
    for value in data:
        ema = value * alpha + ema * (1 - alpha)
    return ema


def calculate_trend(data: list[float]) -> float:
    """Linear regression extrapolated one step ahead, oldest value first."""
    n = len(data)
    if n == 0:
        return 0.0
    sum_x = n * (n - 1) / 2
    sum_x_squared = sum(i * i for i in range(n))
    sum_y = sum(data)
    sum_xy = sum(i * value for i, value in enumerate(data))
    denominator = n * sum_x_squared - sum_x**2
    if denominator == 0:
        return data[-1]
    slope = (n * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / n
    return intercept + slope * n


def reverse_list(lst):
    return lst[::-1] if isinstance(lst, list) else lst


def get_specific_values(values, positive_only: bool = False, negative_only: bool = False) -> list[float]:
    """Values with the other sign replaced by 0.0 (non numbers are skipped)."""
    if positive_only == negative_only:
        raise ValueError("Exactly one of positive_only or negative_only must be True")
    result = []
    for value in values:
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        wrong_sign = value > 0.0 if negative_only else value < 0.0
        result.append(0.0 if wrong_sign else value)
    return result or [0.0]


def get_closest_key(input_value, collection: dict, return_key: bool = False):
    """Value (or key) of the dict key numerically closest to input_value. First key wins ties."""
    if not collection:
        return None if return_key else []
    input_value = float(input_value)
    closest = min(collection, key=lambda key: abs(float(key) - input_value))
    return closest if return_key else collection[closest]


def limit_dict_size(dct: dict, size: int) -> dict:
    """Keep the newest (last inserted) size items."""
    return dict(list(dct.items())[-size:])


def check_next_24_hours_diff(dict1: dict, dict2: dict, now: datetime.datetime) -> bool:
    """True if the datetime keys within the next 24 hours differ."""
    end_time = now + datetime.timedelta(hours=24)
    keys1 = {k for k in dict1 if isinstance(k, datetime.datetime) and now <= k < end_time}
    keys2 = {k for k in dict2 if isinstance(k, datetime.datetime) and now <= k < end_time}
    return keys1 != keys2


def get_dict_value_with_path(d: dict, path: str, default=None):
    current = d
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def set_dict_value_with_path(d: dict, path: str, value) -> dict:
    keys = path.split(".")
    current = d
    for key in keys[:-1]:
        if not isinstance(current.get(key), dict):
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value
    return d


def float_or_none(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
