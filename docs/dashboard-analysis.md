# Dashboard and analysis

With **Dashboard in the sidebar** on (default), each BattMind instance gets its own dashboard, e.g.
`/battmind-dashboard`. It is built on every startup from your setup: cards for price providers and a solar
forecast you don't use are removed.

It needs these HACS frontend cards: [ApexCharts Card](https://github.com/RomRider/apexcharts-card),
[Mushroom](https://github.com/piitaya/lovelace-mushroom),
[Config Template Card](https://github.com/iantrich/config-template-card) and
[card-mod](https://github.com/thomasloven/lovelace-card-mod).

## Overview

| Card | Shows |
|---|---|
| **Battery level forecast** | the forecast battery level for the plan, with the planned 🟢 charging, 🟡 discharging, 🔴 stop (solar only) and forced discharge |
| **Electricity prices** | the buy price incl. taxes, the sell price, the price forecast and the solar forecast. The title shows 🔵 lowest, 🟢 average and 🟣 highest price. A warning with a restart button appears if the price integration is down |
| **Current rule and action** | `sensor.<prefix>_current_charging_rule` and the action, incl. the battery's power right now |
| **Charging history** | each charge and discharge with its rule, kWh and price, and totals per month |
| **Overview** | the plan per day with a reason for each hour (click a row to see the numbers), the bill of the period without/with the battery (with [Cable Juice Planner](cookbook.md#together-with-cable-juice-planner-ev) also the cars' planned charging and the bill incl. the cars), and the solar forecast per day |
| **Status** | above the emoji explanation: the battery level and power, house consumption, solar production and kWh charged by solar, the current buy price and `sensor.<prefix>_analysis` (savings this month). Rows whose entity is not configured are left out |

At the top: **Force planning** recalculates the plan now. The sidebar has the emoji explanation – see [Emojis](emojis.md).

## Settings

All rules, grouped, each group with a **Read more**:

| Group | Contents |
|---|---|
| Control | [whole period optimisation](rules/optimizer.md), forecast type, [correct the price forecast](how-it-works.md#prices) |
| Profit requirements | [min. profit per kWh, min. total profit, min. kWh for export, excluded sell hours](rules/selling.md#profit-requirements) |
| Charging from the grid | [cheapest periods, fill up, most expensive periods, required before max level, negative prices](rules/charging.md) |
| Discharging | [profit only, prioritise by energy cost, before the sun, max. age of kWh](rules/discharging.md) |
| Selling | [maximise profit, sell excess kWh, use midnight battery level, buy to sell](rules/selling.md) |
| Solar | [solar surplus charging, optimise solar charging](rules/solar.md) |

The plan is recalculated 5 seconds after a change.

## Analysis

What the battery earns, and how well BattMind plans. Everything is published on `sensor.<prefix>_analysis`.

The first time, the energy per hour of the last year is read from Home Assistant's **long-term
statistics** (the sensors need a `state_class`). Forecast errors, plans and scores only exist from the
first start, so those sections fill up over the first days.

### Economy per month

From the charging history:

| Column | Meaning |
|---|---|
| Charged (☀️) | kWh into the battery, in brackets the part from solar |
| Charge price | what a kWh in the battery cost: grid at the buy price, solar at the sell price it would otherwise have earned |
| Discharged | kWh out of the battery |
| Value | what a discharged kWh was worth: the energy the house didn't buy, plus sales |
| Spread | value minus charge price – the margin per kWh before loss and wear. The bigger, the better the battery moves energy from cheap to expensive hours |
| Avg. price | the month's average buy price over all hours |
| Savings | what the battery saved, after loss and wear |

### Per rule (12 months)

Charged and discharged kWh per rule. For charging, **est. gain** is the charged kWh after loss, valued
like the battery's discharges on average, minus their price and wear. For discharging, it is the actual
savings. Both tables show the same money from two sides – don't add them up.

### Cycles and wear

One cycle = discharged kWh equal to the battery size. **Wear** = wear per kWh × charged kWh. Use it to
check the wear you set against the battery's guarantee (e.g. 6000 cycles).

### Measured round trip loss

`1 − (discharged + change of the battery level) ÷ charged`, measured with the battery's power sensor (AC to
AC), per month, the last 30 days and in total. **Used now** is the loss BattMind plans with: the configured
one, or the measured one after 6 full cycles if *loss from history* is on. When efficiency curves are
configured, **Used now** is their combined loss at the configured maximum charge/discharge power.

If the measured loss is far from the configured one, check the battery power sensor (and its sign) before
changing the loss.

### Battery efficiency

BattMind measures the charging and discharging efficiency at each AC power itself. Every hour it goes
through the battery power and battery level since the last time: between two changes of the battery
level the battery moved exactly the difference of the two levels, so the AC energy of the power sensor
in that time is compared with it (charging: battery kWh ÷ AC kWh, discharging: AC kWh ÷ battery kWh).
A measurement covers at least 1 % battery level and 2 minutes; it is skipped when the battery was idle
for more than 10 % of the time, ran the other way for more than 3 % of the energy, or the power sensor
was unavailable. The measurements are added up per 500 W step of AC power; consecutive measurements in
the same step share their boundaries, so the rounding of the battery level cancels out. A step is used
once it has at least 20 measurements and 2 kWh (efficiencies above 100 % are capped at 100 %, below
50 % are not used). Beyond 100 kWh in a step the older measurements count half, so the curve follows
an ageing battery.

The first time, BattMind reads the battery power and level from the Home Assistant recorder history
(up to 30 days, as far as the recorder keeps it; one day at a time in the background), so the curve
does not have to wait for new data. Only the history before the hourly measuring started is added,
so nothing is counted twice. BattMind's own charging history cannot be used for this: it stores the
energy per hour, and its battery percentages are calculated from that energy, not measured. The
long-term statistics only have hourly means, which cannot show when the battery level changed.

The solid lines are the curves BattMind plans with: a configured curve, otherwise the measured one. The
dashed lines show every measured step, including the ones without enough measurements yet. The table
below says per direction which curve is used, how many steps are in use and how much energy has been
measured. Without a curve the round-trip loss is used.

The configured battery size converts the battery level to kWh. If it is wrong, the charging and
discharging efficiency move in opposite directions; their product, the round trip, stays right.

BattMind interpolates linearly between the points and uses the nearest endpoint outside the range, just
as the whole-period optimiser does. **Enhance battery efficiency** under **Settings → Control** decides
whether the curves are used in calculations. Turning it off keeps the graph, the saved curves and the
measurements, but planning uses the round-trip loss.

### Self-sufficiency

Where the house's consumption came from per hour: first solar, then the battery, the rest from the grid.
**Self-sufficiency** = (solar + battery) ÷ consumption. **Export** is solar and battery energy sold.

### Forecast accuracy

The average error per hour between the consumption the plan expected and the measured one, for each
forecast type. ✅ marks the selected type, ⭐ the most accurate the last 30 days.

| Column | Meaning |
|---|---|
| Avg. kWh/hour | what each type expected per hour on average over 30 days. The 📏 **Actual** row at the top is the measured consumption |
| Error 7 / 30 days | the average error per hour in kWh |
| Relative | the error as a share of the consumption |
| Bias | > 0: the plan expected too much on average |

The error is counted hour by hour, so a type can hit the average and still miss a lot in the single
hours: the type closest to the actual average is not always ⭐. When another type is more accurate, a
warning shows the numbers behind the suggestion: both types' error, how much smaller it is, and what
each expected against the actual consumption. Choose the type under *Settings → Forecast type*.

### Hindsight score

How much of the possible saving was reached each day:

`score = (cost without battery − actual cost) ÷ (cost without battery − best possible cost)`

The best possible cost is calculated afterwards with the day's actual consumption, solar and prices, the
same start and end level, power, loss and wear. Forecast errors therefore count against the score – 100 %
would require knowing the future.

### Planned against actual

Every evening at 23 BattMind saves what the plan wants to charge from the grid and sell the next day, and
compares it with what happened. Big differences come from forecast errors, and from plans that change
during the day when prices or the battery level change.

### Empty in the expensive hours

Days where the battery was empty (at most 2 % above the min. level) in one of the day's 4 most expensive
hours, and the kWh and money bought from the grid in them. Many such days suggest:
- the battery is too small for the evening, or
- the consumption forecast is too low (see **Forecast accuracy**), or
- the profit requirements are so high that BattMind doesn't charge.

### Solar forecast

The solar surplus the plan expected for the next day (saved the evening before) against the measured
surplus: **Plan** is the combined forecast, **History (weather)** the part from the stored solar history,
**Solcast** the forecast integration. **Bias** > 0: too much sun was expected.

### Price forecast

How well the price integration's forecast (e.g. Carnot in Energi Data Service, or Strømligning) hit the
real buy price. Every hour BattMind saves the forecast for the coming days, without its own small adder
per day ahead. When the real prices of a day are published (around 13:00 the day before), they are compared
with what the forecast said 1, 2, 3 … days before.

| Column | Meaning |
|---|---|
| Days ahead | how many days before the forecast was made. 1 = the day before, before the real prices came |
| Days | days compared the last 30 days |
| Error 7 / 30 days | the average error per hour in kr/kWh |
| Bias | > 0: the forecast was too high on average |
| Worst hour | the biggest error in a single hour |
| Cheapest found | how many of the day's 4 cheapest hours by the forecast really were among the 4 cheapest – the hours the plan charges in. In brackets the extra price per kWh of picking them by the forecast |
| Most expensive found | the same for the 4 most expensive hours – the hours the plan uses the battery in |
| Correction | what the plan adds to the forecast prices for that day ahead: minus the bias, after 7 days of measurements and at most ±0.50 kr. – while there are too few days, or when *Correct the price forecast* (*Settings → Control*) is off |

The level of the forecast matters less than the order of the hours: a forecast that is 0.30 kr too high
but finds the right cheap and expensive hours plans almost as well as the real prices. The charts show the
real price against the forecast 1 to 7 days before per hour (from 2 days ago to 4 days ahead), the
corrected forecast the plan uses for the coming hours, and the error per day for the last 31 days.

The measurements always use the forecast as the price integration published it, never the corrected one,
so the correction does not measure itself.

Only the buy price is measured, and only hours with a forecast from the price integration. Hours BattMind
fills from its own price history are not included. The first numbers come at 13:00 the day after the first
start, "3 days ahead" after 3 days.

### History

The required battery level until the next charge (48 hours), kWh charged by solar (7 days) and the
consumption forecast per hour with the three forecast types.

### What-if simulator

A short description and a link to **Settings → Developer tools → Actions** with the
**battmind.what_if** action preselected (requires an administrator). See
[What-if simulator](what-if-simulator.md).

## Editing the dashboard

You can edit the dashboard like any other (**Edit**, or **⋮ → Raw configuration editor** to copy it). Once
you save your changes, your version is kept, and dashboard changes in later updates of the integration no
longer show up. Press **Reset dashboard** (on the integration's device) to throw your changes away and go
back to the generated dashboard.
