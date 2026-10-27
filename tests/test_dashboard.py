"""The generated dashboard: every language, every setting, entity names without the device name."""
from __future__ import annotations

import pytest
import yaml

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.battmind.const import LANGUAGES, NUMBER_SETTINGS, SELECT_SETTINGS, SWITCH_SETTINGS, TEXT_SETTINGS, build_config
from custom_components.battmind.dashboard import render_dashboard
from custom_components.battmind.formatting import load_texts

from .conftest import MOCK_OPTIONS, PREFIX, async_unload_and_join, set_source_states
from .test_engine import OPTIONS


def rendered(language: str, prefix: str = "battmind") -> str:
    return render_dashboard(build_config({**OPTIONS, "language": language}), prefix)


def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


@pytest.mark.parametrize("language", list(LANGUAGES))
def test_every_language_renders_without_placeholders(language: str) -> None:
    content = rendered(language)
    assert "@@" not in content
    views = yaml.safe_load(content)["views"]
    assert [view["path"] for view in views] == ["overview", "settings", "analysis"]


def test_texts_follow_the_language() -> None:
    da = yaml.safe_load(rendered("da"))
    en = yaml.safe_load(rendered("en"))
    assert da["views"][1]["title"] == "Indstillinger"
    assert en["views"][1]["title"] == "Settings"
    assert da["views"][0]["sections"][0]["cards"][0]["header"]["title"] == "Husbatteri SOC prognose"
    assert en["views"][0]["sections"][0]["cards"][0]["header"]["title"] == "Home battery SOC forecast"


def test_currency_follows_home_assistant() -> None:
    texts = load_texts("en", "DKK")
    assert (texts["ui.common.valuta"], texts["ui.common.valuta_kwh"]) == ("kr", "kr/kWh")
    assert texts["dashboard.price_chart_title"].endswith("(kr/kWh)")
    assert load_texts("da", "EUR")["ui.common.valuta_kwh"] == "€/kWh"
    assert load_texts("da", "CHF")["ui.common.valuta"] == "CHF"
    content = render_dashboard(build_config({**OPTIONS, "language": "da"}), "battmind", currency="EUR")
    assert "€/kWh" in content and "kr/kWh" not in content and "{valuta" not in content


def test_every_language_has_every_text() -> None:
    """A key missing in a language (and in English, the fallback) is shown as the raw key."""
    import json
    from pathlib import Path

    def flat(data: dict, prefix: str = "") -> set[str]:
        keys: set[str] = set()
        for key, value in data.items():
            keys |= flat(value, f"{prefix}{key}.") if isinstance(value, dict) else {f"{prefix}{key}"}
        return keys

    folder = Path(__file__).parents[1] / "custom_components" / "battmind" / "texts"
    keys = {path.stem: flat(json.loads(path.read_text(encoding="utf-8"))) for path in folder.glob("*.json")}
    every = set().union(*keys.values())
    assert {language: sorted(every - found) for language, found in keys.items() if every - found} == {}


@pytest.mark.parametrize(("days", "span"), [(3, "2.5d"), (2, "1.5d"), (7, "6.5d")])
def test_chart_span_follows_days_to_predict(days: int, span: str) -> None:
    content = yaml.safe_load(render_dashboard(build_config({**OPTIONS, "days_to_predict": days}), "battmind"))
    # the analysis charts show fixed periods (months / the last 31 days)
    spans = [
        value for node in walk(content) for key, value in node.items()
        if key in ("graph_span", "selection_span") and "d" in str(value) and value not in ("400d", "31d", "6d")
    ]
    assert len(spans) == 4 and set(spans) == {span}


def test_every_setting_is_on_the_settings_view() -> None:
    settings_view = yaml.safe_load(rendered("en"))["views"][1]
    entities = {node["entity"] for node in walk(settings_view) if isinstance(node.get("entity"), str)}
    for platform, keys in (("switch", SWITCH_SETTINGS), ("number", NUMBER_SETTINGS), ("select", SELECT_SETTINGS), ("text", TEXT_SETTINGS)):
        for key in keys:
            assert f"{platform}.battmind_{key}" in entities, key


def test_own_entity_rows_show_the_entity_name() -> None:
    """name: {type: entity} - the entity name without "BattMind" in front."""
    content = yaml.safe_load(rendered("en", prefix="house"))
    for node in walk(content):
        entity = node.get("entity")
        if not isinstance(entity, str) or not entity.split(".")[-1].startswith("house_"):
            continue
        if "data_generator" in node or str(node.get("type", "")).startswith("custom:") or node.get("condition"):
            continue  # apexcharts series / custom cards / visibility conditions are not entity rows
        if entity == "button.house_enforce_planning":
            assert node.get("name") == "Force planning"  # badges: the entity name as text
            continue
        assert node.get("name") == {"type": "entity"}, entity


def _price_charts(**options) -> list[str]:
    content = yaml.safe_load(render_dashboard(build_config({**OPTIONS, **options}), "battmind"))
    return [node["series"][0]["entity"] for node in walk(content) if node.get("type") == "custom:apexcharts-card" and node["series"][0].get("yaxis_id") == "price"]


def test_one_price_chart_stromligning_preferred() -> None:
    assert _price_charts() == ["sensor.price"]
    assert _price_charts(stromligning_buy_current_price="sensor.sl_price") == ["sensor.sl_price"]


def test_every_chart_has_a_shared_non_intersecting_tooltip() -> None:
    content = yaml.safe_load(rendered("en"))
    charts = [node for node in walk(content) if node.get("type") == "custom:apexcharts-card"]
    assert charts
    for chart in charts:
        tooltip = chart["apex_config"]["tooltip"]
        assert tooltip["shared"] is True
        assert tooltip["intersect"] is False


def test_overview_plan_chart_series_share_the_battery_flow_hours() -> None:
    overview = yaml.safe_load(rendered("en"))["views"][0]
    chart = next(node for node in walk(overview) if node.get("type") == "custom:apexcharts-card" and len(node.get("series", [])) == 5)
    assert "enabledOnSeries" not in chart["apex_config"]["tooltip"]
    custom_tooltip = chart["apex_config"]["tooltip"]["custom"]
    assert "series.map((values, index)" in custom_tooltip
    assert "collapsedSeriesIndices" in custom_tooltip
    assert "values[dataPointIndex]" in custom_tooltip
    for series in chart["series"]:
        generator = series["data_generator"]
        assert "const flow = entity.attributes.battery_level_flow || [];" in generator
        assert "return flow.map(p =>" in generator
    assert all(series.get("curve") == "stepline" for series in chart["series"][1:])
    assert [series["color"] for series in chart["series"]] == ["#4CAF50", "#2196F3", "#FF9800", "#FDD835", "#F44336"]
    assert chart["series"][0]["curve"] == "straight"
    assert chart["series"][0]["opacity"] == 0.25
    assert chart["series"][1]["opacity"] == 0.6
    assert chart["series"][0]["show"]["in_brush"] is True
    assert all(series["show"]["in_brush"] is False for series in chart["series"][1:])
    assert [series.get("show", {}).get("hidden_by_default", False) for series in chart["series"]] == [False, False, False, True, False]


def test_price_overview_shows_the_battery_plan() -> None:
    for options in ({}, {"stromligning_buy_current_price": "sensor.sl_price"}):
        content = yaml.safe_load(render_dashboard(build_config({**OPTIONS, **options, "language": "en"}), "battmind"))
        chart = next(
            node for node in walk(content)
            if node.get("type") == "custom:apexcharts-card" and node.get("series", [{}])[0].get("yaxis_id") == "price"
        )
        actions = [series for series in chart["series"] if series.get("yaxis_id") == "state"]
        assert chart["apex_config"]["stroke"]["curve"] == "stepline"
        assert [series["name"] for series in actions] == ["Charge", "Force discharge"]
        assert all(series["type"] == "area" and series["curve"] == "stepline" for series in actions)
        assert all("_timestamps" in series["data_generator"] and "group_by" not in series for series in actions)
        assert all(not series.get("show", {}).get("hidden_by_default", False) for series in actions)
        assert all(series["show"]["in_brush"] is False for series in actions)
        assert chart["apex_config"]["tooltip"]["enabledOnSeries"] == [0, 1, 2, 4, 5]
        assert chart["apex_config"]["tooltip"]["followCursor"] is True
        assert chart["apex_config"]["tooltip"]["fixed"]["enabled"] is False
        custom = chart["apex_config"]["tooltip"]["custom"]
        assert "const indices = [0, 1, 2, 4, 5];" in custom
        assert custom.strip().endswith("}")
        for index in (0, 1, 2, 4, 5):
            generator = chart["series"][index]["data_generator"]
            assert "const times = [...new Set(" in generator
            assert "return times.map(time =>" in generator


def test_overview_does_not_show_raw_powerwall_action_card() -> None:
    overview = yaml.safe_load(rendered("en"))["views"][0]
    cards = [node for node in walk(overview) if node.get("type") == "custom:mushroom-entity-card"]
    assert all(card.get("entity") != "sensor.battmind_powerwall_action" for card in cards)


def test_price_forecast_chart_series_share_the_same_hours() -> None:
    content = yaml.safe_load(rendered("en"))
    chart = next(node for node in walk(content) if node.get("type") == "custom:apexcharts-card" and node.get("graph_span") == "6d")
    assert len(chart["series"]) == 9
    for series in chart["series"]:
        generator = series["data_generator"]
        assert "const hours = ((entity.attributes.price_forecast || {}).hours || []);" in generator
        assert "return hours.map(h =>" in generator
        assert ".filter(" not in generator


def test_efficiency_curve_chart_keeps_points_inside_its_time_window() -> None:
    content = yaml.safe_load(rendered("en"))
    chart = next(
        node for node in walk(content)
        if node.get("type") == "custom:apexcharts-card"
        and node.get("header", {}).get("title") == "Efficiency by AC power"
    )
    assert chart["graph_span"] == "24h"
    assert chart["span"] == {"end": "minute"}
    assert len(chart["series"]) == 2
    assert "function(value, timestamp)" in chart["apex_config"]["xaxis"]["labels"]["formatter"]
    for series in chart["series"]:
        generator = series["data_generator"]
        assert "const range = end.getTime() - start.getTime();" in generator
        assert "start.getTime() + (p.power / 10000) * range" in generator
        assert "entity.attributes.efficiency_curves" in generator

def test_solar_forecast_chart_series_share_the_same_days() -> None:
    content = yaml.safe_load(rendered("en"))
    chart = next(
        node for node in walk(content)
        if node.get("type") == "custom:apexcharts-card"
        and node.get("series", [{}])[0].get("type") == "column"
        and "entity.attributes.plans" in node.get("series", [{}])[0].get("data_generator", "")
    )
    assert len(chart["series"]) == 2
    custom_tooltip = chart["apex_config"]["tooltip"]["custom"]
    assert "w.globals.seriesNames[index]" in custom_tooltip
    assert "values[dataPointIndex]" in custom_tooltip
    for series in chart["series"]:
        generator = series["data_generator"]
        assert "const days = entity.attributes.plans || [];" in generator
        assert "return days.map(d =>" in generator
        assert ".filter(" not in generator


def test_consumption_forecast_is_aligned_and_visually_distinguishes_totals() -> None:
    content = yaml.safe_load(rendered("en"))
    chart = next(
        node for node in walk(content)
        if node.get("type") == "custom:apexcharts-card" and node.get("header", {}).get("title") == "Consumption forecast"
    )
    assert len(chart["series"]) == 6
    assert chart["apex_config"]["stroke"]["dashArray"] == [0, 0, 0, 6, 6, 6]
    assert chart["apex_config"]["legend"]["position"] == "bottom"
    assert [series["name"] for series in chart["series"][3:]] == ["Total Avg", "Total EMA", "Total Trend"]
    for series in chart["series"]:
        generator = series["data_generator"]
        assert "const keys = [...new Set([" in generator
        assert "return keys.map(ts =>" in generator


async def test_sidebar_dashboard_can_be_edited_and_reset(hass: HomeAssistant, hass_ws_client, mock_entry: MockConfigEntry, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)
    set_source_states(hass)
    entry = MockConfigEntry(domain=mock_entry.domain, title=mock_entry.title, data=dict(mock_entry.data), options={**MOCK_OPTIONS, "dashboard": True})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    client = await hass_ws_client(hass)

    async def command(message: dict) -> dict:
        await client.send_json_auto_id({"url_path": f"{PREFIX}-dashboard", **message})
        response = await client.receive_json()
        assert response["success"], response
        return response["result"]

    generated = await command({"type": "lovelace/config"})
    assert generated["views"]
    # storage mode: the frontend offers the edit UI and the raw configuration editor
    assert hass.data["frontend_panels"][f"{PREFIX}-dashboard"].config == {"mode": "storage"}

    await command({"type": "lovelace/config/save", "config": {"views": [{"title": "Mine"}]}})
    assert await command({"type": "lovelace/config"}) == {"views": [{"title": "Mine"}]}

    # the edits survive a reload, the generated dashboard is not shown over them
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert await command({"type": "lovelace/config"}) == {"views": [{"title": "Mine"}]}

    await hass.services.async_call("button", "press", {"entity_id": f"button.{PREFIX}_reset_dashboard"}, blocking=True)
    assert await command({"type": "lovelace/config"}) == generated
    await async_unload_and_join(hass, entry)
