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
├── config_flow.py / flow/               Setup/options flow, pages, and schemas
├── const/                               Configuration, settings, and signal constants
├── engine/                              Planning and battery analysis
│   ├── engine.py / planning.py          Calculation orchestration
│   ├── optimizer.py / planner/          Optimization and charge/discharge decisions
│   │   ├── what_if.py                   What-if simulator calculation
│   │   └── future/                      Future-period planning rules and context
│   ├── pricing.py                       Price helpers, incl. `sell_value()`
│   ├── diagnostics.py                   Engine debug information
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
├── test_recorder_history.py             Recorder fallbacks for what-if data
├── test_config_flow.py / test_init.py           Setup and lifecycle
├── test_backup.py / test_debug_export.py        Persistence and export
├── test_dashboard.py / test_blueprint_*.py       Dashboard and blueprint behavior
├── fixtures/                            Legacy PyScript config for the import tests
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
- Action entities for the user's automations: `docs/actions.md`
- Inverter setup: `docs/inverters/`
- What-if simulator: `docs/what-if-simulator.md`
- Setup and options: `docs/getting-started.md`
- Backups, import/export, and migration: `docs/migration.md` and the backup
  section in `README.md`
- Dashboard behavior: `docs/dashboard-analysis.md`
- Examples, troubleshooting, and emoji meanings: `docs/cookbook.md`,
  `docs/faq-troubleshooting.md`, and `docs/emojis.md`

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
- After every new, changed, or removed feature, review and update the
  README, the relevant `docs/` pages, the automated tests, and the HA-dev
  seed, simulator, and other test data before the task is done. Remove
  outdated references and test data, and check that documentation and test
  data describe the actual behavior.
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

## Calculations and economic estimates

These rules apply to code, tests, documentation, and the dashboard.

- Follow data and calculations from input to the displayed result; check
  units, timestamps, missing data, fees, losses, and boundary conditions.
- Do not let missing history or unknown prices turn into favourable default
  values. Reject the calculation or use a documented, conservative value.
- Clearly distinguish historical hindsight from a realistic forecast. Do not
  present an optimistic backtest result as expected savings, earnings, or
  payback time.
- Add regression tests for bugs found and for scenarios with missing or
  incomplete data.
- Use Home Assistant data for validation when an environment is available and
  access is approved. Keep credentials and sensitive data out of code, logs,
  and reports.

## Project notes

Agents record lasting decisions and findings for this repository here (not in
private agent memory). Update or remove a note when it stops being true.

- Keep the worker-thread design so calculations never block HA:
  single-thread executor, snapshot -> job result.
- `input_boolean.dezito_show_debug_button` in the generated dashboard is the
  user's own toggle for the debug-info badge. It is intentional; do not flag it.
- README/docs audience: first-time installs, possibly migrating from PyScript. No release notes or "changed since" text about development versions.
  Detailed docs live in `docs/` (English); the README is a short landing page
  that links into it. Anchor links to emoji headings need a leading hyphen
  (`#-buy-to-sell`).

### PyScript import

Many users still run the PyScript version, so the import must keep working and
stay easy: the config flow finds legacy files (`find_legacy_sources`,
`load_legacy_config` in `management/storage.py`, `options_from_legacy` in
`flow/schema.py`). Covered by `test_import_legacy_pyscript_config` in
`tests/test_config_flow.py`.

### Planner and optimiser rules (design requirements)

Keep these invariants when touching any planner rule:

- Solar surplus has first priority: grid charging may not use charge rate that
  solar uses in that hour, nor battery room that solar fills later
  (`grid_headroom_kwh`: highest level from the charge hour until the battery
  hits min). Solar first is soft when a price is known: grid may push solar
  out if that solar is sold for more than the grid kWh costs.
- `solar_prediction_db["total"]` is solar SURPLUS (production minus house
  consumption), so there is no battery consumption in hours with surplus.
- `min_profit_per_kwh` / `min_total_profit` apply to every charge/discharge
  rule; `min_sell_kwh` to every sale. Exception:
  `force_powerwall_max_hours_old_discharge` uses old energy even without
  profit, but only sells it when selling beats using it. In the optimiser,
  `min_total_profit` is a fixed cost per charge/sale hour and must NOT block
  plain self consumption.
- Charging loss is round trip: 1 kWh out costs price/(1-loss)
  (`calc_battery_loss_cost`), split evenly: grid kWh × sqrt(1-loss) into the
  battery, used/sold kWh ÷ sqrt(1-loss) out (`battery_efficiency()`).
- `most_profit` pairs a sale with the cheapest usage hours in the same
  discharge period (no full battery in between).
- The battery never charges and discharges in the same hour (execution
  priority: grid charge > force discharge > discharge).
- `use_midnight_battery_level` with the optimiser: each day of
  `days_to_predict` is optimised on its own (`optimize_per_day()`, max 24 h,
  ends at midnight, next day starts at the previous day's end level).
  Horizons follow `days_to_predict`, not fixed day/day+1 windows.
- Switch `optimizer_control` (default on): `apply_optimizer_plan()` replaces
  the rule plan with the DP's actions; rules still run for comparison and are
  the fallback if the optimiser fails. `explain()` maps every optimised action
  to its matching rule; rule switches are permissions (`RULE_SWITCHES`,
  `optimize_with_rules` forbids the action and re-optimises). The optimiser is
  always calculated.
- Rule switches default on (except `use_midnight_battery_level`);
  `powerwall_max_hours_old` default 48.
- The shadow plan simulates rule hours with the planned kWh and respects
  `exclude_sell_hours`.
- Partial charge hours are real: the battery level is in `plan_inputs_hash`,
  so every cron interval replans from the new level. The optimiser models
  this with grid fractions 25/50/75/100 %.
- A fixed solar sell price (`production_price` != -1) is a settlement
  agreement: a sold kWh is worth fixed price − the hour's raw sell price and
  can be negative. Use `sell_value()` (`engine/pricing.py`) everywhere; keep
  price data and DBs raw and convert at use, never clamp at 0.
- The test harness has no sell entity (sell = buy price); use the
  `solar_engine()` helper in `tests/test_engine.py` for realistic scenarios.

### Debug export

- `management/debug_export.py`, service `battmind.export_debug`, diagnostic
  button `export_debug`, and a discreet "Problem or error?" section at the
  bottom of the last column of the dashboard's Settings view (not a badge on
  the overview). Writes `<config>/battmind_debug/<prefix>_debug_<ts>.json.gz`
  (newest 5 kept) plus a persistent notification with a signed 24 h download
  link and a prefilled `.github/ISSUE_TEMPLATE/bug_report.yml` link.
- The `Redactor` anonymises entity ids, names, locations, and secrets. Keep
  this file in sync with the CJP copy.
- To analyse a file handed in by the user, run from the repo root:
  `PYTHONPATH=../ha-dev/windows %LOCALAPPDATA%/ha-dev-tests/venv/Scripts/python.exe tests/debug_replay.py <file> [failed|planning|<index>]`
  (default: failed, else planning, else last). Older exports may
  lack `planning_job`/`price_data` and replay less
  faithfully.

## Tests and validation

### Devcontainer

`../.devcontainer/devcontainer.json` opens the workspace folder (both
integration repos and `ha-dev`) in a container with Home Assistant installed.
Inside it, run `python -m pytest` from the repository root; it is Linux, so no
shims are needed. The dev HA runs on `http://localhost:8123`; after a code
change run `bash ha-dev/devcontainer/restart.sh`, and
`bash ha-dev/devcontainer/restart.sh reset` wipes the state and seeds again.
Seeding creates a BattMind entry wired to the simulator entities.

The seed data and simulator entities are in `../ha-dev/devcontainer/seed.py`
(`SIM_ENTITIES`/`SEEDS`), the simulator in
`../ha-dev/config/packages/simulator.yaml`, synthetic history and backups in
`../ha-dev/devcontainer/history.py`, and HA config in
`../ha-dev/devcontainer/configuration.yaml`. Test the PyScript import with
`../ha-dev/dev.ps1 import-legacy` before adding the integration.

### Outside the devcontainer, on Windows

Run the suite with `../ha-dev/run_tests_windows.ps1 battmind
[pytest args]`. The first run installs Python 3.14 and the test requirements
in `%LOCALAPPDATA%\ha-dev-tests\venv`; the shims in `../ha-dev/windows`
(fcntl, resource, a pytest plugin that disables the socket guard) must be on
`PYTHONPATH` when calling that venv directly. The system Python 3.11 lacks
HA/astral. Do not create the venv on a network drive; it installs extremely
slowly. The Docker run `../ha-dev/dev.ps1 test battmind [pytest args]` is the
reference. The dev HA runs locally in Docker Desktop, not on a remote server;
see `../ha-dev/README.md` (`ha-dev/dev.ps1 up|restart|reset|test`).
`tests/conftest.py` has `async_advance()` for timers.

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
