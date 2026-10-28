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


def test_recorder_tariff_history_preserves_sorted_timestamped_attributes() -> None:
    start = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    later_tariffs = {"additional_tariffs": {"elafgift": 0.8}, "tariffs": {"0": 0.3}}
    earlier_tariffs = {"additional_tariffs": {"elafgift": 0.5}, "tariffs": {"0": 0.2}}
    states = {
        "sensor.eds_sell": [
            SimpleNamespace(
                last_updated=start + datetime.timedelta(hours=2),
                attributes={"tariffs": later_tariffs},
            ),
            SimpleNamespace(
                last_updated=start,
                attributes={"tariffs": earlier_tariffs},
            ),
        ]
    }

    result = CoordinatorData._recorder_tariff_history(states)

    assert result["sensor.eds_sell"] == [
        (start.timestamp(), earlier_tariffs),
        ((start + datetime.timedelta(hours=2)).timestamp(), later_tariffs),
    ]
