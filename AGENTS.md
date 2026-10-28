# BattMind-HACS agent guide

Instructions for coding agents working in this repository. Follow the existing
implementation and project documentation when this guide does not answer a
question. Do not guess behavior that affects battery planning, stored data, or
Home Assistant configuration.

## Project scope

BattMind is a Home Assistant custom integration installed through HACS. It
calculates battery plans and exposes entities/actions; it does not directly
control an inverter. Users connect the resulting actions to their own
automations or blueprints.

- Integration code: `custom_components/battmind/`
- Tests and shared fixtures: `tests/`
- User and behavior documentation: `docs/` and `README.md`
- Integration metadata: `custom_components/battmind/manifest.json`

## Code map

```text
custom_components/battmind/             The HA integration (HACS payload)
├── __init__.py                          Integration setup and platform forwarding
├── coordinator/                         Shared data, updates, events, and lifecycle
├── config_flow/                         Setup/options flow and schemas
├── const/                               Configuration, settings, and signal constants
├── engine/                              Planning and battery analysis
│   ├── engine.py / planning.py          Calculation orchestration
│   ├── optimizer.py / planner/          Optimization and charge/discharge decisions
│   │   └── future/                      Future-period planning rules and context
│   ├── energy/                          Price, solar, forecast, and energy inputs
│   ├── history/                         Recorder queries and stored history
│   ├── analysis/                        Results, recording, and published analysis
│   └── battery.py / state.py            Battery model and planning state
├── prices/                              Price-source adapters and factory
├── management/                          Storage, backups, dashboard, and blueprint setup
├── utils/                               Shared codecs, formatting, and calculations
├── sensor.py / number.py / switch.py    Main HA entity platforms
├── select.py / button.py / text.py      Other HA entity platforms
├── entity.py / repairs.py               Shared entity base and repair flows
├── translations/ texts/                 HA strings and user-facing descriptions
├── dashboard/ blueprints/               Bundled dashboard and automation blueprint
└── manifest.json                        HA integration metadata

tests/                                   Pytest suite
├── conftest.py                          Shared fixtures and simulated HA entities
├── test_engine.py / test_planner_rules.py  Engine and planning behavior
├── test_solar_optimizer.py / test_efficiency.py  Solar and efficiency calculations
├── test_analysis.py / test_debug_states.py      Analysis and published state
├── test_config_flow.py / test_init.py           Setup and lifecycle
├── test_backup.py / test_debug_export.py        Persistence and export
├── test_dashboard.py / test_blueprint_*.py       Dashboard and blueprint behavior
└── debug_replay.py                      Debug/replay helper

docs/                                    User documentation and behavior details
.github/workflows/validate.yml           CI: hassfest and HACS validation
pytest.ini / requirements_test.txt       Pytest configuration and pinned test deps
```

Use this as a map, not an exhaustive inventory; check the current files before
editing. Prefer the narrowest relevant module and test file over changing a
broad layer for a local behavior change.

Read the relevant documentation before changing behavior. In particular:

- Planning, energy inputs, and stored prices: `docs/how-it-works.md`
- Individual planning rules: `docs/rules/`
- Setup and options: `docs/getting-started.md`
- Backups, import/export, and migration: `docs/migration.md` and the backup
  section in `README.md`
- Dashboard behavior: `docs/dashboard-analysis.md`

## Important behavior and compatibility

- Treat optimizer and planner changes as financially significant. Preserve
  battery, grid, solar, buy/sell price, charging/discharging loss, and wear
  semantics. Keep decisions explainable in the published plan.
- Preserve the distinction between planning and inverter control. Do not add
  direct device control or assume a particular inverter, entity, tariff, or
  automation.
- Respect Home Assistant timestamps and configured time zones. Avoid naive
  datetime assumptions and test day boundaries, missing/future price data,
  and daylight-saving transitions when relevant.
- Preserve compatibility of config-entry options, entity IDs/unique IDs,
  stored data, backups, and migration from the PyScript version. Changes to
  these formats or identifiers require a migration/compatibility path and
  documentation.
- Data from Home Assistant entities and external price integrations can be
  missing, stale, unavailable, malformed, or non-numeric. Handle these cases
  explicitly and consistently with the existing fail-safe behavior; do not
  silently invent prices or measurements.
- Keep network and blocking work out of the event loop. Follow the existing
  coordinator/executor lifecycle and ensure workers and resources are cleaned
  up on unload.
- Do not add telemetry, undisclosed external services, credentials, or
  undocumented network endpoints.

## Implementation conventions

- Follow the existing Home Assistant patterns in this integration for config
  entries, coordinators, entities, services, repairs, and lifecycle management.
- Keep changes focused. Prefer extending the existing module responsible for
  the behavior over adding duplicate logic or broad refactors.
- Add or update tests for behavior changes. Prefer deterministic tests using
  the shared fixtures in `tests/conftest.py`; mock Home Assistant entities and
  external integrations rather than relying on a live installation or device.
- Test boundary conditions and failure paths as well as the expected path.
  For asynchronous lifecycle changes, verify setup, unload, and background
  task cleanup.
- Keep user-facing behavior and documentation aligned. Update README/docs when
  configuration, entities, planning behavior, backup formats, or migration
  steps change.
- Whenever changing dashboard content or behavior, review and update each
  affected "Read more" description so it accurately covers the entities,
  controls, and functions shown beneath it. Check that the descriptions do not
  omit new dashboard content or retain claims about removed content.
- When a HA-dev environment is available, apply and verify dashboard changes
  there as well. For dashboard analysis changes, check that every test
  device/config entry has sufficient representative recorder/history data for
  the analysis entities and time ranges being tested; current entity states
  alone are not enough. If required history is missing, create or update
  dedicated test history and states in HA-dev so the analysis page can render
  and calculate its results. Do not alter live user history. Report any
  missing data that could not safely be added, or if HA-dev is unavailable.
- If changing any file under `custom_components/battmind/texts/` or
  `custom_components/battmind/translations/`, update every supported language
  (`da`, `de`, `en`, `pl`, and `uk`) consistently. Check that keys and
  placeholders match across languages; do not update only Danish or English.
- Do not edit generated/cache files such as `__pycache__`, `.pytest_cache`, or
  `.ruff_cache`.

## Tests and validation

The test configuration is in `pytest.ini`; test dependencies are pinned in
`requirements_test.txt`. Those dependencies target the Home Assistant
2026.10.x / Python 3.14 environment noted in that file.

From the repository root, run the focused test file(s) first, then the full
suite when practical:

```bash
python -m pytest tests/test_<area>.py
python -m pytest
```

If test dependencies are not installed, install the repository's declared
test requirements in the active environment:

```bash
python -m pip install -r requirements_test.txt
```

The GitHub validation workflow runs Home Assistant `hassfest` and the HACS
integration validation action. Run the relevant checks when available for
changes to integration metadata, translations, or HACS structure. Do not claim
a check passed unless it was actually run. No repository Ruff configuration
or Ruff CI check is defined; follow the existing style rather than introducing
an unconfigured formatter or automatic rewrite.

## Before finishing a change

1. Review the diff and confirm it contains only intended changes.
2. Run the focused tests; run the full pytest suite for shared/core behavior
   changes when practical.
3. Run relevant Home Assistant/HACS validation for metadata or packaging
   changes, if available.
4. Check translations, documentation, and compatibility requirements above.
5. Report which checks ran and their results, and disclose any validation that
   could not be run.
