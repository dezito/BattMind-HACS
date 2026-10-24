"""The generated dashboard: every language, every setting, entity names without the device name."""
from __future__ import annotations

import pytest
import yaml

from custom_components.battmind.const import LANGUAGES, NUMBER_SETTINGS, SELECT_SETTINGS, SWITCH_SETTINGS, TEXT_SETTINGS, build_config
from custom_components.battmind.dashboard import render_dashboard

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
    assert yaml.safe_load(rendered("da"))["views"][1]["title"] == "Indstillinger"
    assert yaml.safe_load(rendered("en"))["views"][1]["title"] == "Settings"


@pytest.mark.parametrize(("days", "span"), [(3, "2.5d"), (2, "1.5d"), (7, "6.5d")])
def test_chart_span_follows_days_to_predict(days: int, span: str) -> None:
    content = yaml.safe_load(render_dashboard(build_config({**OPTIONS, "days_to_predict": days}), "battmind"))
    # the analysis charts show fixed periods (months / the last 31 days)
    spans = [
        value for node in walk(content) for key, value in node.items()
        if key in ("graph_span", "selection_span") and "d" in str(value) and value not in ("400d", "31d")
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
        if "data_generator" in node or str(node.get("type", "")).startswith("custom:"):
            continue  # apexcharts series / mushroom cards have their own name / primary_info
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
