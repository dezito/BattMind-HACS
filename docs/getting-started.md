# Getting started

This page takes you from installation to a battery that BattMind controls. Plan about 30 minutes, and a
few days of watching the plan before you trust it.

## 1. Check what you need

| You need | Why |
|---|---|
| A **battery level** sensor (%) | where the plan starts from |
| A **battery power** sensor (W or kW) | measures what goes in and out (charging history, cost price, loss) |
| A **house consumption** sensor (W or kW) – the house's consumption, not the grid import | the consumption forecast per hour |
| [Energi Data Service](https://github.com/MTrab/energidataservice) and/or [Strømligning](https://github.com/MTrab/stromligning) | buy and sell prices per hour |
| A way to control the inverter from Home Assistant | BattMind only decides – your automation does it |

Optional, but recommended with solar panels:

- a **solar production** sensor (W or kW)
- [Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar)
- a weather integration with `weather.get_forecasts` (Met.no, AccuWeather, OpenWeather …)

For the dashboard you need the HACS frontend cards `apexcharts-card`, `mushroom`, `config-template-card` and `card-mod`.

> [!TIP]
> Before you start, find out how your inverter is controlled: which entities or services set forced charging,
> forced discharging and the max. charge/discharge power. Look under *Developer tools → Actions* and *States*
> for your inverter integration. You need them in [step 5](#5-control-the-battery).

Optional for additional battery protection: a battery temperature sensor. The inverter/BMS remains the
primary safety system.

## 2. Install

1. **HACS** → ⋮ → **Custom repositories**: add `https://github.com/dezito/BattMind-HACS` as **Integration**.
2. Search for **BattMind**, install it, and restart Home Assistant.
3. **Settings → Devices & services → Add integration → BattMind**.

## 3. Go through the setup

The setup is split into small pages. Every field has a help text – here are the ones that matter most.

### Prefix and language

The prefix starts every entity id: with `battmind` you get `sensor.battmind_powerwall_action`. Use one
prefix per battery. If the [PyScript version](migration.md) still runs, choose another prefix (e.g.
`battmind_ha`).

### Home battery – sensors

- **Battery power:** BattMind expects **positive while discharging, negative while charging**. If your
  sensor is the other way round, enable **Invert**. Check it in *Developer tools → States* while the
  battery charges.
- **Battery temperature:** optional. When selected, BattMind requests `battery_stopped` outside the safe
  range and while the sensor is unavailable. Configure **Stop battery** in the control blueprint first.

### Home battery – specifications

| Field | How to set it |
|---|---|
| Battery size | the **usable** capacity in kWh |
| Min. level | the reserve BattMind never plans below (e.g. 5–10 %) |
| Max. level | the highest level BattMind charges to (usually 100 %) |
| Charge / discharge power | the battery's max. power in W – limits how many kWh fit in an hour |
| Charge/discharge loss | the **round trip** loss as a fraction: 0.1 = 10 %. A typical lithium battery with inverter loses 8–15 % |
| Loss from history | after 6 full cycles BattMind measures the loss itself – leave it on |
| Charging / discharging efficiency curve | optional object mapping AC power in W to efficiency in percent, e.g. 1000 W = 88 %, 3000 W = 94 %, 5000 W = 96 %. BattMind interpolates between points. Leave empty to let BattMind measure the curve itself (the round-trip loss is used until there are enough measurements) |
| Enhance battery efficiency | switch under **Settings → Control** that decides whether BattMind uses the configured curves. Turning it off keeps the curves and uses the round-trip loss instead |
| Wear per kWh | battery price ÷ expected lifetime in kWh, e.g. 40 000 kr ÷ (10 kWh × 6000 cycles) ≈ 0.67 kr/kWh. Default is 0.10. A higher value makes BattMind more careful |
| Min. / max. safe temperature | limits in °C from the battery manufacturer; defaults are 0 to 50 °C |

The loss and the wear decide how big a price difference has to be before BattMind uses the battery. See
[How BattMind plans](how-it-works.md#the-cost-of-a-kwh-from-the-battery).

### Consumption

- **House consumption:** the house's own consumption. Tell BattMind if the sensor includes the battery's
  charging or discharging, so it isn't counted twice.
- **Ignored consumers:** power sensors subtracted from the house, e.g. the EV charger. They aren't in the
  forecast, so the battery isn't planned to cover them.

### Prices

Choose Energi Data Service and/or Strømligning. With both, the first one with a price for an hour is used,
and the other fills the gaps.

- **Energi Data Service:** enable *Carnot* in Energi Data Service to plan beyond the published prices.
  Without a separate sell sensor, the buy price minus tariffs is used as sell price.
- **Strømligning:** the buy sensors are required. The sell sensors and the tariff page give an accurate
  sell price.
- **Refund** is subtracted from the buy price, **sell fee** is included in the sell price.

### Solar panels and weather (optional)

Leave the page empty without solar. With solar:

- **Inverter's max. power:** limits solar and battery power together.
- **Fixed solar sell price:** −1 uses the hourly sell price. Set a price if your production agreement pays
  a fixed price and settles the difference to the spot price – see [Fixed solar sell price](rules/solar.md#fixed-solar-sell-price).
- **Solcast:** select e.g. `sensor.solcast_pv_forecast_forecast_today`. The other days are found automatically.
- **Weather:** the hourly and daily forecast. Together with the stored solar history per weather type it
  gives a solar forecast that learns your roof – also without Solcast.

### Planning (advanced)

| Field | Default | Meaning |
|---|---|---|
| Days to plan | 3 | the plan covers today and the next days. More days give better decisions about saving energy for later, but cost more CPU and rely on price forecasts |
| Interval | 5 min | how often the plan is checked. It is only recalculated when prices, battery level or settings have changed |
| Dashboard | on | a ready-made dashboard in the sidebar |
| Testing mode | off | marks the rule with 🧪, so a test instance can be told apart |

Everything can be changed later under **Configure** on the integration.

## 4. Look at the plan

Open the **BattMind** dashboard in the sidebar:

- **Overview** shows the battery level forecast, the prices and the plan with a reason for every hour.
  Click a row in the plan to see the numbers behind it.
- **Settings** has all the rules. They are all on by default, and the
  [whole period optimisation](rules/optimizer.md) controls the battery.

Give it a day or two. The consumption forecast gets better as BattMind stores the consumption per hour,
and the solar forecast gets better as it stores the solar surplus per weather type.

## 5. Control the battery

BattMind only sets `sensor.<prefix>_powerwall_action`. The automation that makes the inverter follow it is
yours. The easiest way is the blueprint that comes with the integration:

1. *Settings → Automations & scenes → Blueprints* → **BattMind - control the home battery** → **Create automation**.
2. Select the BattMind sensor.
3. For each of the six actions, add what your inverter should do. See [Actions](actions.md) for what each
   action means, and [Huawei Solar](inverters/huawei.md) for a complete example.
4. Keep **Repeat interval** at 5 minutes (default): the action is sent again, so a lost command or a mode
   the inverter changed by itself is put right. Use 30 seconds for a battery controlled via Modbus.

> [!TIP]
> Start with the automation **off** and watch `sensor.<prefix>_powerwall_action` for a day. When the actions
> look right, turn it on.

## 6. Next steps

- [What-if simulator](what-if-simulator.md) - replay your history with another battery, controller (rules or optimisation) or planning settings

- [How BattMind plans](how-it-works.md) – the model behind the decisions
- [Recipes](cookbook.md) – settings for typical setups
- [Dashboard and analysis](dashboard-analysis.md) – how to see what the battery earns
