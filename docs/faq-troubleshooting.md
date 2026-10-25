# FAQ and troubleshooting

Start with the plan on the dashboard: every hour has a reason, and clicking it shows the numbers behind
the decision.

## Why doesn't it charge now, although the price is low?

One of these is usually the reason:

- **It doesn't pay.** A grid kWh has to be used later at a price that covers the charge price, loss, wear
  and **min. profit per kWh**. At 0.80 kr with 10 % loss and 0.10 kr wear, the energy has to be used in
  hours above 0.99 kr. See [the cost of a kWh](how-it-works.md#the-cost-of-a-kwh-from-the-battery).
- **There is no use for it.** Only what the house will use (or what can be sold with profit) is charged.
  If the battery already covers the coming expensive hours, nothing more is charged.
- **The sun will fill it.** Grid charging leaves room for the forecast solar surplus.
- **It isn't one of the cheapest periods**, or it costs more than *increase for cheapest price* above the
  day's cheapest hour.

## It planned to charge tonight, but the battery wasn't charged in the morning

Usually the plan changed during the night. BattMind recalculates the plan every few minutes, and it moves
or drops the charging when something it depends on changes:

- **The prices changed.** Forecast prices (e.g. Carnot) and hours filled from the price database are
  replaced by the real prices when they are published, and a cheaper hour – or no profitable one – may turn up.
- **The consumption or the battery level differs** from the forecast, so less (or nothing) was needed.
- **The solar forecast changed.** More expected sun means less grid charging, because solar charges first.

To see what actually happened:

- open the history of `sensor.<prefix>_powerwall_action` (*History* or the entity's dialog): it shows when
  the action was `grid_charging` during the night
- `sensor.<prefix>_current_charging_rule` and its history tell why
- the overview shows the plan as it is now, with a reason for every hour
- [Planned against actual](dashboard-analysis.md#planned-against-actual) in the analysis compares the plan
  from the evening before with what was charged

If the action was `grid_charging` but the battery didn't charge, the automation or the inverter is the
problem – check the blueprint's trace (*Automations → ⋮ → Traces*).

## Why is the action `force_solar_only_charging` in the night?

The energy in the battery is saved for more expensive hours (e.g. the morning or the next evening), or
using it now wouldn't pay for what it cost. The reason is in the plan (⏳ or 🚫).

## Why does it sell less than I expected?

- The sell price is usually much lower than the buy price (tariffs and taxes). Selling only pays when it
  beats using the energy in the house.
- **Min. kWh for export**, **min. total profit** or **excluded sell hours** stop small sales.
- The energy is needed before the battery is charged again.

## The battery level forecast is wrong

- Check the sign of the **battery power** sensor: positive while discharging. Otherwise enable **Invert**.
- Check whether the **house consumption** sensor includes the battery's charging/discharging, and tell the
  setup.
- In the first days the consumption and solar history is thin. See
  [Forecast accuracy](dashboard-analysis.md#forecast-accuracy) after a week.

## The measured loss is much higher or lower than expected

It is measured with the battery power sensor. A wrong sign, a sensor in kW labelled W, or a sensor that
also includes the inverter's own consumption gives a wrong loss. Check the sensor before you change the
configured loss.

## What happens when prices are missing?

Single hours without a price (e.g. the days after tomorrow without a price forecast) are filled from
BattMind's own price database – the stored prices of the same hour and weekday. They are listed under
*Using offline prices for some hours* in the overview.

If the price integration is down completely, BattMind keeps the last prices for 2 hours. After that it plans with prices from its own price database:
the stored prices of the same hour and weekday. The battery keeps working, and the plan is based on real
prices again as soon as the integration is back. The dashboard shows a warning with a restart button for
the price integration. See [Prices](how-it-works.md#prices).

The database fills up over time, so right after the first installation it has little to go on – unless it
was imported from the PyScript version or a backup.

## The plan isn't recalculated

It is only recalculated when prices, the battery level or a setting have changed. Press **Force planning**
(or call `battmind.enforce_planning`) to recalculate it now.

## The battery doesn't do what the action says

E.g. the action is `grid_charging`, but the battery doesn't charge – after a restart, a lost command or
because the inverter changed its mode by itself. The blueprint's **Repeat interval** (default 5 minutes)
sends the current action again, also when Home Assistant starts, so it is put right within 5 minutes. If you
set it to 0, the action is only sent when BattMind changes it, and nothing catches a lost command. See
[Repeat interval](actions.md#repeat-interval).

If it still doesn't follow, check the blueprint's trace (*Automations → ⋮ → Traces*): did the actions run,
and did the inverter accept them?

## Today's charging history looks wrong

Call `battmind.recalc_charging_history_today` (or the button on the dashboard) to recalculate it from the
power values.

## Logs and debug info

- Turn on **Diagnostic file log** under **Configure → Advanced**. The log is written to
  `battmind_<prefix>.log` in the config folder (2 MB, 3 rotated files).
- Or enable debug logging in `configuration.yaml`:
  ```yaml
  logger:
    logs:
      custom_components.battmind: debug
  ```
- Call `battmind.debug_info` under **Developer tools → Actions**, and attach the response when you open an
  [issue](https://github.com/dezito/BattMind-HACS/issues).
