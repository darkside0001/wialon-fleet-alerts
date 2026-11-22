"""Fuel level sensor helpers.

Trackers send the raw value of a capacitive level sensor (LLS) over RS-485,
for example ``rs485_lls1 = 2310``. Litres come from the tank's calibration
table (raw value -> litres, measured by filling the tank in steps), the same
table that is loaded into the Wialon sensor. Between points the value is
interpolated linearly, outside the table it is clamped.
"""

from __future__ import annotations

from bisect import bisect_right
from statistics import median


class Calibration:
    def __init__(self, points: list[tuple[float, float]]):
        pts = sorted((float(r), float(v)) for r, v in points)
        if len(pts) < 2:
            raise ValueError("a calibration table needs at least 2 points")
        raws = [r for r, _ in pts]
        if len(set(raws)) != len(raws):
            raise ValueError("calibration table has duplicate raw values")
        if any(b[1] < a[1] for a, b in zip(pts, pts[1:], strict=False)):
            raise ValueError("calibration table must not decrease (check for typos)")
        self.points = pts
        self._raws = raws

    def litres(self, raw: float) -> float:
        pts = self.points
        if raw <= pts[0][0]:
            return pts[0][1]
        if raw >= pts[-1][0]:
            return pts[-1][1]
        i = bisect_right(self._raws, raw)
        (r0, v0), (r1, v1) = pts[i - 1], pts[i]
        return v0 + (v1 - v0) * (raw - r0) / (r1 - r0)


def smoothed(values: list[float], size: int = 5) -> float:
    """Median of the last ``size`` readings: removes fuel sloshing spikes."""
    return median(values[-size:])
