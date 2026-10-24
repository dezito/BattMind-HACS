<img src="custom_components/battmind/brand/icon.png" alt="BattMind" width="128" align="right">

# BattMind🤵‍♂️🔋⚡ for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
[![GitHub Release](https://img.shields.io/github/v/release/dezito/BattMind-HACS)](https://github.com/dezito/BattMind-HACS/releases)
[![License](https://img.shields.io/github/license/dezito/BattMind-HACS)](LICENSE)

Home battery planning based on electricity prices, house consumption, solar forecast and the weather.

BattMind works out when the home battery should charge from the grid, when it may discharge, and when
it pays to sell power. The result is exposed as an action
(`sensor.battmind_powerwall_action`) that you use to control your inverter yourself.

> [!NOTE]
> This is the **integration version** of BattMind, installed via HACS. It replaces the
> [PyScript version](https://github.com/dezito/BattMind) and can import its configuration and
> databases. See [Switching from the PyScript version](#switching-from-the-pyscript-version).

> [!IMPORTANT]
> First HACS release. Please open an [issue](https://github.com/dezito/BattMind-HACS/issues) if something doesn't work.

---

## Features

- **Charging plan for several days** (1–7 days) based on electricity prices, forecasts and consumption history
- **Two ways to control the battery**
  - **Rules** (default): the charge and discharge rules below decide each hour
  - **Whole period optimisation:** calculates the cheapest action for every hour of the whole forecast period at
    once (charging loss, wear, solar, buy and sell prices and the profit requirements). Every action is shown as
    the rule it corresponds to, and the rule switches decide which actions it may use
- **Charging rules that can be switched on and off individually** – solar surplus always charges first, and grid
  charging never pushes out solar that would be sold for less than the grid kWh cost
  - **Cheapest periods:** charge in the cheapest periods for the day's consumption (number of periods and price limits are configurable)
  - **Fill up during cheapest period:** charge the battery fully in the cheapest period
  - **Most expensive periods:** charge for the most expensive hours the battery doesn't cover
  - **Required before max level:** only charge what is missing before the solar panels fill the battery
  - **Negative prices:** fill the battery when charging pays, even if the energy is never used
  - **Buy to sell:** charge cheaply from the grid only to sell the kWh again when it pays after loss and wear
- **Discharging rules**
  - **Profit only:** block discharging in hours where the battery's kWh cost (incl. loss and wear) is higher than the price
  - **Prioritise by energy cost:** save the battery for the most expensive hours until it is charged again
  - **Before the sun:** use/sell the battery before the sun fills it anyway and the surplus would be sold cheaply
  - **Max. age of kWh:** energy older than X hours is used even without profit, and sold when that pays more
- **Selling rules**
  - **Maximise profit:** sell in the most expensive periods when that pays more than using the energy yourself
  - **Sell excess kWh:** sell the energy that isn't used before the battery is empty or full again (forced discharge)
  - **Use midnight battery level:** only look until midnight instead of the whole forecast period
  - min. profit per kWh and in total, min. kWh per sale, and exclude specific selling hours
- **Solar (optional)**
  - **Solar surplus charging:** include the solar surplus in the plan
  - **Optimise solar charging:** sell the solar surplus in the expensive hours and fill the battery in the cheap solar hours instead
  - forecast from [Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar) and/or own history per cloud cover
- **Consumption forecast** from own history: average, EMA or trend
- **Price providers:** [Energi Data Service](https://github.com/MTrab/energidataservice) and/or
  [Strømligning](https://github.com/MTrab/stromligning), incl. sell price, tariffs, selling fee and refund
- **Cost price of the battery's energy:** what the energy in the battery has cost (grid, solar, charge/discharge loss and wear)
- **Detailed charging history and overview** as markdown
- **[Analysis](#dashboard)** of what the battery earns and how well BattMind plans
  - economy per month: charged (grid/solar), discharged, charge price, value of a discharged kWh, spread and savings
  - charged and discharged per rule, cycles and wear
  - measured round trip loss against the configured loss, and self-sufficiency (solar, battery, grid)
  - the error of each consumption forecast type, with a hint when another type is more accurate
  - a hindsight score per day, planned against what happened, days with an empty battery in the most expensive
    hours, and planned against measured solar surplus
  - the first time, the last year is read from Home Assistant's long-term statistics
- **[Ready-made dashboard](#dashboard)** in the sidebar with overview, settings and analysis, built from your setup
- **Fail-safe:** if prices are missing, planning falls back to the internal price database
- **Multiple instances:** each instance has its own prefix (e.g. one per battery)
- **Languages:** Danish, English, German, Polish and Ukrainian

### Does not run in Home Assistant's main thread

All calculations run in BattMind's own worker thread, so Home Assistant is not blocked while
the plan is calculated, as it was with the PyScript version. The attributes
`worker_thread` and `calculation_seconds` on `sensor.battmind_current_charging_rule` show
where the calculation ran and how long it took.

---

## How to control the home battery

BattMind sends **no commands** to your inverter. It exposes what the battery should do right now:

| `sensor.battmind_powerwall_action` | Meaning |
|---|---|
| `grid_charging` | Charge the battery from the grid now |
| `discharge_allowed` | Normal operation: the battery isn't really controlled – it charges from solar and discharges to the house as the inverter normally does |
| `force_discharge` | Discharge / sell to the grid now (the most expensive hours) |
| `force_solar_only_charging` | Charge from solar only, no discharging |
| `blocked_solar_charge` | Don't charge from solar (the surplus is sold instead) |

The easiest way is the ready-made [blueprint](#blueprint-control-the-home-battery), which works with all
inverters. If you'd rather write the automation yourself, the example below is a template where you
insert your own actions:

```yaml
alias: BattMind - control home battery
triggers:
  - trigger: state
    entity_id: sensor.battmind_powerwall_action
actions:
  - choose:
      - conditions: "{{ trigger.to_state.state == 'grid_charging' }}"
        sequence: []  # e.g. force charging from the grid
      - conditions: "{{ trigger.to_state.state == 'discharge_allowed' }}"
        sequence: []  # e.g. normal operation / maximise self-consumption (solar charging and discharging allowed)
      - conditions: "{{ trigger.to_state.state == 'force_discharge' }}"
        sequence: []  # e.g. force discharge to the grid
      - conditions: "{{ trigger.to_state.state in ['force_solar_only_charging', 'blocked_solar_charge'] }}"
        sequence: []  # e.g. stop discharging
```

`sensor.battmind_current_charging_rule` shows why (e.g. *Charging up to 💸*), and
`sensor.battmind_battery_level_needed` shows how much battery is needed until the next charge.

---

## Requirements

- Home Assistant **2025.1** or newer with recorder (default)
- A sensor for the home battery's **battery level** (%) and **power** (W)
- A sensor for the **house consumption** (W)
- Price integration: [Energi Data Service](https://github.com/MTrab/energidataservice) and/or [Strømligning](https://github.com/MTrab/stromligning)
- Optional:
  - solar production (W)
  - [Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar)
  - a weather integration that supports `weather.get_forecasts` with cloud cover, e.g.
    [Meteorologisk institutt](https://www.home-assistant.io/integrations/met/),
    [AccuWeather](https://www.home-assistant.io/integrations/accuweather/) or
    [OpenWeather](https://www.home-assistant.io/integrations/openweathermap/)
- For the dashboard (HACS frontend): `apexcharts-card`, `mushroom`, `config-template-card` and `card-mod`

---

## Installation

### HACS (recommended)

[![Open your Home Assistant and add the repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=dezito&repository=BattMind-HACS&category=integration)

1. **HACS** → ⋮ → **Custom repositories**: add `https://github.com/dezito/BattMind-HACS` as **Integration**.
2. Search for **BattMind**, install it, and restart Home Assistant.
3. Add the integration:

   [![Open your Home Assistant and add BattMind.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=battmind)

   or **Settings → Devices & services → Add integration → BattMind**.
4. Create the automation that controls the home battery, e.g. with the [blueprint](#blueprint-control-the-home-battery).

### Manual

Copy `custom_components/battmind` to `config/custom_components/battmind` and restart Home Assistant.

---

## Setup

Setup is done in the UI, split into small pages with help texts:

1. **Prefix and language:** the prefix becomes the start of all entity ids (default `battmind`)
2. **Home battery:** battery level, power (optionally inverted sign), size, min./max. level,
   charge/discharge power, charge/discharge loss and wear per kWh
3. **Consumption:** the house consumption, and whether it includes the battery's charging/discharging. You can also
   ignore consumers, e.g. the EV charger.
4. **Price providers:** Energi Data Service and/or Strømligning (buy, sell and tariffs), refund and selling fee
5. **Solar panels:** production, the inverter's max. power, fixed sell price or hourly price, and Solcast
6. **Weather:** hourly and daily forecast (only with solar panels)
7. **Advanced:** number of days to plan, interval, dashboard, diagnostic file log and testing mode
8. **Database:** how much history is stored

The entity selectors only show relevant entities. Battery level only shows `battery` sensors,
power only `power` sensors, prices only entities from the price integrations and the forecast only Solcast.

Everything can be changed afterwards under **Configure** on the integration. The integration reloads when you save.

---

## Entities

The entity ids start with the prefix, here `battmind`.

### Settings (changed from the dashboard)

| Type | Entities |
|---|---|
| `switch` | Solar surplus charging, cheapest periods, fill up during cheapest period, most expensive periods, required before max level, discharge only at profit, prioritise discharging by energy cost, sell excess kWh, use midnight battery level, maximise profit, forced discharge of old kWh, optimise solar charging, negative prices, buy to sell, discharge before the sun, control the battery with the whole period optimisation |
| `number` | Cheap price periods, increase for cheapest price / cheap periods, min. profit per kWh, min. total profit, min. kWh for export, max. hours for old kWh |
| `select` | Forecast type (average, EMA or trend) |
| `text` | Exclude selling hours (e.g. `17,18,19`) |
| `button` | Force planning, export data (a [backup](#backup-export-and-import-of-data)), reset dashboard |

The plan is recalculated 5 seconds after a setting is changed. The values are kept across restarts.

### Results

| Entity | Content |
|---|---|
| `sensor.battmind_powerwall_action` | What the battery should do now (see [above](#how-to-control-the-home-battery)) |
| `sensor.battmind_current_charging_rule` | Current rule as text |
| `sensor.battmind_battery_level_needed` | Required battery level until the next charge (%) |
| `sensor.battmind_kwh_charged_by_solar` | kWh charged by solar (only with solar panels) |
| `sensor.battmind_analysis` | Savings this month; the analysis per month and day as attributes (used by the dashboard) |
| `sensor.battmind_overview` | Overview and plan (markdown in the `overview` attribute) |
| `sensor.battmind_charging_history` | Charging history (markdown in the `history` attribute) |
| `sensor.battmind_emoji_description` | Emoji explanation (markdown in the `description` attribute) |

Large attributes (markdown, plans) are not stored in the recorder database.

---

## Dashboard

With **Dashboard in the sidebar** enabled (default), each instance gets its own dashboard,
e.g. `/battmind-dashboard`. It is built on every startup from your setup. Cards for
price providers and solar forecast that you don't use are removed.

- **Overview:** the battery level forecast with the planned charging and discharging, the electricity
  prices with sell price and solar forecast, the current rule and action, the charging history and the overview
- **Settings:** the planners and their limits, grouped (control, profit, charging, discharging, selling,
  solar), each with a "Read more"
- **Analysis:** what the battery earns and how well BattMind plans:
  - economy per month: charged (grid/solar), discharged, charge price, value of a discharged kWh, spread and savings
  - charged and discharged per rule (the last 12 months)
  - cycles, wear and savings per month
  - the measured round trip loss compared with the configured loss
  - self-sufficiency: where the house got its energy from (solar, battery, grid)
  - the error of each consumption forecast type (average, EMA, trend), with a hint when another type is more accurate
  - a hindsight score: how much of the best possible saving was reached each day
  - the grid charging and sale planned the evening before against what happened
  - days where the battery was empty in the most expensive hours
  - the planned solar surplus against the measured surplus

  The first time, the energy per hour of the last year is read from Home Assistant's long-term
  statistics. Forecast errors, plans and scores are collected from then on.

You can edit the dashboard like any other (**Edit**, or **⋮ → Raw configuration editor** to copy it).
Once you save your changes, your version is kept, and changes to the dashboard in later updates of
the integration no longer show up. Press **Reset dashboard** (on the integration's device) to throw
your changes away and go back to the generated dashboard.

---

## Blueprint: control the home battery

BattMind decides *what* the battery should do (`sensor.<prefix>_powerwall_action`), but does not
control the inverter itself. The blueprint does that – it works with all inverters because you
choose what should happen using Home Assistant's regular action editor.

The blueprint is installed automatically with the integration: on startup it is placed in
`config/blueprints/automation/battmind/battery_control.yaml` and can then be found under
*Settings → Automations & scenes → Blueprints* as **BattMind - control the home battery**.
The file is never overwritten on its own. If a new version arrives with a HACS update (or
you have edited the file yourself), a repair shows up after restart under *Settings →
System → Repairs*. If you choose to update, the file is replaced with the bundled version, and
automations using it are reloaded. If you ignore it, you keep your own file. If the file is
deleted, the latest version is put back on the next restart.
The source is [`battery_control.yaml`](custom_components/battmind/blueprints/battery_control.yaml).

1. Click the blueprint → **Create automation** and select the BattMind sensor.
2. Under **Actions**, add what your inverter should do for each BattMind action –
   typically *Number: Set value* on the battery's max. charge/discharge power, *Switch: Turn on/off* or
   a service from the inverter integration. Fields left empty do nothing.
3. If you have an EV, select the charger's power sensor under **EV (optional)** and what should happen while
   the car is charging (e.g. discharge power 0, so the battery isn't drained into the car). If the battery is still
   allowed to charge the car, select an entity under **Allow discharging into the car when** – e.g. Cable Juice
   Planner's `switch.cjp_<car>_powerwall_discharge_above_needed` – and optionally only above a battery level
   (**Only above this battery level**, e.g. `number.cjp_<car>_ev_charge_after_powerwall_battery_level`,
   together with the home battery's level sensor).
4. Set **Repeat interval** (hours, minutes and seconds, 0 = off) if the action should be run
   again and again – e.g. 30 minutes, because the inverter's forced charge/discharge stops by itself after
   a while (Huawei Solar after 2 hours), or 30 seconds if the battery is controlled via
   Modbus and needs a command at fixed intervals. The repetition starts over every time
   BattMind changes action, and it is also started when Home Assistant starts or automations
   are reloaded.
5. If the action should also run on other events, add your own triggers under
   **Custom triggers** – e.g. when an entity from the inverter changes.

<details>
<summary><b>Example: Huawei Solar (LUNA2000, 5000 W)</b></summary>

Choose *Edit in YAML* in each action field and paste. Replace `YOUR_BATTERY_DEVICE_ID` with
the battery device's id (shown in the address bar on the device's page).

**Charge from grid**
```yaml
- action: number.set_value
  target:
    entity_id:
      - number.batteries_maximum_charging_power
      - number.batteries_grid_charge_maximum_power
  data:
    value: 5000
- action: switch.turn_on
  target:
    entity_id: switch.batteries_charge_from_grid
- action: huawei_solar.forcible_charge
  data:
    device_id: YOUR_BATTERY_DEVICE_ID
    duration: 120
    power: 5000
```

**Forced discharge**
```yaml
- action: number.set_value
  target:
    entity_id:
      - number.batteries_maximum_charging_power
      - number.batteries_grid_charge_maximum_power
      - number.batteries_maximum_discharging_power
  data:
    value: 5000
- action: huawei_solar.forcible_discharge
  data:
    device_id: YOUR_BATTERY_DEVICE_ID
    duration: 120
    power: 5000
```

**Discharge allowed (normal operation)**
```yaml
- action: huawei_solar.stop_forcible_charge
  data:
    device_id: YOUR_BATTERY_DEVICE_ID
- action: number.set_value
  target:
    entity_id:
      - number.batteries_maximum_charging_power
      - number.batteries_grid_charge_maximum_power
      - number.batteries_maximum_discharging_power
  data:
    value: 5000
```

**Solar charging only**
```yaml
- action: huawei_solar.stop_forcible_charge
  data:
    device_id: YOUR_BATTERY_DEVICE_ID
- action: number.set_value
  target:
    entity_id:
      - number.batteries_grid_charge_maximum_power
      - number.batteries_maximum_discharging_power
  data:
    value: 0
- action: number.set_value
  target:
    entity_id: number.batteries_maximum_charging_power
  data:
    value: 5000
```

**Solar charging blocked**
```yaml
- action: huawei_solar.stop_forcible_charge
  data:
    device_id: YOUR_BATTERY_DEVICE_ID
- action: number.set_value
  target:
    entity_id:
      - number.batteries_maximum_charging_power
      - number.batteries_grid_charge_maximum_power
  data:
    value: 0
- action: number.set_value
  target:
    entity_id: number.batteries_maximum_discharging_power
  data:
    value: 5000
```

**While the car is charging**
```yaml
- action: huawei_solar.stop_forcible_charge
  data:
    device_id: YOUR_BATTERY_DEVICE_ID
- action: number.set_value
  target:
    entity_id:
      - number.batteries_maximum_charging_power
      - number.batteries_grid_charge_maximum_power
  data:
    value: 5000
- action: number.set_value
  target:
    entity_id: number.batteries_maximum_discharging_power
  data:
    value: 0
```

Set **Repeat interval** to e.g. 30 minutes (`00:30:00`).

</details>

## Services

| Service | Description |
|---|---|
| `battmind.enforce_planning` | Recalculate the plan now |
| `battmind.recalc_charging_history_today` | Recalculate today's charging history |
| `battmind.debug_info` | Returns internal state for troubleshooting |
| `battmind.export_data` | Save a backup of data and settings (see below) |
| `battmind.import_data` | Replace data and settings with a backup |

All services optionally take `config_entry_id` so they only affect one instance (`import_data` requires it).

---

## Backup: export and import of data

A backup is one JSON file in `/config/battmind_backups/` with the databases, the settings (switches, numbers, ...) and the configuration from the setup. The folder is included in Home Assistant's own backups.

- **When you delete an instance**, a backup is saved automatically (`…_deleted.json`) before the databases are removed.
- **Export** with the *Export data* button on the device, under **Configure → Backup** or with `battmind.export_data`.
- **Import into a new instance**: when you create a new one, the setup offers to restore one of the backups. The configuration is pre-filled, and databases and settings are restored on first start.
- **Import into an existing instance** under **Configure → Backup** or with `battmind.import_data` (`file: battmind_20261024_120000.json`). Databases and settings are replaced, and the integration is reloaded. The current data is first saved as `…_before_import.json`, so an import can be undone. With `restore_configuration` the configuration is also taken over. Leave it off when the backup comes from another Home Assistant.

---

## Switching from the PyScript version

BattMind reads the PyScript files but never changes them. The integration's own data is stored in `.storage`.

1. Add the integration. If `battmind_config.yaml` exists in the config folder, the setup
   asks whether it should be imported:
   - the values from `battmind_config.yaml` are filled in on the pages
   - `battmind_*_db.yaml` (charging history, power values, kWh prices, solar production) are imported on first start
   - if the old `input_boolean` / `input_number` / … `battmind_*` still exist, their values are copied to the new entities
2. If you want to run the two versions side by side, choose a different prefix, e.g. `battmind_ha`.
   The setup rejects a prefix if the PyScript version already has entities with that name.
3. When you are satisfied, remove `pyscript/battmind.py` and `packages/battmind.yaml`, and restart.

| PyScript | Integration |
|---|---|
| `battmind_config.yaml` | Setup / **Configure** in the UI |
| `input_boolean.battmind_*` | `switch.battmind_*` |
| `input_number.battmind_*` | `number.battmind_*` (fixed solar sell price is now in the setup) |
| `input_select.battmind_forecast_type` | `select.battmind_forecast_type` |
| `input_text.battmind_exclude_sell_hours` | `text.battmind_exclude_sell_hours` |
| `input_button.battmind_*` | `button.battmind_*` |
| `pyscript.battmind_debug_info` | `battmind.debug_info` |
| Update scripts and `shell_command` | HACS |

### Fixes compared to the PyScript version

- Consumption history is also stored without solar panels and solar forecast, so the consumption forecast isn't empty.
- "Cheapest hours" no longer plans in hours that have already passed.
- "Sell excess kWh" no longer counts the same selling period multiple times.
- An error in one rule no longer stops the whole planning.
- History is read from an in-memory buffer instead of 70+ recorder queries per planning run.

---

## Troubleshooting

- Turn on **Diagnostic file log** under **Configure → Advanced**. The log is written to
  `battmind_<prefix>.log` in the config folder (2 MB, 3 rotated files).
- Or enable debug logging in `configuration.yaml`:
  ```yaml
  logger:
    logs:
      custom_components.battmind: debug
  ```
- Call `battmind.debug_info` under **Developer tools → Actions**, and attach the response when you open an issue.

---

## License

GPL-3.0, see [LICENSE](LICENSE).
