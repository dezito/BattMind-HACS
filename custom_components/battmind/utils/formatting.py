"""Emoji and text formatting helpers shared by the engine modules."""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..const import CHARGING_TYPES, currency_symbol

_HELP_ITEM = re.compile(r"^(?P<indent>\s*)[*•-]\s+(?P<label>.+?):\s*(?P<description>.*)$")
_ANALYSIS_HELP_KEYS = {"cycles_explain", "forecast_explain", "loss_explain", "rules_explain", "score_explain"}
_ABBREVIATIONS = re.compile(
    r"\b(?:[a-zæøåäöü]\.)+[a-zæøåäöü]\.|\b(?:avg|bl|ca|dvs|gns|inkl|kl|max|min|nr|pr)\.",
    re.IGNORECASE,
)


def format_help_text(text: str) -> str:
    """Render labelled help items as spaced bold-italic labels and descriptions."""
    formatted = []
    for line in text.splitlines():
        match = _HELP_ITEM.match(line)
        if match is None:
            formatted.append(line)
            continue

        if formatted and formatted[-1].strip():
            formatted.append("")
        label = match.group("label").strip()
        if label.startswith("**") and label.endswith("**"):
            label = label[2:-2].strip()
        formatted.extend((f"***{label}***", match.group("description")))

    text = "\n".join(formatted).strip()
    text = re.sub(r"(?<=\S) (?=\*\*[^*\n]+\*\*:)", "\n\n", text)
    return re.sub(r"\*\*([^*\n]+)\*\*:\s*", r"***\1***\n", text)


def format_analysis_help_text(text: str) -> str:
    """Put analysis sentences and colon-separated explanations on separate lines."""
    text = format_help_text(text)
    if len(text) > 1 and text.startswith("*") and text.endswith("*"):
        text = text[1:-1]

    protected = re.sub(r"(?<=\d):(?=\d)", "\u0001", text.strip())
    protected = _ABBREVIATIONS.sub(lambda match: match.group().replace(".", "\u0000"), protected)
    points = []
    for sentence in re.split(r"(?<=[.!?])\s+", protected):
        sentence = sentence.replace("\u0000", ".").strip()
        if not sentence:
            continue
        colon = re.match(r"^(?P<label>[^\s:]{1,16}):\s*(?P<description>.+)$", sentence)
        if colon:
            label = colon.group("label").strip().strip("*")
            points.append(f"**{label}**: {colon.group('description').strip()}")
            continue
        if ":" in sentence or ";" in sentence:
            separator = ":" if ":" in sentence else ";"
            label, description = sentence.split(separator, 1)
            points.extend((f"{label.strip()}{separator}", description.strip().capitalize()))
        else:
            points.append(sentence.replace("\u0001", ":"))
    return "\n".join(points)


def emoji_sorting(text: str) -> str:
    emoji_to_priority = {d["emoji"]: float(d["priority"]) for d in CHARGING_TYPES.values()}
    emoji_sorted = {}
    for emoji in text.split():
        if emoji in emoji_to_priority:
            emoji_sorted[emoji_to_priority[emoji]] = emoji
    return " ".join(emoji for _, emoji in sorted(emoji_sorted.items()))


def emoji_parse(data: dict) -> str:
    emojis = [CHARGING_TYPES[key]["emoji"] for key in data if data[key] is True and key in CHARGING_TYPES]
    return emoji_sorting(" ".join(emojis))


def join_unique_emojis(str1: str, str2: str) -> str:
    if str1 and str2:
        return " ".join(set(str1.split()).union(set(str2.split())))
    return str1 or str2


def emoji_text_format(text: str, group_size: int = 3) -> str:
    words = text.split()
    if len(words) <= group_size:
        return "".join(words)
    grouped = ["".join(words[i:i + group_size]) for i in range(0, len(words), group_size)]
    return "<br>".join(grouped)


def active_charging_rules(data: dict) -> dict:
    return {key: True for key, value in data.items() if value is True and key in CHARGING_TYPES}


def emoji_rules(emojis: str) -> dict:
    """The rules of an emoji_parse() text."""
    found = set(str(emojis or "").split())
    return {key: True for key, entry in CHARGING_TYPES.items() if entry["emoji"] in found}


def format_charging_rule(text: str, testing: bool) -> str:
    marker = "🧪" if testing else ""
    return "\n".join(
        f"{marker}{line.strip()}{marker}" for line in str(text).split("\n") if isinstance(line, str) and line.strip()
    )


def get_list_values(data) -> list:
    float_list = []
    for item in data or []:
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            float_list.append(float(item))
        elif isinstance(item, (list, tuple)) and len(item) > 1 and isinstance(item[1], (int, float)):
            float_list.append(float(item[1]))
    return float_list


def translate(texts: dict[str, str], key: str, default: str | None = None, **fmt) -> str:
    """Runtime text from texts/<language>.json (flattened keys, see load_texts).

    Placeholders use str.format, so a literal { or } is written as {{ or }} in the json.
    """
    value = texts.get(key, default if default is not None else key)
    try:
        return value.format(**fmt) if fmt else value
    except (KeyError, IndexError, ValueError):
        return value


def load_texts(language: str, currency: str | None = None) -> dict[str, str]:
    """Flattened runtime texts of texts/<language>.json, English is the fallback for missing keys.

    currency: hass.config.currency; replaces the language's currency in ui.common.valuta* and
    {valuta} / {valuta_kwh} in every text (also @@valuta@@ / @@valuta_kwh@@ in the dashboard).
    Blocking file I/O, run it in the executor.
    """
    texts: dict[str, str] = {}

    def flatten(data: dict, prefix: str = "") -> None:
        for key, value in data.items():
            if isinstance(value, dict):
                flatten(value, f"{prefix}{key}.")
            else:
                texts[f"{prefix}{key}"] = value

    folder = Path(__file__).parent.parent / "texts"
    for name in dict.fromkeys(("en", language)):
        path = folder / f"{name}.json"
        if path.is_file():
            flatten(json.loads(path.read_text(encoding="utf-8")))

    for key, value in texts.items():
        if key.startswith("dashboard.help_") or key.startswith("dashboard.") and key.endswith("_explain"):
            texts[key] = format_help_text(value)
            if key.rsplit(".", 1)[-1] in _ANALYSIS_HELP_KEYS:
                texts[key] = format_analysis_help_text(texts[key])

    if symbol := currency_symbol(currency):
        for key in [key for key in texts if key.startswith("ui.common.valuta")]:
            unit = texts[key]
            texts[key] = symbol + unit[unit.find("/"):] if "/" in unit else symbol
    valuta = {"{valuta}": texts.get("ui.common.valuta", "kr"), "{valuta_kwh}": texts.get("ui.common.valuta_kwh", "kr/kWh")}
    for key, value in texts.items():
        if isinstance(value, str) and "{valuta" in value:
            for token, replacement in valuta.items():
                value = value.replace(token, replacement)
            texts[key] = value
    texts["dashboard.valuta"] = valuta["{valuta}"]
    texts["dashboard.valuta_kwh"] = valuta["{valuta_kwh}"]
    return texts
