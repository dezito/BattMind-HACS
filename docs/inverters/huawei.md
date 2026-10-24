# Huawei Solar (LUNA2000)

Actions for the [blueprint](../actions.md#the-blueprint) with the
[Huawei Solar](https://github.com/wlcrs/huawei_solar) integration and a LUNA2000 battery at 5000 W.
Change `5000` to your battery's max. power.

Choose *Edit in YAML* in each action field and paste. Replace `YOUR_BATTERY_DEVICE_ID` with the battery
device's id (shown in the address bar on the device's page).

## Charge from grid

```yaml
- action: number.set_value
  target:
    entity_id:
      - number.batteries_maximum_charging_power
      - number.batteries_grid_charge_maximum_power
  data:
    value: 5000
- action: switch.turn_on
  target:
    entity_id: switch.batteries_charge_from_grid
- action: huawei_solar.forcible_charge
  data:
    device_id: YOUR_BATTERY_DEVICE_ID
    duration: 120
    power: 5000
```

## Forced discharge

```yaml
- action: number.set_value
  target:
    entity_id:
      - number.batteries_maximum_charging_power
      - number.batteries_grid_charge_maximum_power
      - number.batteries_maximum_discharging_power
  data:
    value: 5000
- action: huawei_solar.forcible_discharge
  data:
    device_id: YOUR_BATTERY_DEVICE_ID
    duration: 120
    power: 5000
```

## Discharge allowed (normal operation)

```yaml
- action: huawei_solar.stop_forcible_charge
  data:
    device_id: YOUR_BATTERY_DEVICE_ID
- action: number.set_value
  target:
    entity_id:
      - number.batteries_maximum_charging_power
      - number.batteries_grid_charge_maximum_power
      - number.batteries_maximum_discharging_power
  data:
    value: 5000
```

## Solar charging only

```yaml
- action: huawei_solar.stop_forcible_charge
  data:
    device_id: YOUR_BATTERY_DEVICE_ID
- action: number.set_value
  target:
    entity_id:
      - number.batteries_grid_charge_maximum_power
      - number.batteries_maximum_discharging_power
  data:
    value: 0
- action: number.set_value
  target:
    entity_id: number.batteries_maximum_charging_power
  data:
    value: 5000
```

## Solar charging blocked

```yaml
- action: huawei_solar.stop_forcible_charge
  data:
    device_id: YOUR_BATTERY_DEVICE_ID
- action: number.set_value
  target:
    entity_id:
      - number.batteries_maximum_charging_power
      - number.batteries_grid_charge_maximum_power
  data:
    value: 0
- action: number.set_value
  target:
    entity_id: number.batteries_maximum_discharging_power
  data:
    value: 5000
```

## While the car is charging

```yaml
- action: huawei_solar.stop_forcible_charge
  data:
    device_id: YOUR_BATTERY_DEVICE_ID
- action: number.set_value
  target:
    entity_id:
      - number.batteries_maximum_charging_power
      - number.batteries_grid_charge_maximum_power
  data:
    value: 5000
- action: number.set_value
  target:
    entity_id: number.batteries_maximum_discharging_power
  data:
    value: 0
```

## Repeat interval

The forced charge/discharge stops after `duration` minutes. Set **Repeat interval** to e.g. 30 minutes
(`00:30:00`), so it is renewed while the action lasts.
