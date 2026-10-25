# BattMind documentation

The [README](../README.md) gives the short version. These pages go into detail: how BattMind thinks, what
every rule does, and how to connect it to your inverter.

## Start here

| Page | Read it when |
|---|---|
| [Getting started](getting-started.md) | you install BattMind for the first time |
| [How BattMind plans](how-it-works.md) | you want to understand why the battery does what it does |
| [Actions and battery control](actions.md) | you build the automation that controls the inverter |

## Rules

| Page | Content |
|---|---|
| [Whole period optimisation](rules/optimizer.md) | the default way of controlling the battery, and how it relates to the rules |
| [Charging from the grid](rules/charging.md) | cheapest periods, fill up, most expensive periods, required before max level, negative prices, buy to sell |
| [Discharging](rules/discharging.md) | profit only, prioritise by energy cost, before the sun, max. age of kWh |
| [Selling](rules/selling.md) | maximise profit, sell excess kWh, use midnight battery level, profit requirements, excluded sell hours |
| [Solar](rules/solar.md) | solar surplus charging, optimised solar charging, the solar forecast |

## Inverters

| Page | Content |
|---|---|
| [Huawei Solar](inverters/huawei.md) | ready-made actions for the blueprint (LUNA2000) |

Have you set up another inverter? Please share your actions in an
[issue](https://github.com/dezito/BattMind-HACS/issues), so a page can be added for it.

## Reference

| Page | Content |
|---|---|
| [Dashboard and analysis](dashboard-analysis.md) | every card on the dashboard, and how to read the analysis |
| [Emojis](emojis.md) | what every emoji in the plan, the history and the current rule means |
| [Recipes](cookbook.md) | typical setups: no solar, solar with a fixed sell price, together with Cable Juice Planner … |
| [Switching from the PyScript version](migration.md) | import of configuration and databases |
| [FAQ and troubleshooting](faq-troubleshooting.md) | "why doesn't it charge now?", logs and debug info |
