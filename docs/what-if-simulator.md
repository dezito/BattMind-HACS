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
returning the full simulation response for automations and scripts.

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

All fields are optional. Power is in W, battery capacity in kWh, and battery levels and round-trip loss
are percentages. Setting **round_trip_loss** temporarily replaces the configured efficiency curves.

## Reading the response

| Part | Meaning |
|---|---|
| **period** | first/last hour and the amount of historical data actually used |
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
historical benchmark rather than expected savings or battery payback. It requires continuous hourly
long-term statistics for the configured consumption, battery-power, solar, ignored-load and price
entities used in the comparison. Statistics are read from Home Assistant's Recorder for those
configured entities; the simulator does not fall back to BattMind's offline price database. The
simulator uses only the latest continuous period shared by the required entities, starting after the
most recent gap. It never skips missing hours within the simulated period or assumes that the buy price
was earned when selling. The displayed day count is based on the number of hourly samples, not calendar
dates. Without a configured historical sell-price entity, variable export revenue is treated as zero
(a fixed configured solar sell price is still used). Home Assistant may provide fewer days than
requested if older statistics are unavailable.
