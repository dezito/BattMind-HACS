"""Replay a debug export (custom_components/battmind/debug_export.py) in the engine.

    <venv python> tests/debug_replay.py battmind_debug_20261025_120000.json.gz [failed | planning | <index>]

(the test venv of ha-dev/run_tests_windows.ps1: %LOCALAPPDATA%\ha-dev-testsenv\Scripts\python.exe)

The engine gets the exported configuration and runtime state (databases, plan, caches) and
runs the job of a recorded snapshot again (default: the failed job, else the last planning
job, else the last one), so the situation of the user can be debugged without their Home
Assistant. <index> counts in the list of the last snapshots that is printed.
"""
from __future__ import annotations

import datetime
import gzip
import json
import os
import sys
from copy import deepcopy
from typing import Any
from zoneinfo import ZoneInfo

if __package__ in (None, ""):  # started as tests/debug_replay.py: the repo root holds custom_components
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from custom_components.battmind.codec import decode
from custom_components.battmind.const import DEFAULT_CONFIG
from custom_components.battmind.debug_export import PLANNING_JOBS
from custom_components.battmind.engine import BattMindEngine, JobResult, Snapshot
from custom_components.battmind.formatting import load_texts

# engine attributes that are rebuilt from the configuration instead of the export
_REBUILT = {"name", "title", "testing", "max_kwh_charging", "max_kwh_discharging", "price_provider"}


def load_export(path: str) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as file:
        return json.load(file)


def _restore(obj: Any) -> Any:
    """Undo the debug encoding ({"__date__"} / {"__time__"}) and then the database encoding (codec)."""
    def dates(value: Any) -> Any:
        if isinstance(value, dict):
            if len(value) == 1 and "__date__" in value:
                return datetime.date.fromisoformat(value["__date__"])
            if len(value) == 1 and "__time__" in value:
                return datetime.time.fromisoformat(value["__time__"])
            return {key: dates(item) for key, item in value.items()}
        if isinstance(value, list):
            return [dates(item) for item in value]
        return value

    return decode(dates(obj))


def _rows(series: dict[str, list]) -> dict[str, list]:
    """Buffer/statistics rows back to tuples (they are compared with tuples by bisect)."""
    return {entity_id: [tuple(row) for row in rows] for entity_id, rows in (series or {}).items()}


def build_snapshot(item: dict[str, Any]) -> Snapshot:
    data = _restore(item["snapshot"])
    data["tz"] = ZoneInfo(data["tz"])
    data["buffers"] = _rows(data.get("buffers"))
    data["statistics"] = _rows(data.get("statistics"))
    data["sun_events"] = {
        datetime.date.fromisoformat(date) if isinstance(date, str) else date: events
        for date, events in (data.get("sun_events") or {}).items()
    }
    return Snapshot(**data)


def build_engine(export: dict[str, Any]) -> BattMindEngine:
    """Engine with the exported configuration and runtime state."""
    config = deepcopy(export["config"])
    solar = config.setdefault("solar", {})
    solar.setdefault("battery_temperature_min", DEFAULT_CONFIG["solar"]["battery_temperature_min"])
    solar.setdefault("battery_temperature_max", DEFAULT_CONFIG["solar"]["battery_temperature_max"])
    engine = BattMindEngine(config, export["prefix"], load_texts(config.get("language") or "en"))
    for key, value in (export.get("engine") or {}).items():
        if key in _REBUILT or not hasattr(engine, key):
            continue
        value = _restore(value)
        current = getattr(engine, key)
        if current is None or value is None or isinstance(value, type(current)):
            setattr(engine, key, value)
    return engine


def pick(export: dict[str, Any], which: str | None = None) -> dict[str, Any]:
    """which: failed, planning, an index in the last snapshots, or None (failed, planning, last)."""
    if which in (None, "failed") and export.get("failed_job"):
        return export["failed_job"]
    if which in (None, "failed", "planning") and export.get("planning_job"):
        return export["planning_job"]
    snapshots = export.get("snapshots") or []
    if not snapshots:
        raise ValueError("The export has no snapshots (no job ran before the export)")
    if which not in (None, "failed", "planning"):
        return snapshots[int(which)]
    # exports without planning_job: the last planning job among the last snapshots
    return next((item for item in reversed(snapshots) if item["job"] in PLANNING_JOBS), snapshots[-1])


def _now(item: dict[str, Any]) -> str:
    now = item["snapshot"].get("now")
    return now.get("__dt__", now) if isinstance(now, dict) else str(now)


def replay(export: dict[str, Any], which: str | None = None, engine: BattMindEngine | None = None) -> JobResult:
    """Run the job of a recorded snapshot again. Price data of any snapshot is loaded first."""
    engine = engine or build_engine(export)
    item = pick(export, which)
    snapshot = build_snapshot(item)
    if snapshot.price_data is None:
        # prices of the newest snapshot that has them, else the prices at the time of the export
        priced = [s for s in (*(export.get("snapshots") or []), export.get("planning_job")) if s and s["snapshot"].get("price_data")]
        snapshot.price_data = _restore(priced[-1]["snapshot"]["price_data"]) if priced else _restore(export.get("price_data"))
    if snapshot.price_data and item["job"] != "update_grid_prices":
        engine.run_job(snapshot, engine.update_grid_prices)
    args = [_restore(arg) for arg in item.get("args") or []]
    return engine.run_job(snapshot, getattr(engine, item["job"]), *args)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    which = sys.argv[2] if len(sys.argv) > 2 else None
    export = load_export(sys.argv[1])
    print(f"BattMind {export['version']} (HA {export['ha_version']}), prefix {export['prefix']}, exported {export['created']}")
    for label, item in (("failed", export.get("failed_job")), ("planning", export.get("planning_job"))):
        if item:
            print(f"  {label:>8}: {item['job']} at {_now(item)} (error: {item.get('error')})")
    for index, item in enumerate(export.get("snapshots") or []):
        print(f"  {index:>8}: {item['job']} at {_now(item)} (error: {item.get('error')})")
    item = pick(export, which)
    print(f"Replaying {item['job']} at {_now(item)}")
    result = replay(export, which)
    print(f"Result: {result.value!r}, error: {result.error!r}, {result.duration:.2f}s")
    if rule := result.states.get("current_charging_rule"):
        print(rule)


if __name__ == "__main__":
    main()
