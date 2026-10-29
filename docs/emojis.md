# Emojis

BattMind marks every decision with an emoji, so a plan or a history row can be read at a glance. The same
emoji always means the same rule.

## Where they are used

| Where | What the emojis tell |
|---|---|
| `sensor.<prefix>_current_charging_rule` (dashboard → current rule) | the rule behind the action right now, e.g. *💸 Charging up to …*. A second line shows the battery's measured power, with the rules of the current hour and ⌛ (an average) |
| The plan in the overview | every hour starts with the emoji of its rule – click the row to see the numbers behind it. 📦 joins neighbouring periods with the same rule |
| The bill of the period (overview) | 📈 charged from the grid, 📤 sold, ☀️ solar, 💰 the saving, 🚗 the Cable Juice Planner cars' planned charging |
| The solar forecast (overview) | ☀️ the surplus per day, 🌅/🌇 sunrise and sunset, 🌤️ when the sun would fill the battery |
| The charging history | the rules that were active in each hour, and the column headers (📈 charged, 📉 discharged, ⚖️ balance, 💰 savings, ☀️ solar share) |
| The analysis → *Per rule* | charged and discharged kWh per rule |
| The emoji explanation (dashboard sidebar) | `sensor.<prefix>_emoji_description` – the list below in your language |

In testing mode (setup → *Advanced*), the current rule is wrapped in 🧪, so a test instance can be told apart.

## Charging

| Emoji | Meaning | Rule |
|---|---|---|
| 💸 | Charging during the cheapest periods | [Cheapest periods](rules/charging.md#-cheapest-periods) |
| 🔥 | Charging for the most expensive periods | [Most expensive periods](rules/charging.md#-most-expensive-periods) |
| 🩹 | Charging what is needed before the maximum battery level is reached | [Required before max level](rules/charging.md#-required-before-max-level) |
| 🎁 | Charging at negative prices | [Negative prices](rules/charging.md#-negative-prices) |
| 🔁 | Bought to sell later | [Buy to sell](rules/charging.md#-buy-to-sell) |
| 📈 | Charging (in general, and in the history's headers) | |
| ☀️ | Solar charging / surplus production | [Solar](rules/solar.md) |
| 🌤️ | Maximum charge reached – the rest is solar surplus | [Solar](rules/solar.md) |
| 🎛️ | Solar charging blocked – the surplus is sold | [Optimise solar charging](rules/solar.md#-optimise-solar-charging) |

## Discharging and selling

| Emoji | Meaning | Rule |
|---|---|---|
| 📉 | Discharging (in general) | |
| 🌄 | Discharging before the sun fills the battery | [Before the sun](rules/discharging.md#-before-the-sun) |
| 🚫 | No profit from discharging – the battery's kWh cost is above the price | [Profit only](rules/discharging.md#-profit-only) |
| ⏳ | Saved for the most expensive hours | [Prioritise by energy cost](rules/discharging.md#-prioritise-by-energy-cost) |
| 🤑 | Sold instead of used (pays more) | [Maximise profit](rules/selling.md#-maximise-profit) |
| 📤 | Selling excess kWh | [Sell excess kWh](rules/selling.md#-sell-excess-kwh) |
| 🕰️ | Old energy used or sold | [Max. age of kWh](rules/discharging.md#-max-age-of-kwh) |
| 🛒 | Forced discharge – selling to the grid in the most expensive hours | |

## Status

| Emoji | Meaning |
|---|---|
| ☠️ | Critical error – see [FAQ and troubleshooting](faq-troubleshooting.md#logs-and-debug-info) |
| ⚠️ | Charging without a rule |

## Info

| Emoji | Meaning |
|---|---|
| ⚖️ | Balance: charged minus discharged kWh (charging history) |
| 🌅 / 🌇 | Sunrise / sunset |
| 💰 | Savings and revenue from discharging |
| ⌛ | Average (e.g. the battery's power over the last interval) |
| 📦 | A group of periods |
