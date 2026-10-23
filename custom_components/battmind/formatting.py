"""Emoji and text formatting helpers shared by the engine modules."""
from __future__ import annotations

import json
from pathlib import Path

from .const import CHARGING_TYPES


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


def load_texts(language: str) -> dict[str, str]:
    """Flattened runtime texts of texts/<language>.json, English is the fallback for missing keys.

    Blocking file I/O, run it in the executor.
    """
    texts: dict[str, str] = {}

    def flatten(data: dict, prefix: str = "") -> None:
        for key, value in data.items():
            if isinstance(value, dict):
                flatten(value, f"{prefix}{key}.")
            else:
                texts[f"{prefix}{key}"] = value

    folder = Path(__file__).parent / "texts"
    for name in dict.fromkeys(("en", language)):
        path = folder / f"{name}.json"
        if path.is_file():
            flatten(json.loads(path.read_text(encoding="utf-8")))
    return texts
