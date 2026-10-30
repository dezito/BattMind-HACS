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


async def test_efficiency_backfill_reads_the_battery_history_a_day_at_a_time(monkeypatch) -> None:
    import homeassistant.components.recorder as recorder
    from homeassistant.components.recorder import history

    from custom_components.battmind.utils.efficiency import EFFICIENCY_BACKFILL_DAYS

    calls = []

    def significant_states(hass, start, end, entities, _filters, include_start_time_state, **_kwargs):
        calls.append((start, end, include_start_time_state))
        return {
            "sensor.battery_power": [SimpleNamespace(last_updated=start, state="-3000"), {"state": "minimal"}],
            "sensor.battery_level": [SimpleNamespace(last_updated=start, state="50")],
        }

    class Instance:
        async def async_add_executor_job(self, job):
            return job()

    monkeypatch.setattr(recorder, "get_instance", lambda hass: Instance())
    monkeypatch.setattr(history, "get_significant_states", significant_states)
    jobs = []

    async def run_job(name, data):
        jobs.append((name, data))

    coordinator = SimpleNamespace(
        hass=SimpleNamespace(config=SimpleNamespace(components={"recorder"})),
        config={"home": {"entity_ids": {"powerwall_watt_flow_entity_id": "sensor.battery_power", "powerwall_battery_level_entity_id": "sensor.battery_level"}}},
        async_run_job=run_job,
    )

    await CoordinatorData._async_efficiency_backfill(coordinator)

    assert len(calls) in (EFFICIENCY_BACKFILL_DAYS, EFFICIENCY_BACKFILL_DAYS + 1)
    assert [call[2] for call in calls[:2]] == [True, False]
    assert all(earlier[1] == later[0] for earlier, later in zip(calls, calls[1:]))
    [(name, data)] = jobs
    assert name == "analysis_efficiency_backfill"
    assert len(data["flow"]) == len(calls) and data["flow"][0] == (calls[0][0].timestamp(), -3000.0)
    assert data["level"] == sorted(data["level"])
