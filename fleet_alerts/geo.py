"""Geofence geometry. Coordinates are (lat, lon) in degrees."""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def point_in_polygon(lat: float, lon: float, points: list[tuple[float, float]]) -> bool:
    """Ray casting. Good enough for zones of a few km where the Earth is flat."""
    inside = False
    n = len(points)
    j = n - 1
    for i in range(n):
        yi, xi = points[i]
        yj, xj = points[j]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


@dataclass(frozen=True)
class Zone:
    name: str
    kind: str  # "circle" | "polygon"
    center: tuple[float, float] | None = None
    radius_m: float = 0.0
    points: tuple[tuple[float, float], ...] = ()

    def contains(self, lat: float, lon: float) -> bool:
        if self.kind == "circle":
            assert self.center is not None
            return haversine_m(lat, lon, *self.center) <= self.radius_m
        return point_in_polygon(lat, lon, list(self.points))

    @classmethod
    def from_config(cls, name: str, raw: dict) -> Zone:
        kind = raw.get("type")
        if kind == "circle":
            lat, lon = raw["center"]
            radius = float(raw["radius_m"])
            if radius <= 0:
                raise ValueError(f"zone {name}: radius_m must be > 0")
            return cls(name, "circle", center=(float(lat), float(lon)), radius_m=radius)
        if kind == "polygon":
            pts = tuple((float(a), float(b)) for a, b in raw["points"])
            if len(pts) < 3:
                raise ValueError(f"zone {name}: a polygon needs at least 3 points")
            return cls(name, "polygon", points=pts)
        raise ValueError(f"zone {name}: unknown type {kind!r} (use circle or polygon)")
