"""Replay a debug export (custom_components/battmind/debug_export.py) in the engine.

    python -m tests.debug_replay battmind_debug_20261025_120000.json.gz [snapshot index | failed]

The engine gets the exported configuration and runtime state (databases, plan, caches) and
runs the job of a recorded snapshot again (default: the failed job, else the last one), so
the situation of the user can be debugged without their Home Assistant.
"""
from __future__ import annotations

import datetime
import gzip
import json
import sys
from typing import Any
from zoneinfo import ZoneInfo

from custom_components.battmind.codec import decode
from custom_components.battmind.engine import BattMindEngine, JobResult, Snapshot
from custom_components.battmind.formatting import load_texts

# engine attributes that are rebuilt from the configuration instead of the export
_REBUILT = {"name", "title", "testing", "max_kwh_charging", "max_kwh_discharging", "price_provider"}


def load_export(path: str) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as file:
        return json.load(file)


def _restore(obj: Any) -> Any:
    """Undo the debug encoding ({"__date__"} dates) and then the database encoding (codec)."""
    def dates(value: Any) -> Any:
        if isinstance(value, dict):
            if len(value) == 1 and "__date__" in value:
                return datetime.date.fromisoformat(value["__date__"])
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
    config = export["config"]
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
    if which in (None, "failed") and export.get("failed_job"):
        return export["failed_job"]
    snapshots = export.get("snapshots") or []
    if not snapshots:
        raise ValueError("The export has no snapshots (no job ran before the export)")
    return snapshots[int(which) if which not in (None, "failed") else -1]


def replay(export: dict[str, Any], which: str | None = None, engine: BattMindEngine | None = None) -> JobResult:
    """Run the job of a recorded snapshot again. Price data of any snapshot is loaded first."""
    engine = engine or build_engine(export)
    item = pick(export, which)
    priced = [s for s in (*(export.get("snapshots") or []), item) if s["snapshot"].get("price_data")]
    if priced and item["job"] != "update_grid_prices":
        engine.run_job(build_snapshot(priced[-1]), engine.update_grid_prices)
    args = [_restore(arg) for arg in item.get("args") or []]
    return engine.run_job(build_snapshot(item), getattr(engine, item["job"]), *args)


def main() -> None:
    export = load_export(sys.argv[1])
    item = pick(export, sys.argv[2] if len(sys.argv) > 2 else None)
    print(f"BattMind {export['version']} (HA {export['ha_version']}), exported {export['created']}")
    print(f"Replaying {item['job']} at {item['snapshot']['now']} (recorded error: {item.get('error')})")
    result = replay(export, sys.argv[2] if len(sys.argv) > 2 else None)
    print(f"Result: {result.value!r}, error: {result.error!r}, {result.duration:.2f}s")
    print(result.states.get("current_charging_rule", ""))


if __name__ == "__main__":
    main()
