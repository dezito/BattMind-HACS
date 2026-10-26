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
entry and fill only the values you want to replace. Run the action and Home Assistant shows the response.

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
| **assumptions** | the complete temporary battery values used for the comparison |
| **current** | historical economy with the configured battery |
| **scenario** | economy with the temporary values |
| **difference.profit** | scenario profit minus current profit; positive is better |
| **difference.bill** | scenario bill minus current bill; negative is better |

Both **current** and **scenario** contain the bill with and without the battery, expected profit, charged
grid/solar energy, energy delivered to the house, sales, wear and the final battery level.

This is a hindsight calculation, not a forecast or guarantee. It needs hourly long-term statistics for
the configured house-consumption and buy-price entities; solar and sell-price statistics improve its
accuracy. Home Assistant may provide fewer days than requested if older statistics are unavailable.
