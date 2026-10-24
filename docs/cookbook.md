# Recipes

Settings for typical setups. Start from the defaults (all rules on, the whole period optimisation
controls the battery) and only change what the recipe says.

## Battery without solar panels

The battery only moves energy from cheap to expensive hours.

- Setup: leave the **Solar panels** page empty. The weather page is skipped.
- The solar rules (*Optimise solar charging*, *Before the sun*) don't do anything and can stay on.
- Set the **wear** realistically: without solar, every kWh in the battery is bought, and the price
  difference between night and evening has to pay for loss and wear.
- Look at **Spread** and **Savings** in the [economy analysis](dashboard-analysis.md#economy-per-month)
  after a month. If the spread is small, raise **min. profit per kWh** so the battery only cycles on days
  that pay.

## Solar panels with the hourly sell price

The default setup. Make sure:

- **Fixed solar sell price** is −1.
- The sell price comes from the price integration: Energi Data Service with a separate sell sensor, or
  Strømligning with the sell sensors and the tariff page.
- **Optimise solar charging** is only useful if your inverter can stop solar charging without stopping
  discharging (see [Actions](actions.md#blocked_solar_charge)). Otherwise switch it off.

## Solar panels with a fixed sell price

Your production agreement pays a fixed price and settles the difference to the spot price, so a sold kWh
is worth `fixed price − spot price` – negative when the spot price is above the fixed price.

- Setup: **Fixed solar sell price** = the fixed price per kWh. See [Fixed solar sell price](rules/solar.md#fixed-solar-sell-price).
- Keep **Optimise solar charging** on: selling pays in the cheap hours and costs money in the expensive
  ones, so it matters a lot *when* the battery takes the solar surplus.
- Selling battery energy in the expensive evening hours doesn't pay with this agreement – using it in the
  house does.

## Together with Cable Juice Planner (EV)

The home battery shouldn't be drained into the car – unless there is energy to spare.

1. BattMind setup → **Consumption → Ignored consumers**: add the charger's power sensor, so the car isn't
   in the consumption forecast.
2. Blueprint → **EV (optional)**:
   - **EV charger power:** the charger's power sensor
   - **While the car is charging:** set the battery's discharge power to 0
   - **Allow discharging into the car when:** `switch.cjp_<car>_powerwall_discharge_above_needed`
   - **Only above this battery level:** `number.cjp_<car>_ev_charge_after_powerwall_battery_level`, and the
     home battery's level sensor under **Battery level**
3. In Cable Juice Planner, set the home battery's level and power, so CJP can plan with it.

The car is then charged from the home battery only while CJP allows it and the battery is above the
level you set.

## Careful: fewer cycles, more margin

For a new battery, or when you don't trust the price forecast yet:

| Setting | Value |
|---|---|
| Min. profit per kWh | 0.20–0.30 kr |
| Min. total profit | 1–2 kr |
| Use midnight battery level | on – the energy is used the same day |
| Days to plan | 2 |

## Battery controlled via Modbus

Some batteries fall back to their own mode if no command arrives for a while.

- Blueprint → **Repeat interval**: e.g. 30 seconds.
- Fill in every action, also **Discharge allowed**, so the battery always gets a command.

## Huawei Solar

See [Huawei Solar](inverters/huawei.md) for complete actions. Set the repeat interval to 30 minutes,
because Huawei's forced charge/discharge stops by itself.
