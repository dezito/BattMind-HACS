"""Lossless JSON encoding of the BattMind databases (datetime values and non-str keys)."""
from __future__ import annotations

import datetime
from typing import Any

_DT = "__dt__"
_DICT = "__dict__"


def encode(obj: Any) -> Any:
    """Convert to JSON friendly data."""
    if isinstance(obj, datetime.datetime):
        return {_DT: obj.isoformat()}
    if isinstance(obj, dict):
        if all(isinstance(key, str) and key not in (_DT, _DICT) for key in obj):
            return {key: encode(value) for key, value in obj.items()}
        return {_DICT: [[encode(key), encode(value)] for key, value in obj.items()]}
    if isinstance(obj, (list, tuple)):
        return [encode(value) for value in obj]
    return obj


def _decode_key(key: Any) -> Any:
    key = decode(key)
    return tuple(key) if isinstance(key, list) else key


def decode(obj: Any) -> Any:
    """Reverse of :func:`encode`."""
    if isinstance(obj, dict):
        if len(obj) == 1 and _DT in obj:
            return datetime.datetime.fromisoformat(obj[_DT])
        if len(obj) == 1 and _DICT in obj:
            return {_decode_key(key): decode(value) for key, value in obj[_DICT]}
        return {key: decode(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [decode(value) for value in obj]
    return obj
