"""Window calculations on buffered recorder data.

The PyScript version asked the recorder for every window it needed (70+ queries per
planning run, running on the event loop). The integration keeps a rolling buffer of
states and hourly statistics per entity instead, and this module reproduces the old
``history.get_values`` / ``history.get_average_value`` semantics from those buffers:

* short term: states inside the window plus the state at the window start, linearly
  interpolated to 100 points (``include_start_time_state=True`` + ``np.interp``)
* long term: hourly mean statistics for the window, interpolated to 100 points
* average = mean of both point sets

All functions are pure and run in the worker thread.
"""
from __future__ import annotations

import bisect
import datetime
from typing import Iterable

NUM_POINTS = 100

_POWER_FACTORS_TO_W = {
    "mW": 0.001,
    "W": 1.0,
    "kW": 1000.0,
    "MW": 1000000.0,
    "GW": 1000000000.0,
}


def unit_factor_to_w(unit: str | None) -> float:
    """Factor converting a power unit (kWh is treated as kW like power_convert) to W."""
    if not unit:
        return 1.0
    return _POWER_FACTORS_TO_W.get(unit.replace("h", ""), 1.0)


def _interp(points: list[tuple[float, float]], start_ts: float, end_ts: float, num: int = NUM_POINTS) -> list[float]:
    """numpy.interp(linspace(start, end, num), xs, ys) without numpy."""
    if not points:
        return []
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    out = []
    span = end_ts - start_ts
    last = len(xs) - 1
    for i in range(num):
        x = start_ts + span * i / (num - 1) if num > 1 else start_ts
        if x <= xs[0]:
            out.append(ys[0])
            continue
        if x >= xs[last]:
            out.append(ys[last])
            continue
        j = bisect.bisect_right(xs, x) - 1
        x0, x1 = xs[j], xs[j + 1]
        y0, y1 = ys[j], ys[j + 1]
        out.append(y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0))
    return out


class WindowCalculator:
    """Calculate window values from state/statistics buffers.

    buffers:    entity_id -> sorted list of (epoch_ts, float|None)
    statistics: entity_id -> sorted list of (hour_start_epoch_ts, mean)
    units:      entity_id -> unit_of_measurement
    """

    def __init__(self, buffers, statistics, units, tz) -> None:
        self.buffers = buffers or {}
        self.statistics = statistics or {}
        self.units = units or {}
        self.tz = tz
        self._key_cache: dict[tuple[str, str], list[float]] = {}

    def _keys(self, kind: str, entity_id: str, rows: list) -> list[float]:
        cache_key = (kind, entity_id)
        keys = self._key_cache.get(cache_key)
        if keys is None:
            keys = [r[0] for r in rows]
            self._key_cache[cache_key] = keys
        return keys

    def _ts(self, naive: datetime.datetime) -> float:
        return naive.replace(tzinfo=self.tz).timestamp()

    def _short_points(self, entity_id: str, start_ts: float, end_ts: float) -> list[tuple[float, float]]:
        rows = self.buffers.get(entity_id)
        if not rows:
            return []
        keys = self._keys("short", entity_id, rows)
        # state at the window start (latest row at or before start) placed at start,
        # followed by every row strictly inside the window
        lo = bisect.bisect_right(keys, start_ts)
        hi = bisect.bisect_left(keys, end_ts)
        points: dict[float, float | None] = {}
        if lo > 0:
            points[start_ts] = rows[lo - 1][1]
        for ts, value in rows[lo:hi]:
            points[ts] = value
        return sorted((ts, v) for ts, v in points.items() if v is not None)

    def short_values(self, entity_id: str, start: datetime.datetime, end: datetime.datetime) -> list[float]:
        """Equivalent of history.get_values(float_type=True, convert_to='W')."""
        if not entity_id:
            return []
        start, end = min(start, end), max(start, end)
        start_ts, end_ts = self._ts(start), self._ts(end)
        factor = unit_factor_to_w(self.units.get(entity_id))
        return [v * factor for v in _interp(self._short_points(entity_id, start_ts, end_ts), start_ts, end_ts)]

    def long_values(self, entity_id: str, start: datetime.datetime, end: datetime.datetime) -> list[float]:
        """Equivalent of history.get_longterm_values(state_type='mean', convert_to='W')."""
        rows = self.statistics.get(entity_id)
        if not entity_id or not rows:
            return []
        start, end = min(start, end), max(start, end)
        window_start = start.replace(minute=0, second=0, microsecond=0)
        window_end = end.replace(minute=0, second=0, microsecond=0)
        if window_start == window_end:
            window_end = window_start + datetime.timedelta(hours=1)
        start_ts, end_ts = self._ts(window_start), self._ts(window_end)
        keys = self._keys("long", entity_id, rows)
        lo = bisect.bisect_left(keys, start_ts)
        hi = bisect.bisect_left(keys, end_ts)
        points = [(ts, v) for ts, v in rows[lo:hi] if v is not None]
        factor = unit_factor_to_w(self.units.get(entity_id))
        return [v * factor for v in _interp(points, start_ts, end_ts)]

    def average(self, entity_id: str, start: datetime.datetime, end: datetime.datetime, error_state=0.0):
        """Equivalent of history.get_average_value(convert_to='W')."""
        if not entity_id:
            return error_state
        values = self.short_values(entity_id, start, end) + self.long_values(entity_id, start, end)
        if not values:
            return error_state
        return sum(values) / len(values)

    def last_numeric(self, entity_id: str):
        rows = self.buffers.get(entity_id) or []
        for _, value in reversed(rows):
            if value is not None:
                return value
        return None


def merge_rows(existing: list, new_rows: Iterable) -> list:
    """Merge rows (ts, value) keeping sort order and dropping duplicate timestamps."""
    merged = {ts: value for ts, value in existing}
    for ts, value in new_rows:
        merged[ts] = value
    return sorted(merged.items())
