# Switching from the PyScript version

The integration replaces the [PyScript version](https://github.com/dezito/BattMind). It reads the PyScript
files but never changes them, and stores its own data in `.storage`.

## Steps

1. **Add the integration.** If `battmind_config.yaml` exists in the config folder, the setup asks whether
   it should be imported:
   - the values from `battmind_config.yaml` are filled in on the setup pages
   - `battmind_*_db.yaml` (charging history, power values, kWh prices, solar production) are imported on
     first start
   - if the old `input_boolean` / `input_number` / … `battmind_*` still exist, their values are copied to the
     new entities
2. **Run both side by side** (optional): choose a different prefix, e.g. `battmind_ha`. The setup rejects a
   prefix if the PyScript version already has entities with that name. Keep the new automation off while
   the PyScript version controls the battery, and compare the plans for a few days.
3. **Switch over:** remove `pyscript/battmind.py` and `packages/battmind.yaml`, restart, and turn the new
   automation on.

## What became what

| PyScript | Integration |
|---|---|
| `battmind_config.yaml` | Setup / **Configure** in the UI |
| `input_boolean.battmind_*` | `switch.battmind_*` |
| `input_number.battmind_*` | `number.battmind_*` (the fixed solar sell price is now in the setup) |
| `input_select.battmind_forecast_type` | `select.battmind_forecast_type` |
| `input_text.battmind_exclude_sell_hours` | `text.battmind_exclude_sell_hours` |
| `input_button.battmind_*` | `button.battmind_*` |
| `pyscript.battmind_debug_info` | `battmind.debug_info` |
| Your automation on the PyScript sensor | the [blueprint](actions.md#the-blueprint) on `sensor.<prefix>_powerwall_action` |
| Update scripts and `shell_command` | HACS |

## What is new

- The [whole period optimisation](rules/optimizer.md) controls the battery by default.
- New rules: [negative prices](rules/charging.md#-negative-prices), [buy to sell](rules/charging.md#-buy-to-sell)
  and [before the sun](rules/discharging.md#-before-the-sun).
- The [profit requirements](rules/selling.md#profit-requirements) apply to every rule, not only to *Profit only*.
- Solar surplus always charges first, and the loss is split evenly between charging and discharging.
- An [analysis](dashboard-analysis.md#analysis) of the economy and the planning.
- Backup and restore of data and settings.
- The calculations run in their own thread, so Home Assistant isn't blocked.

## Fixes compared to the PyScript version

- Consumption history is also stored without solar panels and solar forecast, so the consumption forecast
  isn't empty.
- "Cheapest hours" no longer plans in hours that have already passed.
- "Sell excess kWh" no longer counts the same selling period multiple times.
- An error in one rule no longer stops the whole planning.
- History is read from an in-memory buffer instead of 70+ recorder queries per planning run.
