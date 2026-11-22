"""Domain objects shared by the engine, the store and the notifiers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SEVERITIES = ("info", "warning", "critical")


def severity_rank(severity: str) -> int:
    return SEVERITIES.index(severity)


@dataclass(frozen=True)
class Message:
    """One tracker message, normalised from Wialon's ``avl_evts`` / ``lmsg`` format."""

    unit_id: int
    t: int  # unix time reported by the tracker
    lat: float | None = None
    lon: float | None = None
    speed: float | None = None  # km/h
    sats: int | None = None
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_wialon(cls, unit_id: int, d: dict[str, Any]) -> Message:
        pos = d.get("pos") or {}
        return cls(
            unit_id=int(unit_id),
            t=int(d["t"]),
            lat=pos.get("y"),
            lon=pos.get("x"),
            speed=pos.get("s"),
            sats=pos.get("sc"),
            params=dict(d.get("p") or {}),
        )

    def to_wialon(self) -> dict[str, Any]:
        d: dict[str, Any] = {"t": self.t, "tp": "ud", "p": self.params}
        if self.lat is not None and self.lon is not None:
            d["pos"] = {
                "y": self.lat,
                "x": self.lon,
                "s": self.speed or 0,
                "sc": self.sats or 0,
                "c": 0,
                "z": 0,
            }
        else:
            d["pos"] = None
        return d


@dataclass(frozen=True)
class AlertEvent:
    """Output of the rule engine. The store turns these into alert rows."""

    action: str  # "open" | "resolve"
    unit_id: int
    rule_id: str
    severity: str
    t: int
    title: str
    oneshot: bool = False  # one-shot alerts (fuel drain, geofence) are never auto-resolved
    details: dict[str, Any] = field(default_factory=dict)
