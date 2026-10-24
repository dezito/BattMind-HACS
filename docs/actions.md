# Actions and battery control

BattMind sends **no commands** to the inverter. It sets `sensor.<prefix>_powerwall_action` to what the
battery should do right now, and your automation makes the inverter do it.

## The five actions

| Action | The battery should | Typical inverter setting |
|---|---|---|
| `grid_charging` | charge from the grid now | forced charge from the grid at full power |
| `force_discharge` | discharge to the house and sell the rest to the grid | forced discharge at full power |
| `discharge_allowed` | **run normally** – BattMind doesn't really control the battery in this state | the inverter's normal self-consumption mode: charge from solar, discharge to the house |
| `force_solar_only_charging` | charge from solar, but not discharge – the energy is saved for later | discharge power 0, no grid charging, charge power max |
| `blocked_solar_charge` | not charge from solar – the surplus is sold | charge power 0, discharge power max |

`discharge_allowed` is the normal state. Most hours of the day are in it, and the battery simply does what
the inverter does on its own. BattMind only steps in with the other four actions.

Why the battery is in an action is shown in `sensor.<prefix>_current_charging_rule` (e.g. *Charging up to
💸*) and in the plan on the dashboard.

## The blueprint

The blueprint **BattMind - control the home battery** is installed with the integration and works with all
inverters, because you choose what happens with Home Assistant's normal action editor.

### BattMind

| Input | Meaning |
|---|---|
| BattMind action | `sensor.<prefix>_powerwall_action` |
| Repeat interval | run the current action again with this interval (0 = off). See [below](#repeat-interval) |
| Custom triggers | extra triggers that run the current action again, e.g. a change on an inverter entity |

### Actions

One field per action: **Charge from grid**, **Forced discharge**, **Discharge allowed (normal operation)**,
**Solar charging only** and **Solar charging blocked**. Typically *Number: Set value* on the battery's max.
charge/discharge power, *Switch: Turn on/off*, or a service from the inverter integration. Fields left
empty do nothing.

Tips per action:

- **Charge from grid** – if the inverter's forced charge needs a duration or a target level, use a long
  duration (e.g. 120 min) and a target of 100 %. BattMind stops the charge by changing action when the
  planned kWh are in.
- **Forced discharge** – the same: BattMind changes action when the sale is over.
- **Discharge allowed** – stop any forced charge/discharge and set the charge and discharge power back to
  max. Without this, a forced mode from an earlier action keeps running.
- **Solar charging only** – stop forced charge/discharge, set the discharge power (and grid charging) to 0,
  keep the charge power at max.
- <a id="blocked_solar_charge"></a>**Solar charging blocked** – set the charge power to 0 and keep the
  discharge power at max. It is only used when [optimise solar charging](rules/solar.md#-optimise-solar-charging)
  is on, and only in hours with solar surplus.

### EV (optional)

Without this, the battery may discharge into the car while it charges – the car is just another consumer.
If you don't want that:

| Input | Meaning |
|---|---|
| EV charger power | one or more power sensors of the chargers |
| Car is charging above | above this power (default 1000 W) the car counts as charging. The change has to last 30 seconds |
| While the car is charging | runs instead of *Discharge allowed* and *Solar charging blocked* while the car charges, e.g. discharge power 0. *Charge from grid*, *Forced discharge* and *Solar charging only* still follow BattMind |
| Allow discharging into the car when | while this entity is on, *Discharge allowed* runs as normal, e.g. Cable Juice Planner's `switch.cjp_<car>_powerwall_discharge_above_needed` |
| Only above this battery level | together with the battery level sensor: only allowed while the battery is above this value, e.g. `number.cjp_<car>_ev_charge_after_powerwall_battery_level` |

Tip: also add the charger's power sensor under **Ignored consumers** in the BattMind setup, so the car
isn't in the consumption forecast.

### Repeat interval

Some inverters need the command again and again:

- **Huawei Solar:** forced charge/discharge runs for a given duration. Repeat every 30 minutes.
- **Modbus:** some batteries fall back to their own mode if no command arrives. Repeat every 30 seconds.
- **Settings changed elsewhere** (e.g. in the inverter's app): repeat every few minutes to put them back.

The repetition starts over every time BattMind changes action, and also when Home Assistant starts or the
automations are reloaded (only with a repeat interval set).

## Your own automation

If you'd rather not use the blueprint:

```yaml
alias: BattMind - control home battery
triggers:
  - trigger: state
    entity_id: sensor.battmind_powerwall_action
    not_to: [unavailable, unknown]
actions:
  - choose:
      - conditions: "{{ trigger.to_state.state == 'grid_charging' }}"
        sequence: []  # e.g. force charging from the grid
      - conditions: "{{ trigger.to_state.state == 'force_discharge' }}"
        sequence: []  # e.g. force discharge to the grid
      - conditions: "{{ trigger.to_state.state == 'force_solar_only_charging' }}"
        sequence: []  # e.g. discharge power 0
      - conditions: "{{ trigger.to_state.state == 'blocked_solar_charge' }}"
        sequence: []  # e.g. charge power 0
    default: []  # discharge_allowed: normal operation, stop forced modes, charge/discharge power max
```

## Sensors and attributes

| Entity | Content |
|---|---|
| `sensor.<prefix>_powerwall_action` | the action now |
| ↳ `battery_level_flow` | the forecast battery level per hour |
| ↳ `charge_timestamps`, `force_discharge_timestamps`, `discharge_timestamps`, `blocked_solar_charge_timestamps`, `force_solar_only_charging_timestamps`, `force_powerwall_max_hours_old_discharge_timestamps` | the planned hours per action, for your own automations or charts |
| ↳ `hour_cost_prediction_ema` / `_trend` / `_avg` | the forecast consumption per hour (and `sum_*` accumulated) |
| ↳ `optimizer_shadow_summary` | totals of the [optimised plan, the rule plan and self consumption](rules/optimizer.md#comparing-the-two) |
| `sensor.<prefix>_current_charging_rule` | why, as text. The attributes `worker_thread` and `calculation_seconds` show where and how fast the plan was calculated |
| `sensor.<prefix>_battery_level_needed` | the battery level needed until the next charge (%), with the forecast in `battery_level_flow_prediction` |
