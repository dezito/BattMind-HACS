# Selling

These rules sell energy from the battery to the grid (`force_discharge`). A sale always runs in the most
expensive sell periods (15 minutes with 15-minute prices), and the battery covers the house first in that
period.

See also [Buy to sell](charging.md#-buy-to-sell) (charge only to sell) and [Before the sun](discharging.md#-before-the-sun)
(sell before the sun fills the battery).

---

## Profit requirements

**Reserve for the house (not sold)** is a separate battery level that BattMind does not plan to export below.
Normal self-consumption may still use this reserve down to the battery's minimum level.

**Numbers:** *Min. profit per kWh*, *Min. total profit*, *Minimum excess kWh for export* · **Text:** *Exclude selling hours*

They apply to **every** charge, discharge and sale rule, and to the [whole period optimisation](optimizer.md).

| Setting | Default | Meaning |
|---|---|---|
| Min. profit per kWh | 0.00 kr | a charge, discharge or sale has to earn at least this per kWh after loss and wear. A sale always has to earn at least 0.01 more than using the energy |
| Min. total profit | 0.00 kr | a charge hour or a sale has to earn at least this in total per hour (split over the hour's price periods) |
| Min. excess kWh for export | 0.2 kWh | the smallest amount sold per hour (split over the hour's price periods) |
| Exclude selling hours | empty | hours where nothing is sold, e.g. `17,18,19` |
| Reserve for the house (not sold) | 0% | BattMind stops forced export at this level; normal self-consumption may still use it |

> [!TIP]
> Raise **min. profit per kWh** (e.g. 0.10–0.30 kr) if the battery cycles a lot for small gains. Each cycle
> wears the battery, and the forecasts are never exact – a margin protects against plans that only pay on
> paper.

---

## 🤑 Maximise profit

**Switch:** *Discharging: Maximise profit*

Sells battery energy in the most expensive sell periods when that pays more than using it in the house.

The sold kWh are missing later, so the house has to buy them from the grid in the hours the battery would
otherwise have covered. BattMind takes the **cheapest** of those hours – but only hours in the same
discharge period, i.e. before the battery is full or charged from the grid again (otherwise the sold
energy would be replaced by solar or by a charge anyway). A sale has to:

- beat that buy price by **min. profit per kWh** (at least 0.01)
- cover what the energy in the battery cost (incl. loss and wear)
- sell at least **min. kWh for export** and earn **min. total profit**
- never leave the battery short in a later hour

**Example:** at 18:00 the sell price is 2.10 kr/kWh. The battery would otherwise cover the house at 22:00,
where the buy price is 1.30 kr/kWh. Selling 1 kWh at 18:00 and buying it at 22:00 earns 0.80 kr.

---

## 📤 Sell excess kWh

**Switch:** *Discharging: Sell excess kWh*

Sells the energy that won't be used before the battery is empty or full again.

BattMind finds the lowest level the battery reaches until it is charged again (by solar or the grid) – also
on the following days, with a big battery or a low consumption. Everything above the min. level at that
point is **excess**. It is sold in the most expensive sell periods from now until that point (at least
until the morning), when each sale covers the battery's cost plus **min. profit per kWh**.

**Example:** a 10 kWh battery with 10 % min. level never gets below 40 % before the sun fills it again
tomorrow. That leaves 30 % = 3 kWh (2.8 kWh after loss) that can be sold tonight without the house missing
anything.

---

## Use midnight battery level

**Switch:** *Use midnight battery level* – off by default

By default, the rules and the optimisation look at the whole plan: energy saved tonight may be used
tomorrow. With this switch on, they only look until midnight. The battery is then planned to be used or
sold the same day, and each day is optimised on its own.

Turn it on if the price forecast for the next days is unreliable and you would rather use the energy today.
