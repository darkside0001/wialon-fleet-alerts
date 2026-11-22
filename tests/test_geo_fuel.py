import pytest

from fleet_alerts.fuel import Calibration, smoothed
from fleet_alerts.geo import Zone, haversine_m, point_in_polygon


def test_haversine_known_distance():
    # 0.01 degree of latitude is ~1112 m
    assert haversine_m(41.0, 69.0, 41.01, 69.0) == pytest.approx(1112, rel=0.01)


def test_circle_zone():
    z = Zone.from_config("depot", {"type": "circle", "center": [41.23, 69.15], "radius_m": 400})
    assert z.contains(41.2310, 69.1505)
    assert not z.contains(41.2400, 69.1500)


def test_polygon_zone_and_concave_shape():
    square = [(0, 0), (0, 10), (10, 10), (10, 0)]
    assert point_in_polygon(5, 5, square)
    assert not point_in_polygon(11, 5, square)
    u_shape = [(0, 0), (0, 10), (10, 10), (10, 7), (3, 7), (3, 3), (10, 3), (10, 0)]
    assert not point_in_polygon(5, 5, u_shape)  # inside the notch
    assert point_in_polygon(1, 5, u_shape)


@pytest.mark.parametrize(
    "raw, err",
    [
        ({"type": "circle", "center": [1, 2], "radius_m": 0}, "radius_m"),
        ({"type": "polygon", "points": [[0, 0], [1, 1]]}, "3 points"),
        ({"type": "hexagon"}, "unknown type"),
    ],
)
def test_bad_zones(raw, err):
    with pytest.raises(ValueError, match=err):
        Zone.from_config("z", raw)


def test_calibration_interpolates_and_clamps():
    c = Calibration([(1000, 100), (0, 0), (2000, 150)])
    assert c.litres(500) == 50
    assert c.litres(1500) == 125
    assert c.litres(-10) == 0
    assert c.litres(9999) == 150


@pytest.mark.parametrize(
    "points, err",
    [
        ([(0, 0)], "at least 2"),
        ([(0, 0), (0, 5)], "duplicate"),
        ([(0, 0), (100, 50), (200, 40)], "must not decrease"),
    ],
)
def test_calibration_rejects_bad_tables(points, err):
    with pytest.raises(ValueError, match=err):
        Calibration(points)


def test_median_removes_slosh_spike():
    assert smoothed([100, 101, 160, 99, 100]) == 100
