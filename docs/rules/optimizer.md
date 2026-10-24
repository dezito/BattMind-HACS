# Whole period optimisation

**Switch:** *Control the battery with the whole period optimisation* (`switch.<prefix>_optimizer_control`) – on by default.

## What it does

The rules decide one thing at a time: first what to charge, then what to sell. Each rule is sensible on its
own, but they can't see each other's consequences – a sale may make a charge later necessary, which costs
more than the sale earned.

The optimisation looks at the **whole plan at once** (days to plan, default 3). For every hour it picks one
of the five [actions](../actions.md), so the total electricity bill of the period is as low as possible:

| Action | In the model |
|---|---|
| `discharge_allowed` | solar charges the battery, and without solar the battery covers the house |
| `force_solar_only_charging` | solar charges the battery, the house uses the grid – the energy is saved |
| `grid_charging` | the battery charges from the grid (25, 50, 75 or 100 % of the hour) on top of solar |
| `force_discharge` | the battery covers the house and sells the rest (25–100 % of the hour) |
| `blocked_solar_charge` | the solar surplus is sold instead of charging the battery |

The calculation includes the charge and discharge power, the min./max. level, the loss, the wear, the buy
and sell prices per hour, the forecast consumption and solar surplus, and the
[profit requirements](selling.md#profit-requirements).

The energy left in the battery at the end of the period is valued at the average buy price (after wear
and min. profit). Without that, the cheapest plan would always empty the battery in the last hours.

## How it relates to the rules

The rules still run, also when the optimisation controls the battery:

- **Every action is explained as a rule.** A charge followed by a sale is shown as *Buy to sell*, a charge
  before the sun fills the battery as *Required before max level*, a held-back hour as *Prioritise by energy
  cost* and so on – with the same emojis as the rules, and with the hour the energy is for.
- **The rule switches decide what the optimisation may do.** If the rule an action corresponds to is
  switched off, that action is forbidden in that hour, and the period is optimised again.
- **The rules are the fallback.** If the optimisation fails, the rule plan controls the battery.

Which switches allow which kind of action:

| The optimisation wants to … | Shown as | Allowed when one of these is on |
|---|---|---|
| charge from the grid for later use | Cheapest periods, Most expensive periods or Required before max level | Cheapest periods, Most expensive periods, Required before max level |
| charge at a price ≤ 0 | Negative prices | Negative prices, or one of the three above |
| charge and sell the kWh again | Buy to sell | Buy to sell (off: the sale is forbidden, the charge may still be used in the house) |
| sell energy from the battery | Maximise profit | Maximise profit, Sell excess kWh, Max. age of kWh |
| sell before the sun fills the battery | Before the sun | Before the sun, or one of the three above |
| hold the energy back for a more expensive hour | Prioritise by energy cost | Prioritise by energy cost, Profit only |
| sell the solar surplus and charge later | Optimise solar charging | Optimise solar charging |

Normal operation (`discharge_allowed`: the battery covers the house) is always allowed.

## Comparing the two

The overview on the dashboard shows the bill of the period **without the battery** and **with the
battery**, item by item, for the plan that controls the battery:

| Line | Meaning |
|---|---|
| Paid → house / battery | grid kWh bought for the house and for the battery |
| Earned → solar surplus / from battery | solar and battery kWh sold |
| Wear | wear per kWh out of the battery |
| Energy | the value of what is in the battery at the start (without) or the end (with) |
| = Bill | the sum. The difference is what the battery earns in the period |

The attribute `optimizer_shadow_summary` on `sensor.<prefix>_powerwall_action` holds the totals of the
optimised plan (`optimal`), the rule plan (`rules`) and plain self consumption (`self_consumption`), and
`controls` tells whether the optimisation controls the battery.

## When to turn it off

Keep it on unless you want the battery to follow the rules exactly, e.g. to understand a rule or because
the price forecast for the coming days is unreliable. With the switch off, the rules control the battery
one after another in a fixed order – see
[When the rules control the battery](../how-it-works.md#when-the-rules-control-the-battery). The
optimisation is still calculated for the comparison.

## Use midnight battery level

With [Use midnight battery level](selling.md#use-midnight-battery-level) on, each day is optimised on its
own until midnight, and the next day starts at the level the day before ends with.
