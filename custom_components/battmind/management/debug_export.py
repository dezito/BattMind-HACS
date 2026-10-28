"""Debug export: one anonymised file a user attaches to a GitHub issue.

The coordinator keeps the last jobs (name, duration, error, charging rule), the snapshots of
the last jobs and of the last failed job, and the package log goes to a ring buffer. The
export_debug service/button writes them with the configuration, the settings, the engine
state and the databases to <config>/battmind_debug/<prefix>_debug_<time>.json.gz and
creates a notification with a download link and a prefilled GitHub issue.

Nothing personal may end up in the file: entity ids of other integrations are replaced by
the config key they are used for (sensor.power_consumption), names of people, the home and
notify services are replaced by <redacted>, coordinates are rounded to whole degrees and
keys, addresses, e-mails, URLs and IP addresses are removed. The snapshots can be replayed
with tests/debug_replay.py.
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime
import gzip
import json
import logging
import os
import platform
import re
import traceback
from collections import deque
from copy import deepcopy
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from aiohttp import web

from homeassistant.components import persistent_notification
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import KEY_HASS
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from ..utils.codec import encode
from ..const import DEFAULT_CONFIG, DOMAIN

if TYPE_CHECKING:
    from ..coordinator import BattMindCoordinator
    from ..engine import JobResult, Snapshot

_LOGGER = logging.getLogger(__name__)

DEBUG_DIR = f"{DOMAIN}_debug"
DEBUG_FORMAT = 1
KEEP_FILES = 5  # per instance
LINK_VALID = datetime.timedelta(hours=24)
VIEW_URL = f"/api/{DOMAIN}/debug"
ISSUE_URL = "https://github.com/dezito/BattMind-HACS/issues/new"
ISSUE_TEMPLATE = "bug_report.yml"
LOG_RECORDS = 1000
LOG_FILE_LINES = 2000
JOB_HISTORY = 50
SNAPSHOTS = 2
# jobs of the debug services themselves are not part of the history
DEBUG_JOBS = {"debug_info", "debug_state"}
# the snapshot of the last planning job is kept as well, so small jobs cannot push it out
PLANNING_JOBS = {"charge_if_needed"}
# larger job arguments (the databases of startup, statistics) are not kept
MAX_ARG_ITEMS = 50
STATE_TIMEOUT = 60
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.json\.gz$")

ENTITY_DOMAINS = (
    "alarm_control_panel", "automation", "binary_sensor", "button", "calendar", "camera", "climate", "counter",
    "cover", "date", "datetime", "device_tracker", "event", "fan", "group", "humidifier", "image", "input_boolean",
    "input_button", "input_datetime", "input_number", "input_select", "input_text", "lawn_mower", "light", "lock",
    "media_player", "notify", "number", "person", "remote", "scene", "script", "select", "sensor", "siren", "sun",
    "switch", "text", "time", "timer", "todo", "update", "vacuum", "valve", "water_heater", "weather", "zone",
)
# not preceded by a path or module name (custom_components/battmind/sensor.py, homeassistant.components.sensor)
_ENTITY_RE = re.compile(r"(?<![\w./\\-])(" + "|".join(ENTITY_DOMAINS) + r")\.([a-z0-9_]+)(?!\w)")
# Solcast style sensors next to the configured one keep their day, so the replay finds them
_SUFFIX_RE = re.compile(r"^(.+?)(_today|_tomorrow|_day_\d+)$")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_URL_RE = re.compile(r"\b(?:https?|wss?)://[^\s)\"'<>]+")
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_SENSITIVE_KEY_RE = re.compile(
    r"^(address|street|city|postal_code|zip_code|country|vin|serial|serial_number|username|user_name|email|phone|"
    r"mac|ip|ip_address|host|url|entity_picture|friendly_name|notify_list|location_name|name_by_user)$"
    r"|token|password|secret|api_?key|webhook"
)
_COORDINATE_KEYS = {"latitude", "longitude", "lat", "lon", "lng"}
_ROUNDED_KEYS = {"elevation": -2, "altitude": -2, "gps_accuracy": -2}
REDACTED = "<redacted>"

# ---------------------------------------------------------------------- #
# Log ring buffer
# ---------------------------------------------------------------------- #


class _LogBuffer(logging.Handler):
    """The last log records of the package (what the logger level lets through)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: deque[dict[str, Any]] = deque(maxlen=LOG_RECORDS)
        self._formatter = logging.Formatter()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            item = {
                "time": datetime.datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
                "level": record.levelname,
                "logger": record.name,
                "thread": record.threadName,
                "message": record.getMessage(),
            }
            if record.exc_info:
                item["traceback"] = self._formatter.formatException(record.exc_info)
            self.records.append(item)
        except Exception:  # noqa: BLE001
            self.handleError(record)


LOG_BUFFER = _LogBuffer()


def install_log_buffer() -> None:
    package_logger = logging.getLogger(__package__.rpartition(".")[0])
    if LOG_BUFFER not in package_logger.handlers:
        package_logger.addHandler(LOG_BUFFER)


# ---------------------------------------------------------------------- #
# Job history
# ---------------------------------------------------------------------- #


def _arg_summary(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple, set)) and len(value) > MAX_ARG_ITEMS:
        return f"<{type(value).__name__} with {len(value)} items, not exported>"
    return value


class DebugRecorder:
    """The last jobs of a coordinator and the snapshots they ran with."""

    def __init__(self) -> None:
        self.jobs: deque[dict[str, Any]] = deque(maxlen=JOB_HISTORY)
        self.snapshots: deque[dict[str, Any]] = deque(maxlen=SNAPSHOTS)
        self.failed: dict[str, Any] | None = None
        self.planning: dict[str, Any] | None = None

    def record(self, job_name: str, args: tuple, snapshot: Snapshot, result: JobResult, rule: Any) -> None:
        if job_name in DEBUG_JOBS:
            return
        job = {
            "job": job_name,
            "now": snapshot.now.isoformat(),
            "seconds": round(result.duration, 3),
            "error": f"{type(result.error).__name__}: {result.error}" if result.error else None,
            "charging_rule": rule,
        }
        if result.error:
            job["traceback"] = "".join(traceback.format_exception(result.error))
        self.jobs.append(job)
        item = {"job": job_name, "args": [_arg_summary(arg) for arg in args], "error": job["error"], "snapshot": snapshot}
        self.snapshots.append(item)
        if result.error:
            self.failed = item
        if job_name in PLANNING_JOBS:
            self.planning = item


# ---------------------------------------------------------------------- #
# Anonymisation
# ---------------------------------------------------------------------- #


def _round_time(value: Any, minutes: int = 5) -> Any:
    if not isinstance(value, datetime.datetime):
        return value
    rounded = round((value.hour * 60 + value.minute + value.second / 60) / minutes) * minutes
    return datetime.datetime.combine(value.date(), datetime.time()) + datetime.timedelta(minutes=rounded)


class Redactor:
    """Replaces everything personal in the export, see the module docstring."""

    def __init__(self, own_prefix: str, config: dict, words: list[str]) -> None:
        self.own_prefix = own_prefix
        self.aliases: dict[str, str] = {}  # domain.object base -> alias base
        self._used: set[str] = set()
        self._collect_aliases(config)
        words = sorted({word.strip() for word in words if word and len(word.strip()) >= 3}, key=len, reverse=True)
        self._words = re.compile(r"\b(" + "|".join(map(re.escape, words)) + r")\b", re.IGNORECASE) if words else None

    def _collect_aliases(self, node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            for child_key, value in node.items():
                self._collect_aliases(value, str(child_key))
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node, 1):
                self._collect_aliases(value, f"{key}_{index}")
        elif isinstance(node, str) and (match := _ENTITY_RE.fullmatch(node.strip())):
            name = re.sub(r"_entity_ids?(?=_\d+$|$)", "", key) or "entity"
            self._alias(match.group(1), match.group(2), name)

    def _alias(self, domain: str, object_id: str, name: str | None = None) -> str:
        suffix_match = _SUFFIX_RE.match(object_id)
        base, suffix = (suffix_match.group(1), suffix_match.group(2)) if suffix_match else (object_id, "")
        key = f"{domain}.{base}"
        if key not in self.aliases:
            alias = name.removesuffix(suffix) if name else f"entity_{len(self.aliases) + 1}"
            while f"{domain}.{alias}" in self._used:
                alias = f"{alias}_{len(self._used)}"
            self._used.add(f"{domain}.{alias}")
            self.aliases[key] = alias
        return f"{domain}.{self.aliases[key]}{suffix}"

    def _entity(self, match: re.Match) -> str:
        domain, object_id = match.group(1), match.group(2)
        if object_id == "py" or object_id.startswith(f"{self.own_prefix}_") or (domain == "sun" and object_id == "sun"):
            return match.group(0)
        return self._alias(domain, object_id)

    def text(self, value: str) -> str:
        value = _ENTITY_RE.sub(self._entity, value)
        value = _EMAIL_RE.sub("<email>", value)
        value = _URL_RE.sub("<url>", value)
        value = _IP_RE.sub("<ip>", value)
        if self._words:
            value = self._words.sub(REDACTED, value)
        return value

    def key(self, key: Any) -> str:
        """Dict keys get the same treatment as text (e.g. overview snapshots keyed by the title)."""
        if not isinstance(key, str):
            key = key.isoformat() if isinstance(key, (datetime.date, datetime.time)) else str(key)
        return self.text(key)

    def value(self, value: Any, key: str = "") -> Any:
        lower = key.lower()
        if lower in _COORDINATE_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool):
            return round(value)
        if lower in _ROUNDED_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool):
            return round(value, _ROUNDED_KEYS[lower])
        if lower and _SENSITIVE_KEY_RE.search(lower) and value not in (None, "", [], {}):
            return REDACTED
        if isinstance(value, dict):
            return {self.key(k): self.value(v, k if isinstance(k, str) else "") for k, v in value.items()}
        if isinstance(value, (list, tuple, set, frozenset, deque)):
            return [self.value(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        if isinstance(value, datetime.datetime):
            return {"__dt__": value.isoformat()}
        if isinstance(value, datetime.date):
            return {"__date__": value.isoformat()}
        if isinstance(value, datetime.time):
            return {"__time__": value.isoformat()}
        if isinstance(value, datetime.timedelta):
            return str(value)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return self.value({field.name: getattr(value, field.name) for field in dataclasses.fields(value)}, key)
        return self.text(str(value))


# ---------------------------------------------------------------------- #
# Export
# ---------------------------------------------------------------------- #


def debug_dir(hass: HomeAssistant) -> str:
    return hass.config.path(DEBUG_DIR)


def snapshot_dict(snapshot: Snapshot) -> dict[str, Any]:
    """Snapshot fields in the encoding of the databases (codec.encode), sun times rounded."""
    data = {field.name: getattr(snapshot, field.name) for field in dataclasses.fields(snapshot)}
    data["tz"] = str(snapshot.tz)
    data["sun_events"] = {
        date: {event: _round_time(value) for event, value in events.items()} for date, events in (snapshot.sun_events or {}).items()
    }
    return encode(data)


async def _async_words(hass: HomeAssistant, coordinator: BattMindCoordinator) -> list[str]:
    """Names that must not end up in the file: people, users, the home and notify services."""
    words = [hass.config.location_name or ""]
    for state in hass.states.async_all("person"):
        name = str(state.attributes.get("friendly_name") or "")
        words += [name, state.object_id, *(part for part in re.split(r"[\s_-]+", name) if len(part) >= 4)]
    for user in await hass.auth.async_get_users():
        if not user.system_generated and user.name:
            words += [user.name, *(part for part in user.name.split() if len(part) >= 4)]
    words += [str(name) for name in coordinator.config.get("notify_list") or []]
    return words


def _log_file_tail(path: str) -> list[str] | None:
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as file:
        return [line.rstrip("\n") for line in deque(file, maxlen=LOG_FILE_LINES)]


def _write(folder: str, name: str, prefix: str, data: dict[str, Any], redactor: Redactor) -> int:
    """Anonymise and write the export, remove the oldest files of the instance (blocking)."""
    content = json.dumps(redactor.value(data), ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with gzip.open(f"{path}.tmp", "wb") as file:
        file.write(content)
    os.replace(f"{path}.tmp", path)
    own = sorted(file for file in os.listdir(folder) if file.startswith(f"{prefix}_debug_") and _NAME_RE.match(file))
    for old in own[:-KEEP_FILES]:
        os.remove(os.path.join(folder, old))
    return os.path.getsize(path)


async def async_export_debug(hass: HomeAssistant, coordinator: BattMindCoordinator) -> str:
    """Write the debug file of coordinator, returns its name (in debug_dir)."""
    now = dt_util.now()
    prefix = coordinator.prefix
    engine_state: Any = None
    if coordinator.engine is not None:
        try:
            async with asyncio.timeout(STATE_TIMEOUT):
                engine_state = (await coordinator.async_run_job("debug_state")).value
        except TimeoutError:
            engine_state = "<timeout, a job is still running>"
    recorder = coordinator.debug
    entities = {}
    for entity_id in coordinator.configured_entities:
        state = hass.states.get(entity_id)
        entities[entity_id] = {
            "platform": coordinator._entity_platforms.get(entity_id),
            "state": state.state if state else None,
            "unit": state.attributes.get("unit_of_measurement") if state else None,
            "device_class": state.attributes.get("device_class") if state else None,
            "last_changed": state.last_changed.isoformat() if state else None,
        }
    snapshots = [{**item, "snapshot": snapshot_dict(item["snapshot"])} for item in recorder.snapshots]
    failed, planning = recorder.failed, recorder.planning
    config = deepcopy(coordinator.config)
    solar = config.setdefault("solar", {})
    solar.setdefault("battery_temperature_min", DEFAULT_CONFIG["solar"]["battery_temperature_min"])
    solar.setdefault("battery_temperature_max", DEFAULT_CONFIG["solar"]["battery_temperature_max"])
    data = {
        "integration": DOMAIN,
        "format": DEBUG_FORMAT,
        "created": now.isoformat(timespec="seconds"),
        "version": (await async_get_integration(hass, DOMAIN)).version,
        "ha_version": HA_VERSION,
        "python": platform.python_version(),
        "time_zone": hass.config.time_zone,
        "prefix": prefix,
        "status": {"started": coordinator.started, "testing": coordinator.testing, "last_job": dict(coordinator.last_job)},
        "options": dict(coordinator.entry.options),
        "config": config,
        "entities": entities,
        "settings": dict(coordinator.settings),
        "states": dict(coordinator.data),
        "attributes": {key: dict(value) for key, value in coordinator.attributes.items()},
        "jobs": list(recorder.jobs),
        "snapshots": snapshots,
        "failed_job": {**failed, "snapshot": snapshot_dict(failed["snapshot"])} if failed else None,
        "planning_job": {**planning, "snapshot": snapshot_dict(planning["snapshot"])} if planning else None,
        "engine": engine_state,
        "databases": await coordinator.storage.async_load_raw(),
        # the snapshots only carry prices when they were refreshed, the replay falls back to these
        "price_data": coordinator._price_snapshot(),
        "log": list(LOG_BUFFER.records),
        "log_file": await hass.async_add_executor_job(_log_file_tail, hass.config.path(f"{DOMAIN}_{prefix}.log")),
    }
    redactor = Redactor(prefix, coordinator.config, await _async_words(hass, coordinator))
    name = f"{prefix}_debug_{now.strftime('%Y%m%d_%H%M%S')}.json.gz"
    size = await hass.async_add_executor_job(_write, debug_dir(hass), name, prefix, data, redactor)
    _LOGGER.info("Debug info of BattMind %s written to %s (%s kB)", prefix, os.path.join(DEBUG_DIR, name), round(size / 1024))
    return name


def _download_link(hass: HomeAssistant, name: str) -> str | None:
    """Signed link to the file, valid for LINK_VALID (None without the http integration)."""
    try:
        from homeassistant.components.http.auth import async_sign_path

        return async_sign_path(hass, f"{VIEW_URL}/{name}", LINK_VALID)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("No download link for %s: %s", name, err)
        return None


async def async_notify_debug(hass: HomeAssistant, coordinator: BattMindCoordinator, name: str) -> None:
    """Notification with the download link and how to report the problem on GitHub."""
    version = (await async_get_integration(hass, DOMAIN)).version
    created = dt_util.now().strftime("%Y-%m-%d %H:%M")
    issue = f"{ISSUE_URL}?" + urlencode(
        {"template": ISSUE_TEMPLATE, "version": version, "ha_version": HA_VERSION, "when": created, "debug_file": name}
    )
    link = _download_link(hass, name)
    download = f'<a href="{link}" target="_blank" download="{name}">⬇️ {name}</a>' if link else f"`{DEBUG_DIR}/{name}`"
    persistent_notification.async_create(
        hass,
        coordinator.t("ui.debug.exported", download=download, file=f"{DEBUG_DIR}/{name}", issue=issue, hours=int(LINK_VALID.total_seconds() // 3600)),
        title=f"🐞BattMind ({coordinator.prefix})",
        notification_id=f"{coordinator.prefix}_debug_export",
    )


class DebugDownloadView(HomeAssistantView):
    """Download of a debug file (logged in, or the signed link of the notification)."""

    url = VIEW_URL + "/{name}"
    name = f"api:{DOMAIN}:debug"
    requires_auth = True

    async def get(self, request: web.Request, name: str) -> web.StreamResponse:
        hass: HomeAssistant = request.app[KEY_HASS]
        if not _NAME_RE.match(name):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        path = os.path.join(debug_dir(hass), name)
        if not await hass.async_add_executor_job(os.path.isfile, path):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        return web.FileResponse(
            path, headers={"Content-Disposition": f'attachment; filename="{name}"', "Content-Type": "application/gzip"}
        )
