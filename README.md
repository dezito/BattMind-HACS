<img src="custom_components/battmind/brand/icon.png" alt="BattMind" width="128" align="right">

# BattMind🤵‍♂️🔋⚡ for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
[![GitHub Release](https://img.shields.io/github/v/release/dezito/BattMind-HACS)](https://github.com/dezito/BattMind-HACS/releases)
[![License](https://img.shields.io/github/license/dezito/BattMind-HACS)](LICENSE)

Home battery optimisation that aims to save and earn as much money as possible from the battery.

BattMind uses electricity prices, house consumption, solar forecasts, battery losses and wear to work
out when the home battery should charge from the grid, discharge or sell power. The result is exposed as an action
(`sensor.battmind_powerwall_action`) that you use to control your inverter yourself.

## How BattMind compares with other home battery optimisers

Home battery optimisation can mean different things: a Home Assistant integration, a separate energy
optimisation service, or software that directly controls supported inverters.

BattMind plans in Home Assistant and exposes what the battery should do. An automation, such as the
included blueprint, applies the action to your inverter.

The examples below compare published approaches, not prices or performance in a controlled test.
Features, supported hardware and tariffs can change, and actual savings depend on prices, forecasts,
battery losses and household consumption.

BattMind's main goal is to minimise the electricity bill and maximise profitable export from the battery.
It combines whole-period optimisation with explainable rules and analyses what the battery actually
earned and how accurately the plan performed.

BattMind does more than select cheap charging hours. It tracks the cost of the energy stored in the
battery, including its source, charging and discharging losses, and battery wear. The plan can use this
to decide whether energy should be used now, kept for a more expensive period, or sold to the grid.

Compare the projects by approach and use case. The table describes features from BattMind's
documentation and the linked projects' published descriptions.

| Area | BattMind | [Predbat](https://github.com/springfall2008/batpred) | [EMHASS](https://github.com/davidusb-geek/emhass) | [Battery Controller](https://github.com/bvweerd/battery_controller) |
| --- | --- | --- | --- | --- |
| Focus and setup | Maximising battery savings and export income in a native HACS integration. | Home Assistant app with broad inverter support and deep configuration. | Add-on, Docker or Python optimisation of batteries and flexible loads. | Native HACS integration for vendor-independent battery control. |
| Planning | Whole-period optimisation or selectable rules, 1–7 days ahead. | Lowest-cost plan from available price, load and solar forecasts. | Configurable mathematical optimisation horizon and time step. | Dynamic programming over the available and modelled forecast horizon. |
| Prices and forecasts | Buy/sell prices, stored-energy cost, Solcast and forecasts learned from HA history. | Import/export tariffs with configured solar and load forecasts. | Configurable price, PV and load forecasts, including ML options. | Import/export prices, PV forecasts and learned household consumption. |
| Loss and wear | Round-trip loss, optional charge/discharge efficiency curves and configurable wear. | Separate battery, inverter and charging losses plus cycle cost. | Battery efficiency and stress cost can be modelled. | Efficiency curves and degradation cost. |
| Charging and export | Grid charging, preserving energy for expensive hours and profitable forced export. | Modes from monitoring to automatic charging and forced export. | Determined by the selected model and connected automations. | Charge and discharge setpoints for several control modes. |
| Inverter control | Generic actions mapped by the included blueprint. | Direct control of many supported inverter integrations. | Plan output connected to HA automations. | Generic setpoints connected through HA automations. |
| Explanation and analysis | Named rules plus savings, wear, forecast accuracy and hindsight score. | Detailed plan, status, cost reports, web console and logs. | Publishes forecast and optimisation results. | Schedule, profitability and browser-based diagnostics. |
| Best suited for | Getting the best financial result from a home battery with transparent decisions and detailed evaluation. | Extensive inverter support and advanced control. | Whole-home energy and flexible-load optimisation. | Generic real-time battery setpoint control. |

Related projects include [PV Opt](https://github.com/stevebuk1/pv_opt), which focuses primarily on
Solis inverters and time-of-use tariffs, and
[Smart Battery Pilot](https://github.com/cygnusb/ha-smart-battery-pilot), a vendor-neutral Home Assistant
integration for price, consumption and PV-based battery planning.

### BattMind's trade-offs

| ✅ Advantages | ⚠️ Trade-offs |
| --- | --- |
| Combines prices, solar, consumption, loss and wear in one multi-day plan. | Good results depend on reliable data and forecasts. |
| Offers whole-period optimisation or individually selectable, explainable rules. | More options require more setup than a simple price scheduler. |
| Tracks stored-energy cost and evaluates savings, forecasts, cycles and wear. | Analysis requires suitable Home Assistant recorder statistics. |
| Works with any controllable inverter through generic actions and a blueprint. | You must map and test the inverter commands yourself. |

---

> [!NOTE]
> This is the **integration version** of BattMind, installed via HACS. It replaces the
> [PyScript version](https://github.com/dezito/BattMind) and can import its configuration and
> databases. See [Switching from the PyScript version](docs/migration.md).

> [!IMPORTANT]
> First HACS release. Please open an [issue](https://github.com/dezito/BattMind-HACS/issues) if something doesn't work.

📖 **[Documentation](docs/README.md)** – [getting started](docs/getting-started.md), [how BattMind plans](docs/how-it-works.md),
every rule explained with examples, [inverter setups](docs/inverters/huawei.md) and [FAQ](docs/faq-troubleshooting.md).

---

## Features

- **Charging plan for several days** (1–7 days) based on electricity prices, forecasts and consumption history
- **Two ways to control the battery** – [read more](docs/rules/optimizer.md)
  - **Whole period optimisation** (default): calculates the cheapest action for every hour of the whole forecast
    period at once (charging loss, wear, solar, buy and sell prices and the profit requirements). Every action is
    shown as the rule it corresponds to, and the rule switches decide which actions it may use
  - **Rules:** the charge and discharge rules below decide each hour
- **[Charging rules](docs/rules/charging.md) that can be switched on and off individually** – solar surplus always
  charges first, and grid charging never pushes out solar that would be sold for less than the grid kWh cost
  - **Cheapest periods:** charge in the cheapest periods for the day's consumption (number of periods and price limits are configurable)
  - **Fill up during cheapest period:** charge the battery fully in the cheapest period
  - **Most expensive periods:** charge for the most expensive hours the battery doesn't cover
  - **Required before max level:** only charge what is missing before the solar panels fill the battery
  - **Negative prices:** fill the battery when charging pays, even if the energy is never used
  - **Buy to sell:** charge cheaply from the grid only to sell the kWh again when it pays after loss and wear
- **[Discharging rules](docs/rules/discharging.md)**
  - **Profit only:** block discharging in hours where the battery's kWh cost (incl. loss and wear) is higher than the price
  - **Prioritise by energy cost:** save the battery for the most expensive hours until it is charged again
  - **Before the sun:** use/sell the battery before the sun fills it anyway and the surplus would be sold cheaply
  - **Max. age of kWh:** energy older than X hours is used even without profit, and sold when that pays more
- **[Selling rules](docs/rules/selling.md)**
  - **Maximise profit:** sell in the most expensive periods when that pays more than using the energy yourself
  - **Sell excess kWh:** sell the energy that isn't used before the battery is empty or full again (forced discharge)
  - **Use midnight battery level:** only look until midnight instead of the whole forecast period
  - min. profit per kWh and in total, min. kWh per sale, reserve for the house, and exclude specific selling hours
- **[Solar](docs/rules/solar.md) (optional)**
  - **Solar surplus charging:** include the solar surplus in the plan
  - **Optimise solar charging:** sell the solar surplus in the expensive hours and fill the battery in the cheap solar hours instead
  - forecast from [Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar) and/or own history per cloud cover
- **Consumption forecast** from own history: average, EMA or trend
- **Price providers:** [Energi Data Service](https://github.com/MTrab/energidataservice) and/or
  [Strømligning](https://github.com/MTrab/stromligning), incl. sell price, tariffs, selling fee and refund;
  forecast prices are corrected with their measured bias
- **[Cost price of the battery's energy](docs/how-it-works.md#the-cost-of-a-kwh-from-the-battery):** what the energy in the battery has cost (grid, solar, charge/discharge loss and wear)
- **Detailed charging history and overview** as markdown
- **[Analysis](docs/dashboard-analysis.md#analysis)** of what the battery earns and how well BattMind plans
- **[What-if simulator](docs/what-if-simulator.md):** compare battery capacity, power, loss, wear and reserve without changing the active plan
  - economy per month: charged (grid/solar), discharged, charge price, value of a discharged kWh, spread and savings
  - charged and discharged per rule, cycles and wear
  - measured round trip loss against the configured loss, and self-sufficiency (solar, battery, grid)
  - the error of each consumption forecast type against the actual consumption, and why another type is suggested when it is more accurate
  - the price forecast against the real prices per day ahead: the error, and how often it found the cheapest and most expensive hours
  - a hindsight score per day, planned against what happened, days with an empty battery in the most expensive
    hours, and planned against measured solar surplus
  - the first time, the last year is read from Home Assistant's long-term statistics
- **[Ready-made dashboard](docs/dashboard-analysis.md)** in the sidebar with overview, settings and analysis, built from your setup
- **Fail-safe:** BattMind stores the prices in its own [price database](docs/how-it-works.md#prices). Hours
  without a price are filled from it, and if the price integration is down, the last prices are kept for 2
  hours, and then planning falls back to the database
- **Multiple instances:** each instance has its own prefix (e.g. one per battery)
- **Languages:** Danish, English, German, Polish and Ukrainian
- **Does not run in Home Assistant's main thread:** all calculations run in BattMind's own worker thread, so
  Home Assistant is not blocked while the plan is calculated

---

## How to control the home battery

BattMind sends **no commands** to your inverter. It exposes what the battery should do right now:

| `sensor.battmind_powerwall_action` | Meaning |
|---|---|
| `battery_stopped` | Stop charging and discharging because the configured battery temperature is unsafe or unavailable |
| `grid_charging` | Charge the battery from the grid now |
| `discharge_allowed` | Normal operation: the battery isn't really controlled – it charges from solar and discharges to the house as the inverter normally does |
| `force_discharge` | Discharge / sell to the grid now (the most expensive hours) |
| `force_solar_only_charging` | Charge from solar only, no discharging |
| `blocked_solar_charge` | Don't charge from solar (the surplus is sold instead) |

The easiest way is the **blueprint** that is installed with the integration
(*Settings → Automations & scenes → Blueprints → BattMind - control the home battery*). It works with all
inverters: you add what your inverter should do for each action, and optionally what should happen while
an EV is charging. See [Actions and battery control](docs/actions.md) and the complete
[Huawei Solar example](docs/inverters/huawei.md).

The blueprint file is never overwritten on its own. If a new version arrives with a HACS update (or you
have edited the file yourself), a repair shows up after restart under *Settings → System → Repairs*. If
you choose to update, the file is replaced with the bundled version, and automations using it are
reloaded. If the file is deleted, the latest version is put back on the next restart.

`sensor.battmind_current_charging_rule` shows why (e.g. *Charging up to 💸*), and
`sensor.battmind_battery_level_needed` shows how much battery is needed until the next charge.

---

## Requirements

- Home Assistant **2025.1** or newer with recorder (default)
- A sensor for the home battery's **battery level** (%) and **power** (W)
- A sensor for the **house consumption** (W)
- Price integration: [Energi Data Service](https://github.com/MTrab/energidataservice) and/or [Strømligning](https://github.com/MTrab/stromligning)
- Optional:
  - battery temperature sensor for a configurable charge/discharge safety stop
  - solar production (W)
  - [Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar)
  - a weather integration that supports `weather.get_forecasts` with cloud cover, e.g.
    [Meteorologisk institutt](https://www.home-assistant.io/integrations/met/),
    [AccuWeather](https://www.home-assistant.io/integrations/accuweather/) or
    [OpenWeather](https://www.home-assistant.io/integrations/openweathermap/)
- For the dashboard (install these HACS frontend cards): [ApexCharts Card](https://github.com/RomRider/apexcharts-card),
  [Mushroom](https://github.com/piitaya/lovelace-mushroom),
  [Config Template Card](https://github.com/iantrich/config-template-card) and
  [card-mod](https://github.com/thomasloven/lovelace-card-mod)

---

## Installation

### HACS (recommended)

[![Open your Home Assistant and add the repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=dezito&repository=BattMind-HACS&category=integration)

1. **HACS** → ⋮ → **Custom repositories**: add `https://github.com/dezito/BattMind-HACS` as **Integration**.
2. Search for **BattMind**, install it, and restart Home Assistant.
3. Add the integration:

   [![Open your Home Assistant and add BattMind.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=battmind)

   or **Settings → Devices & services → Add integration → BattMind**.
4. Create the automation that controls the home battery, e.g. with the [blueprint](docs/actions.md#the-blueprint).

### Manual

Copy `custom_components/battmind` to `config/custom_components/battmind` and restart Home Assistant.

---

## Setup

Setup is done in the UI, split into small pages with help texts:

1. **Prefix and language:** the prefix becomes the start of all entity ids (default `battmind`)
2. **Home battery:** battery level, power (optionally inverted sign), size, min./max. level,
   charge/discharge power, charge/discharge loss, optional efficiency curves and wear per kWh. The curves can be enabled or disabled from **Settings** without deleting them
3. **Consumption:** the house consumption, and whether it includes the battery's charging/discharging. You can also
   ignore consumers, e.g. the EV charger.
4. **Price providers:** Energi Data Service and/or Strømligning (buy, sell and tariffs), refund and selling fee
5. **Solar panels:** production, the inverter's max. power, fixed sell price or hourly price, and Solcast
6. **Weather:** hourly and daily forecast (only with solar panels)
7. **Advanced:** number of days to plan, interval, dashboard, diagnostic file log and testing mode
8. **Database:** how much history is stored

Everything can be changed afterwards under **Configure** on the integration. The integration reloads when you save.
[Getting started](docs/getting-started.md) goes through every page and how to set the values.

---

## Entities

The entity ids start with the prefix, here `battmind`.

### Settings (changed from the dashboard)

| Type | Entities |
|---|---|
| `switch` | Solar surplus charging, cheapest periods, fill up during cheapest period, most expensive periods, required before max level, discharge only at profit, prioritise discharging by energy cost, sell excess kWh, use midnight battery level, maximise profit, forced discharge of old kWh, optimise solar charging, negative prices, buy to sell, discharge before the sun, control the battery with the whole period optimisation, correct the price forecast |
| `number` | Cheap price periods, increase for cheapest price / cheap periods, min. profit per kWh, min. total profit, min. kWh for export, max. hours for old kWh |
| `select` | Forecast type (average, EMA or trend) |
| `text` | Exclude selling hours (e.g. `17,18,19`) |
| `button` | Force planning, export data (a [backup](#backup-export-and-import-of-data)), reset dashboard |

The plan is recalculated 5 seconds after a setting is changed. The values are kept across restarts.

### Results

| Entity | Content |
|---|---|
| `sensor.battmind_powerwall_action` | What the battery should do now (see [above](#how-to-control-the-home-battery)), the planned hours per action as [attributes](docs/actions.md#sensors-and-attributes) |
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
e.g. `/battmind-dashboard`, with three views:

- **Overview:** the battery level forecast with the planned charging and discharging, the electricity
  prices with sell price and solar forecast, the current rule and action, the charging history and the overview
- **Settings:** the planners and their limits, grouped (control, profit, charging, discharging, selling,
  solar), each with a "Read more"
- **Analysis:** what the battery earns and how well BattMind plans

Every card is explained in [Dashboard and analysis](docs/dashboard-analysis.md). Once you save your own
changes to the dashboard, your version is kept. Press **Reset dashboard** (on the integration's device) to
go back to the generated dashboard.

---

## Services

| Service | Description |
|---|---|
| `battmind.enforce_planning` | Recalculate the plan now |
| `battmind.recalc_charging_history_today` | Recalculate today's charging history |
| `battmind.debug_info` | Returns internal state for troubleshooting |
| `battmind.export_debug` | Save an anonymised debug file for a GitHub issue |
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

If `battmind_config.yaml` exists in the config folder, the setup offers to import its configuration and
databases, and the values of the old helpers are copied to the new entities. The PyScript files are only
read, never changed, so both versions can run side by side with different prefixes.
See [Switching from the PyScript version](docs/migration.md) for the steps and what became what.

---

## Troubleshooting

See [FAQ and troubleshooting](docs/faq-troubleshooting.md). In short:

- Turn on **Diagnostic file log** under **Configure → Advanced** (`battmind_<prefix>.log` in the config folder).
- Found a bug? Press **Export debug info** on the device (or run `battmind.export_debug`) right after it happened, and follow the
  notification: it links to the file and to a prefilled GitHub issue. See [Report a problem](docs/faq-troubleshooting.md#report-a-problem).

---

## License

GPL-3.0, see [LICENSE](LICENSE).
