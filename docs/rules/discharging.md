# Discharging

By default the battery may discharge to the house in every hour (`discharge_allowed`). These rules decide
in which hours it **may not**, so the energy is saved for when it is worth more – and when it should be
used even though it doesn't pay.

An hour where discharging is blocked becomes `force_solar_only_charging`: the battery can still charge from
solar, but the house uses the grid.

---

## 🚫 Profit only

**Switch:** *Discharging: Profit only*

Blocks discharging in hours where the buy price is lower than what a kWh from the battery costs, incl. loss
and wear, plus **min. profit per kWh**.

The battery's cost is calculated for that hour: the energy already in the battery plus what is planned to
be charged before it (grid at the buy price, solar at the sell price it would otherwise have got).

**Example:** the battery was charged at night at 0.80 kr/kWh, so a kWh out costs 0.99 kr. At 14:00 the
price is 0.90 kr/kWh: using the battery would lose 0.09 kr per kWh. Discharging is blocked, and the energy
is saved for the evening.

Only the profit per kWh counts here – **min. total profit** doesn't block small hours, because not using
the battery for the house earns nothing.

---

## ⏳ Prioritise by energy cost

**Switch:** *Discharging: Prioritise by energy cost*

Saves the battery for the most expensive hours until it is charged again.

From the hour the battery is fullest until it is charged again (by solar or the grid), BattMind goes through
the hours from the most expensive (price × consumption) down and gives each its share of the battery. When
the battery is used up, the remaining cheaper hours before the last prioritised hour are blocked.

**Example:** the battery holds 4 kWh after the night. 07–08 costs 1.80 kr, 17–20 costs 2.50 kr, and the
house uses 1 kWh per hour. The four evening hours get the 4 kWh, and the morning hours use the grid.

---

## 🌄 Before the sun

**Switch:** *Discharging: Before the sun*

On days where the sun fills the battery to max and the surplus is sold, every kWh emptied before the sun
is refilled with solar that would otherwise be sold. So a kWh used or sold before the sun only has to beat
the solar sell price:

`refill cost = solar sell price ÷ efficiency² + wear`

The rule allows discharging in blocked hours before the sun, and sells in the most expensive periods before
it, as long as `price − refill cost ≥ min. profit per kWh`. It never empties more than the solar surplus
that would be pushed out.

**Example:** the sun fills the battery at 11:00 and 6 kWh surplus are sold at 0.40 kr/kWh. Refill costs
0.40 ÷ 0.9 + 0.10 = 0.54 kr/kWh. At 07:00 the price is 1.60 kr/kWh: using the battery earns 1.06 kr per
kWh, even though the night charge cost more.

---

## 🕰️ Max. age of kWh

**Switch:** *Forced discharge: Max. kWh age in hours* · **Number:** *Max. age of kWh in home battery*
(default 48 h)

Energy that has been in the battery longer than this is used even without profit: discharging is allowed
in every hour from then. This keeps old, expensive energy from blocking the battery forever – e.g. when it
was charged before a drop in prices.

If selling the old energy in the most expensive sell periods pays more than using it (by the
[profit requirements](selling.md#profit-requirements)), it is sold instead.
