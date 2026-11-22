# Rules reference

A rules file has five sections: `timezone`, `cooldown_minutes`, `zones`, `units`, `rules` and
`notifiers`. `samples/rules.yaml` is a complete example; `fleet-alerts check-config --rules FILE`
validates a file and prints a summary.

## Common options

| Option | Default | |
|---|---|---|
| `id` | required | unique name, used in messages, API filters and metrics |
| `type` | required | one of the types below |
| `severity` | `warning` | `info`, `warning` or `critical`; notifiers subscribe by minimum severity |
| `units` | all | list of Wialon unit ids |
| `groups` | all | list of group names from the `units` section |
| `active_hours` | always | local time window, may cross midnight: `"22:00-06:00"` |

## offline

```yaml
- { id: offline, type: offline, after_minutes: 60 }
```

Checked every `TICK_INTERVAL_S` (60 s) against Wialon's server time. Choose `after_minutes` above the
longest normal gap: trackers in sleep mode on a parked vehicle often report every 30–60 minutes.

## overspeed

```yaml
- { id: overspeed, type: overspeed, limit_kmh: 90, min_duration_s: 120, hysteresis_kmh: 5 }
```

Messages without a GPS fix (fewer than 4 satellites) are skipped: their speed is not reliable. The
alert carries the highest speed seen while it was open.

## power_loss

```yaml
- { id: power-cut, type: power_loss, param: pwr_ext, below_volts: 9.0, scale: 1, min_duration_s: 60 }
```

`param` is the tracker parameter with the external voltage (`pwr_ext` on Teltonika). Use
`scale: 0.001` for trackers that report millivolts. For 24 V trucks a threshold of 18–20 V also
catches a failing battery; for 12 V vehicles 9 V catches only a disconnected one.

## gps_lost

```yaml
- { id: gps-lost, type: gps_lost, min_satellites: 4, min_duration_s: 600 }
```

Fires when messages keep arriving without a usable fix. Ten minutes avoids alerts in tunnels and
underground parkings.

## fuel_drain

```yaml
- id: fuel-drain
  type: fuel_drain
  min_litres: 25
  window_minutes: 30
  max_speed_kmh: 5
  median_of: 5
```

Needs a `fuel` block for the unit:

```yaml
units:
  "202":
    fuel:
      param: rs485_lls1
      table: [[30, 0], [433, 30.3], ..., [4060, 400.0]]   # raw -> litres
```

How it works:

1. Every raw reading is converted with the calibration table (linear between points, clamped
   outside) and smoothed with a median of the last `median_of` readings.
2. While the speed is at most `max_speed_kmh`, levels are kept for `window_minutes`. Driving clears the
   window: fuel used on the road is consumption, not a drain.
3. When the level is `min_litres` below the highest level in the window, the rule waits until the level
   has stopped falling for 3 readings and then reports the whole drop, with the level before and after.

Tuning: `min_litres` should be larger than the sensor noise plus what the vehicle burns at idle within
the window (an excavator working in place burns 10–20 L/h). The table is validated: it must have at
least two points, no duplicate raw values and must not decrease, which catches the most common typo
in hand-written calibration tables.

## geofence

```yaml
zones:
  depot: { type: circle, center: [41.23, 69.15], radius_m: 400 }
  quarry: { type: polygon, points: [[41.308, 69.020], [41.308, 69.031], [41.302, 69.031], [41.302, 69.020]] }
rules:
  - { id: depot-night-exit, type: geofence, zone: depot, trigger: exit, active_hours: "22:00-06:00" }
```

`trigger` is `enter` or `exit`. The first position after start only sets the state; messages without a
fix are ignored so GPS drift near the border does not create events.

## Notifiers

```yaml
notifiers:
  - { name: log, type: log, min_severity: info }
  - name: dispatch-chat
    type: telegram
    token: ${TELEGRAM_BOT_TOKEN}
    chat_id: ${TELEGRAM_CHAT_ID}
    min_severity: warning
  - name: mining-team
    type: telegram
    token: ${TELEGRAM_BOT_TOKEN}
    chat_id: ${MINING_CHAT_ID}
    groups: [construction]
    notify_resolve: false
  - name: crm
    type: webhook
    url: https://crm.example.com/hooks/fleet-alerts
    secret: ${WEBHOOK_SECRET}
    min_severity: critical
```

Webhook body:

```json
{"id": 1, "event": "open", "unit": {"id": 202, "name": "Excavator 03"}, "rule": "fuel-drain",
 "severity": "critical", "status": "open", "title": "Fuel drop 46 L while parked",
 "opened_at": 1794948060, "resolved_at": null,
 "details": {"litres": 45.8, "level_before": 360.4, "level_after": 314.6, "lat": 41.305, "lon": 69.025}}
```

Verify `X-Fleet-Alerts-Signature` on the receiving side as shown in
`fleet_alerts/notify/webhook.py::verify_signature`.
