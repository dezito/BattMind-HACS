# What-if simulator

The **battmind.what_if** action answers questions such as:

- Would a larger battery have saved more money during the last year?
- How much would more charge/discharge power have improved the economy?
- How much do loss, wear or the reserve affect the result?

It backtests the configured battery and the temporary battery values against the same hourly Home
Assistant long-term statistics for consumption, solar production and buy/sell prices. By default it
uses up to 365 days. Nothing is saved, the active plan is not changed, and no inverter command is sent.

## Run a simulation

Open **Developer tools ? Actions**, select **BattMind: What-if simulator**, choose the BattMind config
entry and fill only the values you want to replace. Run the action to receive a persistent notification
with a two-column comparison of the configured values and the what-if scenario. The action also keeps
returning the full simulation response for automations and scripts. A progress notification is shown
while history is loaded and the simulation runs; the finished result (or an error) replaces that same
notification.

Example action:

    action: battmind.what_if
    data:
      config_entry_id: YOUR_CONFIG_ENTRY_ID
      history_days: 365
      battery_size: 20
      max_charging_power: 8000
      max_discharging_power: 8000
      round_trip_loss: 5
      wear_cost_per_kwh: 0.1
      usage_reserve: 30
      most_profit: false
      min_profit_per_kwh: 0.25

All fields are optional. Power is in W, battery capacity in kWh, and battery levels and round-trip loss
are percentages. Setting **round_trip_loss** temporarily replaces the configured efficiency curves.
You can also override the rule switches used by the optimizer as constraints (for example
**most_profit**, **grid_charge_to_sell**, or **optimize_solar_charging**) and the thresholds
**min_profit_per_kwh**, **min_total_profit**, and **min_sell_kwh**. The notification shows the current
switch states beside the scenario states.

## Reading the response

| Part | Meaning |
|---|---|
| **period** | first/last hour and the amount of historical data actually used |
| **data_sources** | counts of used entity-hour samples from long-term statistics and raw Recorder fallback |
| **price_provider** | configured provider with the most complete matching hourly data |
| **tariff_estimated_hours** | hours where the closest recorded tariff value was used instead of an exact-hour value |
| **tariff_unavailable_hours** | hours where required tariff history was unavailable; export income is conservatively set to zero |
| **current_assumptions** | configured battery values used as the current comparison |
| **assumptions** | the complete temporary battery values used for the scenario |
| **current** | historical economy with the configured battery |
| **scenario** | economy with the temporary values |
| **difference.profit** | scenario profit minus current profit; positive is better |
| **difference.bill** | scenario bill minus current bill; negative is better |

Both **current** and **scenario** contain the net result with and without the battery, expected profit,
charged grid/solar energy, energy delivered to the house, sales, wear and the final battery level.
A negative net result is not necessarily a cash credit: the calculation includes an estimated value for
energy remaining in the battery, but excludes the battery purchase price and any fixed fees.

This is a hindsight calculation, not a forecast or guarantee: the simulation uses each day's actual
prices and solar production as if they were known ahead of time, so treat the result as an optimistic
historical benchmark rather than expected savings or battery payback. It uses Home Assistant Recorder's
long-term statistics first and fills missing hourly values only from fully covered, time-weighted raw
Recorder state history for the same configured entities. Unknown/unavailable state periods are never
filled or assumed. Hours without data for every required entity are skipped, so a gap does not discard
older valid history. The calculation treats each continuous section separated by a gap independently:
the first section uses the selected starting level and later sections restart at the battery's minimum
level rather than carrying an unknown state of charge across missing data. If both Strømligning and
Energi Data Service are configured, it uses the one with the most complete matching hourly data for the
whole simulation and reports that provider; it does not mix prices from different providers. The
simulator does not use BattMind's offline price database or assume that the buy price was earned when
selling.

Historical sell prices use BattMind's normal sell-price formula, including the configured sell fee and
the recorded tax/tariff components. Energi Data Service tariff schedules are read from timestamped
Recorder attributes; Strømligning tariff sensors use the nearest recorded hourly value. When a
tariff sample is missing at the exact simulation hour, the closest available sample is used and the
notification reports the number of estimated hours. If a configured tariff has no history at all
(or an EDS schedule is incomplete), sales are valued at zero for that hour rather than assuming a
favorable tariff.

The current controller mode is included under **current_assumptions.control_mode**. The current rule
switches and the scenario's overridden switches constrain which actions the historical
optimizer may choose. This is not a replay of the exact live rule planner: the optimizer still has
hindsight of actual prices and solar, and uses the configured rule switches as action constraints.
For tractable year-long comparisons, it optimizes one day at a time regardless of the live
**use_midnight_battery_level** setting.
The displayed day count is based on hourly samples, not calendar dates. Without a configured historical
sell-price entity, variable export revenue is treated as zero (a fixed configured solar sell price is
still used). Home Assistant may provide fewer days than requested if older statistics are unavailable.
