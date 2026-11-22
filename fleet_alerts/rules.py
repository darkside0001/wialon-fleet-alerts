"""Rule engine.

The engine is pure: it receives normalised messages and clock ticks and
returns :class:`AlertEvent` objects. It never talks to the database or the
network, so every rule is tested with plain data (see ``tests/test_rules.py``)
and the same code runs in the worker and in ``fleet-alerts replay``.

Two kinds of rules:

* **condition** rules (offline, overspeed, power loss, GPS loss) open an alert
  when a condition has held for ``min_duration_s`` and resolve it when the
  condition clears. They are debounced, so one 2-second spike is not an alert.
* **one-shot** rules (fuel drain, geofence) describe an event that already
  happened. They open an alert that an operator acknowledges; the store
  applies a cooldown so the same unit does not spam the chat.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from .fuel import Calibration, smoothed
from .geo import Zone
from .models import SEVERITIES, AlertEvent, Message


@dataclass
class UnitInfo:
    id: int
    name: str = ""
    groups: tuple[str, ...] = ()
    fuel_param: str | None = None
    fuel: Calibration | None = None


def _hm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


@dataclass
class ActiveHours:
    """``"22:00-06:00"`` style window, may cross midnight."""

    start: time
    end: time

    @classmethod
    def parse(cls, value: str) -> ActiveHours:
        a, b = value.split("-")
        return cls(_hm(a.strip()), _hm(b.strip()))

    def contains(self, t: time) -> bool:
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end


class Rule:
    kind = "base"
    oneshot = False

    def __init__(self, raw: dict[str, Any]):
        self.id: str = str(raw["id"])
        self.severity: str = raw.get("severity", "warning")
        if self.severity not in SEVERITIES:
            raise ValueError(f"rule {self.id}: severity must be one of {SEVERITIES}")
        self.units: set[int] = {int(u) for u in raw.get("units", [])}
        self.groups: set[str] = set(raw.get("groups", []))
        hours = raw.get("active_hours")
        self.active_hours = ActiveHours.parse(hours) if hours else None
        self.engine: Engine | None = None

    def applies_to(self, unit: UnitInfo) -> bool:
        if self.units and unit.id not in self.units:
            return False
        if self.groups and not self.groups.intersection(unit.groups):
            return False
        return True

    def is_active(self, t: int) -> bool:
        if not self.active_hours or self.engine is None:
            return True
        local = datetime.fromtimestamp(t, self.engine.tz).time()
        return self.active_hours.contains(local)

    def event(self, action: str, unit: UnitInfo, t: int, title: str, **details: Any) -> AlertEvent:
        return AlertEvent(action, unit.id, self.id, self.severity, t, title, self.oneshot, details)

    # hooks -----------------------------------------------------------------
    def on_message(self, unit: UnitInfo, msg: Message, st: dict) -> list[AlertEvent]:
        return []

    def on_tick(self, unit: UnitInfo, now: int, last_t: int | None, st: dict) -> list[AlertEvent]:
        return []


class ConditionRule(Rule):
    """Opens after the condition holds for ``min_duration_s``, resolves when it clears."""

    def __init__(self, raw: dict[str, Any]):
        super().__init__(raw)
        self.min_duration_s = int(raw.get("min_duration_s", 0))

    def condition(self, unit: UnitInfo, msg: Message) -> bool | None:
        raise NotImplementedError

    def cleared(self, unit: UnitInfo, msg: Message) -> bool | None:
        c = self.condition(unit, msg)
        return None if c is None else not c

    def describe(self, unit: UnitInfo, msg: Message, st: dict) -> tuple[str, dict]:
        raise NotImplementedError

    def on_message(self, unit: UnitInfo, msg: Message, st: dict) -> list[AlertEvent]:
        if st.get("open"):
            if self.cleared(unit, msg):
                st.clear()
                return [self.event("resolve", unit, msg.t, f"{self.id} cleared")]
            self.track(unit, msg, st)
            return []
        cond = self.condition(unit, msg)
        if cond is None:
            return []
        if not cond:
            st.clear()
            return []
        st.setdefault("since", msg.t)
        self.track(unit, msg, st)
        if msg.t - st["since"] >= self.min_duration_s and self.is_active(msg.t):
            st["open"] = True
            title, details = self.describe(unit, msg, st)
            details["duration_s"] = msg.t - st["since"]
            return [self.event("open", unit, msg.t, title, **details)]
        return []

    def track(self, unit: UnitInfo, msg: Message, st: dict) -> None:
        """Hook to remember extra data while the condition holds (e.g. max speed)."""


class OfflineRule(Rule):
    kind = "offline"

    def __init__(self, raw: dict[str, Any]):
        super().__init__(raw)
        self.after_s = int(float(raw.get("after_minutes", 60)) * 60)

    def on_tick(self, unit, now, last_t, st):
        if last_t is None or st.get("open"):
            return []
        silent = now - last_t
        if silent >= self.after_s and self.is_active(now):
            st["open"] = True
            hours = silent / 3600
            return [
                self.event(
                    "open", unit, now, f"No data for {hours:.1f} h", last_message_at=last_t, silent_s=silent
                )
            ]
        return []

    def on_message(self, unit, msg, st):
        if st.pop("open", False):
            return [self.event("resolve", unit, msg.t, "Tracker is back online")]
        return []


class OverspeedRule(ConditionRule):
    kind = "overspeed"

    def __init__(self, raw):
        super().__init__(raw)
        self.limit = float(raw["limit_kmh"])
        self.hysteresis = float(raw.get("hysteresis_kmh", 5))

    def condition(self, unit, msg):
        if msg.speed is None or msg.sats is not None and msg.sats < 4:
            return None  # no reliable speed without a GPS fix
        return msg.speed > self.limit

    def cleared(self, unit, msg):
        if msg.speed is None:
            return None
        return msg.speed <= self.limit - self.hysteresis

    def track(self, unit, msg, st):
        st["max"] = max(st.get("max", 0), msg.speed or 0)

    def describe(self, unit, msg, st):
        return (
            f"Speed {st['max']:.0f} km/h (limit {self.limit:.0f})",
            {"max_speed": st["max"], "limit": self.limit, "lat": msg.lat, "lon": msg.lon},
        )


class PowerLossRule(ConditionRule):
    """External power below a threshold: battery removed, wire cut or tracker unplugged."""

    kind = "power_loss"

    def __init__(self, raw):
        super().__init__(raw)
        self.param = raw.get("param", "pwr_ext")
        self.below = float(raw.get("below_volts", 9.0))
        self.scale = float(raw.get("scale", 1.0))  # 0.001 when the tracker reports millivolts

    def volts(self, msg):
        v = msg.params.get(self.param)
        return None if v is None else float(v) * self.scale

    def condition(self, unit, msg):
        v = self.volts(msg)
        return None if v is None else v < self.below

    def cleared(self, unit, msg):
        v = self.volts(msg)
        return None if v is None else v >= self.below + 0.5

    def describe(self, unit, msg, st):
        v = self.volts(msg)
        return (
            f"External power {v:.1f} V (below {self.below:.1f} V)",
            {"volts": v, "lat": msg.lat, "lon": msg.lon},
        )


class GpsLostRule(ConditionRule):
    """Messages keep coming but without a valid fix: antenna damage or a GPS jammer."""

    kind = "gps_lost"

    def __init__(self, raw):
        super().__init__(raw)
        self.min_sats = int(raw.get("min_satellites", 4))

    def condition(self, unit, msg):
        return msg.lat is None or (msg.sats or 0) < self.min_sats

    def describe(self, unit, msg, st):
        return (f"No GPS fix ({msg.sats or 0} satellites)", {"satellites": msg.sats or 0})


class GeofenceRule(Rule):
    kind = "geofence"
    oneshot = True

    def __init__(self, raw):
        super().__init__(raw)
        self.zone_name = raw["zone"]
        self.trigger = raw.get("trigger", "exit")
        if self.trigger not in ("enter", "exit"):
            raise ValueError(f"rule {self.id}: trigger must be enter or exit")
        self.zone: Zone | None = None

    def on_message(self, unit, msg, st):
        if msg.lat is None or msg.lon is None or (msg.sats or 0) < 4:
            return []
        assert self.zone is not None
        inside = self.zone.contains(msg.lat, msg.lon)
        before = st.get("inside")
        st["inside"] = inside
        if before is None or before == inside:
            return []
        happened = "enter" if inside else "exit"
        if happened != self.trigger or not self.is_active(msg.t):
            return []
        verb = "entered" if inside else "left"
        return [
            self.event(
                "open",
                unit,
                msg.t,
                f"{verb} zone {self.zone_name}",
                zone=self.zone_name,
                lat=msg.lat,
                lon=msg.lon,
            )
        ]


class FuelDrainRule(Rule):
    """Fuel level drops while the vehicle stands still.

    The raw LLS value is converted with the tank calibration, smoothed with a
    median of the last readings (fuel sloshes when a truck brakes or parks on
    a slope), and compared with the highest level in a sliding window.
    """

    kind = "fuel_drain"
    oneshot = True
    STABLE_L = 1.5  # level change that still counts as "stopped falling"
    STABLE_READINGS = 3  # consecutive stable readings before the drop is reported

    def __init__(self, raw):
        super().__init__(raw)
        self.min_litres = float(raw.get("min_litres", 20))
        self.window_s = int(float(raw.get("window_minutes", 30)) * 60)
        self.max_speed = float(raw.get("max_speed_kmh", 5))
        self.median_of = int(raw.get("median_of", 5))

    def applies_to(self, unit):
        return unit.fuel is not None and super().applies_to(unit)

    def on_message(self, unit, msg, st):
        raw = msg.params.get(unit.fuel_param or "")
        if raw is None:
            return []
        assert unit.fuel is not None
        readings: list[float] = st.setdefault("readings", [])
        readings.append(unit.fuel.litres(float(raw)))
        del readings[: -self.median_of]
        if len(readings) < self.median_of:
            return []  # not enough readings for a stable median yet
        level = smoothed(readings, self.median_of)
        if (msg.speed or 0) > self.max_speed:
            st["window"] = []  # moving: consumption, not a drain
            st.pop("pending", None)
            return []
        if "pending" in st:
            return self._follow_drop(unit, msg, st, level)
        window: list[tuple[int, float]] = st.setdefault("window", [])
        window.append((msg.t, level))
        while window and msg.t - window[0][0] > self.window_s:
            window.pop(0)
        peak_t, peak = max(window, key=lambda p: p[1])
        if peak - level >= self.min_litres:
            # A drain is in progress. Wait until the level stops falling to report the full amount.
            st["pending"] = {"since": peak_t, "peak": peak, "low": level, "stable": 0}
        return []

    def _follow_drop(self, unit, msg, st, level):
        p = st["pending"]
        if level < p["low"] - self.STABLE_L:
            p["low"], p["stable"] = level, 0
            return []
        p["stable"] += 1
        if p["stable"] < self.STABLE_READINGS:
            return []
        del st["pending"]
        st["window"] = [(msg.t, level)]
        drop = p["peak"] - p["low"]
        if not self.is_active(msg.t):
            return []
        return [
            self.event(
                "open",
                unit,
                msg.t,
                f"Fuel drop {drop:.0f} L while parked",
                litres=round(drop, 1),
                level_before=round(p["peak"], 1),
                level_after=round(p["low"], 1),
                since=p["since"],
                lat=msg.lat,
                lon=msg.lon,
            )
        ]


RULE_TYPES: dict[str, type[Rule]] = {
    r.kind: r for r in (OfflineRule, OverspeedRule, PowerLossRule, GpsLostRule, GeofenceRule, FuelDrainRule)
}


@dataclass
class _UnitRuntime:
    last_t: int | None = None
    state: dict[str, dict] = field(default_factory=dict)


class Engine:
    def __init__(
        self,
        rules: Iterable[Rule],
        units: dict[int, UnitInfo] | None = None,
        zones: dict[str, Zone] | None = None,
        timezone: str = "UTC",
    ):
        self.tz = ZoneInfo(timezone)
        self.rules = list(rules)
        self.units: dict[int, UnitInfo] = dict(units or {})
        self.zones = dict(zones or {})
        ids = [r.id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids must be unique")
        for rule in self.rules:
            rule.engine = self
            if isinstance(rule, GeofenceRule):
                if rule.zone_name not in self.zones:
                    raise ValueError(f"rule {rule.id}: unknown zone {rule.zone_name!r}")
                rule.zone = self.zones[rule.zone_name]
        self._rt: dict[int, _UnitRuntime] = {}

    def unit(self, unit_id: int, name: str | None = None) -> UnitInfo:
        info = self.units.get(unit_id)
        if info is None:
            info = self.units[unit_id] = UnitInfo(unit_id, name or "")
        elif name:
            info.name = name  # Wialon is the source of truth for unit names
        return info

    def last_seen(self, unit_id: int) -> int | None:
        rt = self._rt.get(unit_id)
        return rt.last_t if rt else None

    def seed_last_seen(self, unit_id: int, t: int | None) -> None:
        """Start the offline timer from the last message Wialon already has."""
        if t:
            self._rt.setdefault(unit_id, _UnitRuntime()).last_t = int(t)

    def restore_open(self, unit_id: int, rule_id: str) -> None:
        rt = self._rt.setdefault(unit_id, _UnitRuntime())
        rt.state.setdefault(rule_id, {})["open"] = True

    def on_message(self, msg: Message) -> list[AlertEvent]:
        rt = self._rt.setdefault(msg.unit_id, _UnitRuntime())
        if rt.last_t is not None and msg.t <= rt.last_t:
            return []  # duplicate or out-of-order history message
        unit = self.unit(msg.unit_id)
        out: list[AlertEvent] = []
        for rule in self.rules:
            if rule.applies_to(unit):
                out += rule.on_message(unit, msg, rt.state.setdefault(rule.id, {}))
        rt.last_t = msg.t
        return out

    def tick(self, now: int) -> list[AlertEvent]:
        out: list[AlertEvent] = []
        for unit_id, rt in self._rt.items():
            unit = self.unit(unit_id)
            for rule in self.rules:
                if rule.applies_to(unit):
                    out += rule.on_tick(unit, now, rt.last_t, rt.state.setdefault(rule.id, {}))
        return out
