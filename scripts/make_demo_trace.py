"""Generate samples/demo-trace.jsonl: one synthetic day of a small mixed fleet.

Everything is invented (unit ids, names, coordinates, readings). The
scenarios are the ones that keep fleet dispatchers busy in practice:

* 201 truck speeding on the ring road for a few minutes
* 202 excavator parked at a quarry losing 45 L of fuel at night
* 203 fuel truck leaving the depot at night
* 204 van whose tracker loses external power (battery wire cut / tracker unplugged)
* 205 truck driving without a GPS fix for 25 minutes (antenna damage or a jammer)
* 206 pickup that stops reporting in the afternoon

Run: python scripts/make_demo_trace.py > samples/demo-trace.jsonl
"""

from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=5))  # Asia/Tashkent
DAY = datetime(2026, 11, 18, tzinfo=TZ)
DEPOT = (41.23000, 69.15000)
QUARRY = (41.30500, 69.02500)
RING = [(41.23400, 69.16200), (41.27000, 69.20500), (41.33000, 69.21500), (41.36500, 69.17000)]
rng = random.Random(42)
rows: list[dict] = []


def ts(h: int, m: int = 0) -> int:
    return int((DAY + timedelta(hours=h, minutes=m)).timestamp())


def emit(uid, name, t, pos, speed, sats=12, **params):
    d = {"t": t, "tp": "ud", "pos": None, "p": {"pwr_ext": round(rng.uniform(27.6, 28.4), 2), **params}}
    if pos is not None:
        d["pos"] = {
            "y": round(pos[0], 6),
            "x": round(pos[1], 6),
            "z": 420,
            "s": round(speed),
            "c": 0,
            "sc": sats,
        }
    rows.append({"i": uid, "nm": name, "d": d})


def dist_km(a, b):
    return math.hypot((a[0] - b[0]) * 111.2, (a[1] - b[1]) * 83.7)


def drive(uid, name, t, path, speed, step=60, **kw):
    """Drive along waypoints at ~speed km/h, one message per ``step`` seconds. Returns end time."""
    for a, b in zip(path, path[1:], strict=False):
        n = max(1, int(dist_km(a, b) / (speed * step / 3600)))
        for i in range(n):
            f = i / n
            v = speed + rng.uniform(-6, 6)
            emit(uid, name, t, (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f), max(0, v), **kw)
            t += step
    return t


def park(uid, name, t, until, pos, step=300, **kw):
    while t < until:
        emit(uid, name, t, (pos[0] + rng.uniform(-2e-5, 2e-5), pos[1] + rng.uniform(-2e-5, 2e-5)), 0, **kw)
        t += step
    return t


# 201: normal shift, speeding on the ring road for ~6 minutes after 08:10
n = "Truck 01 · Isuzu NQR"
t = park(201, n, ts(0, 2), ts(8, 0), DEPOT, io_239=0)
t = drive(201, n, t, [DEPOT, RING[0], RING[1]], 55, io_239=1)
for _ in range(6):
    emit(201, n, t, (RING[1][0] + rng.uniform(0, 0.01), RING[1][1]), rng.uniform(98, 112), io_239=1)
    t += 60
t = drive(201, n, t, [RING[1], RING[2], RING[3]], 60, io_239=1)
t = park(201, n, t, ts(13, 0), RING[3], io_239=0)
t = drive(201, n, t, [RING[3], RING[2], RING[1], RING[0], DEPOT], 58, io_239=1)
park(201, n, t, ts(23, 58), DEPOT, io_239=0)

# 202: excavator at the quarry; burns ~14 L/h while working, 45 L disappear at 01:20-01:32
n = "Excavator 03 · CAT 320"
level = 360.0
t = ts(0, 1)


def lls(litres):  # invert the demo calibration table in rules.yaml (raw 30..4060 -> 0..400 L)
    return round(30 + (litres / 400) ** (1 / 1.12) * 4030 + rng.uniform(-6, 6))


while t < ts(23, 59):
    local = datetime.fromtimestamp(t, TZ)
    working = 8 <= local.hour < 18 and local.hour != 13
    if working:
        level -= 14 * 120 / 3600
    if datetime(2026, 11, 18, 1, 20, tzinfo=TZ) <= local < datetime(2026, 11, 18, 1, 32, tzinfo=TZ):
        level -= 45 / 6
    if local.hour == 17 and local.minute == 30:
        level = 390.0  # refuelled from the fuel truck
    slosh = rng.uniform(-8, 8) if working else 0
    emit(202, n, t, QUARRY, 0, io_239=int(working), rs485_lls1=lls(max(0, level + slosh)))
    t += 120

# 203: fuel truck, day shift, then leaves the depot at 23:10 and comes back
n = "Fuel truck 05 · MAN TGS"
t = park(203, n, ts(0, 3), ts(9, 0), DEPOT, io_239=0)
t = drive(203, n, t, [DEPOT, QUARRY], 45, io_239=1)
t = park(203, n, t, ts(17, 40), QUARRY, io_239=0)
t = drive(203, n, t, [QUARRY, DEPOT], 45, io_239=1)
t = park(203, n, t, ts(23, 10), DEPOT, io_239=0)
t = drive(203, n, t, [DEPOT, (41.2420, 69.1700)], 40, io_239=1)
park(203, n, t, ts(23, 59), (41.2420, 69.1700), io_239=0)

# 204: van; external power drops to ~0 V at 14:05 for 20 minutes
n = "Van 07 · GAZelle Next"
t = park(204, n, ts(0, 4), ts(9, 30), DEPOT, io_239=0)
t = drive(204, n, t, [DEPOT, RING[0], RING[1], RING[2]], 40, io_239=1)
t = park(204, n, t, ts(14, 5), RING[2], io_239=0)
while t < ts(14, 25):
    emit(204, n, t, RING[2], 0, io_239=0)
    rows[-1]["d"]["p"]["pwr_ext"] = round(rng.uniform(0.0, 0.4), 2)  # running on the internal battery
    t += 60
t = park(204, n, t, ts(16, 0), RING[2], io_239=0)
t = drive(204, n, t, [RING[2], RING[1], RING[0], DEPOT], 40, io_239=1)
park(204, n, t, ts(23, 59), DEPOT, io_239=0)

# 205: truck loses the GPS fix 11:00-11:25 while the engine runs
n = "Truck 09 · Shacman X3000"
t = park(205, n, ts(0, 5), ts(9, 0), DEPOT, io_239=0)
t = drive(205, n, t, [DEPOT, RING[0], RING[1]], 50, io_239=1)
t = park(205, n, t, ts(11, 0), RING[1], io_239=0)
while t < ts(11, 25):
    emit(205, n, t, None, 0, sats=0, io_239=1)
    t += 60
t = drive(205, n, t, [RING[2], RING[3]], 50, io_239=1)
t = park(205, n, t, ts(18, 0), RING[3], io_239=0)
t = drive(205, n, t, [RING[3], RING[2], RING[1], RING[0], DEPOT], 50, io_239=1)
park(205, n, t, ts(23, 59), DEPOT, io_239=0)

# 206: pickup; last message at 15:52, then silence
n = "Pickup 11 · Toyota Hilux"
t = park(206, n, ts(0, 6), ts(10, 0), DEPOT, io_239=0)
t = drive(206, n, t, [DEPOT, QUARRY], 60, io_239=1)
t = park(206, n, t, ts(15, 40), QUARRY, io_239=0)
drive(206, n, t, [QUARRY, (41.2900, 69.0700)], 50, io_239=1)

rows.sort(key=lambda r: (r["d"]["t"], r["i"]))
print("# synthetic demo trace, generated by scripts/make_demo_trace.py; all data is invented")
for r in rows:
    print(json.dumps(r, separators=(",", ":"), ensure_ascii=False))
