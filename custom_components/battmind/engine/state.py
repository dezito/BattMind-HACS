"""The data handed to and returned from the engine worker."""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Snapshot:
    """Plain data collected on the event loop for one worker job."""

    now: datetime.datetime
    tz: Any
    settings: dict[str, Any] = field(default_factory=dict)
    entity_states: dict[str, Any] = field(default_factory=dict)
    entity_platforms: dict[str, str | None] = field(default_factory=dict)
    units: dict[str, str | None] = field(default_factory=dict)
    buffers: dict[str, list] = field(default_factory=dict)
    statistics: dict[str, list] = field(default_factory=dict)
    price_data: dict | None = None
    forecast_dict: dict = field(default_factory=lambda: {"hourly": [], "daily": []})
    solcast: dict[str, list] = field(default_factory=dict)
    sun_events: dict = field(default_factory=dict)
    # Cable Juice Planner cars: {"name", "charge_plan"}; charge_plan None = no plan available
    ev_charging: list[dict] = field(default_factory=list)


@dataclass
class JobResult:
    """Everything a worker job wants the event loop to publish."""

    states: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, dict[str, Any]] = field(default_factory=dict)
    notifications: list[tuple[str, str, str]] = field(default_factory=list)
    db_saves: dict[str, Any] = field(default_factory=dict)
    solar_kwh_increment: float = 0.0
    value: Any = None
    error: BaseException | None = None
    request_reload: bool = False
    worker_thread: str = ""
    duration: float = 0.0
