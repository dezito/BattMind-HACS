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

## The automation doesn't run after a restart

The blueprint only runs on startup if a **Repeat interval** is set. Otherwise it waits for the next change
of action. Set a repeat interval if your inverter forgets its settings on restart.

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
