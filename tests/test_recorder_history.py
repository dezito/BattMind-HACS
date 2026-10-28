"""Recorder-state fallbacks for hourly what-if data."""
from __future__ import annotations

import datetime
from types import SimpleNamespace

from custom_components.battmind.coordinator.data import CoordinatorData


def test_recorder_hourly_means_uses_time_weighted_values() -> None:
    start = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    states = {
        "sensor.power": [
            SimpleNamespace(last_updated=start, state="1000"),
            SimpleNamespace(last_updated=start + datetime.timedelta(minutes=30), state="3000"),
            SimpleNamespace(last_updated=start + datetime.timedelta(hours=1), state="2000"),
        ]
    }

    result = CoordinatorData._recorder_hourly_means(
        states, start, start + datetime.timedelta(hours=2)
    )

    assert result["sensor.power"] == [
        (start.timestamp(), 2000.0),
        ((start + datetime.timedelta(hours=1)).timestamp(), 2000.0),
    ]


def test_recorder_hourly_means_rejects_unknown_periods() -> None:
    start = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    states = {
        "sensor.power": [
            SimpleNamespace(last_updated=start, state="1000"),
            SimpleNamespace(last_updated=start + datetime.timedelta(minutes=30), state="unavailable"),
            SimpleNamespace(last_updated=start + datetime.timedelta(minutes=45), state="3000"),
        ]
    }

    result = CoordinatorData._recorder_hourly_means(
        states, start, start + datetime.timedelta(hours=1)
    )

    assert result["sensor.power"] == []


def test_what_if_prefers_statistics_and_fills_only_missing_hours() -> None:
    start = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    stats = {
        "sensor.power": [
            {"start": start, "mean": 1200.0, "state": None},
        ]
    }
    recorder = {
        "sensor.power": [
            (start.timestamp(), 1000.0),
            ((start + datetime.timedelta(hours=1)).timestamp(), 2000.0),
        ]
    }

    combined, statistical_timestamps = CoordinatorData._combine_what_if_statistics(
        {"sensor.power"}, stats, recorder
    )

    assert combined["sensor.power"] == [
        (start.timestamp(), 1200.0),
        ((start + datetime.timedelta(hours=1)).timestamp(), 2000.0),
    ]
    assert statistical_timestamps == {"sensor.power": [start.timestamp()]}
