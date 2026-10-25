# Dashboard and analysis

With **Dashboard in the sidebar** on (default), each BattMind instance gets its own dashboard, e.g.
`/battmind-dashboard`. It is built on every startup from your setup: cards for price providers and a solar
forecast you don't use are removed.

It needs the HACS frontend cards `apexcharts-card`, `mushroom`, `config-template-card` and `card-mod`.

## Overview

| Card | Shows |
|---|---|
| **Battery level forecast** | the forecast battery level for the plan, with the planned 🟢 charging, 🟡 discharging, 🔴 stop (solar only) and forced discharge |
| **Electricity prices** | the buy price incl. taxes, the sell price, the price forecast and the solar forecast. The title shows 🔵 lowest, 🟢 average and 🟣 highest price. A warning with a restart button appears if the price integration is down |
| **Current rule and action** | `sensor.<prefix>_current_charging_rule` and the action, incl. the battery's power right now |
| **Charging history** | each charge and discharge with its rule, kWh and price, and totals per month |
| **Overview** | the plan per day with a reason for each hour (click a row to see the numbers), the bill of the period without/with the battery, and the solar forecast per day |

At the top: **Force planning** recalculates the plan now. The sidebar has the emoji explanation – see [Emojis](emojis.md).

## Settings

All rules, grouped, each group with a **Read more**:

| Group | Contents |
|---|---|
| Control | [whole period optimisation](rules/optimizer.md), forecast type |
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
one, or the measured one after 6 full cycles if *loss from history* is on.

If the measured loss is far from the configured one, check the battery power sensor (and its sign) before
changing the loss.

### Self-sufficiency

Where the house's consumption came from per hour: first solar, then the battery, the rest from the grid.
**Self-sufficiency** = (solar + battery) ÷ consumption. **Export** is solar and battery energy sold.

### Forecast accuracy

The average error per hour between the consumption the plan expected and the measured one, for each
forecast type. ✅ marks the selected type, ⭐ the most accurate the last 30 days. **Bias** > 0: the plan
expected too much. If another type is better, choose it under *Settings → Forecast type*.

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

### History

The required battery level until the next charge (48 hours), kWh charged by solar (7 days) and the
consumption forecast per hour with the three forecast types.

## Editing the dashboard

You can edit the dashboard like any other (**Edit**, or **⋮ → Raw configuration editor** to copy it). Once
you save your changes, your version is kept, and dashboard changes in later updates of the integration no
longer show up. Press **Reset dashboard** (on the integration's device) to throw your changes away and go
back to the generated dashboard.
