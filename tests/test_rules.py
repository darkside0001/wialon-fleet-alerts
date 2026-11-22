import pytest

from fleet_alerts.config import ConfigError
from tests.helpers import T0, engine, msg


def actions(events):
    return [(e.action, e.rule_id) for e in events]


def test_overspeed_is_debounced_and_uses_hysteresis():
    e = engine([{"id": "speed", "type": "overspeed", "limit_kmh": 90, "min_duration_s": 60}])
    assert e.on_message(msg(t=T0, speed=100)) == []
    assert e.on_message(msg(t=T0 + 30, speed=105)) == []
    ev = e.on_message(msg(t=T0 + 60, speed=98))
    assert actions(ev) == [("open", "speed")]
    assert ev[0].details["max_speed"] == 105
    assert e.on_message(msg(t=T0 + 90, speed=88)) == []  # within hysteresis
    assert actions(e.on_message(msg(t=T0 + 120, speed=80))) == [("resolve", "speed")]


def test_short_overspeed_spike_is_ignored():
    e = engine([{"id": "speed", "type": "overspeed", "limit_kmh": 90, "min_duration_s": 60}])
    e.on_message(msg(t=T0, speed=120))
    e.on_message(msg(t=T0 + 30, speed=70))
    assert e.on_message(msg(t=T0 + 70, speed=95)) == []  # timer restarted


def test_overspeed_ignores_speed_without_gps_fix():
    e = engine([{"id": "speed", "type": "overspeed", "limit_kmh": 90}])
    assert e.on_message(msg(t=T0, speed=250, sats=2)) == []


def test_offline_opens_on_tick_and_resolves_on_message():
    e = engine([{"id": "off", "type": "offline", "after_minutes": 60}])
    e.on_message(msg(t=T0))
    assert e.tick(T0 + 3599) == []
    ev = e.tick(T0 + 3600)
    assert actions(ev) == [("open", "off")]
    assert e.tick(T0 + 7200) == []  # not repeated
    assert actions(e.on_message(msg(t=T0 + 7300))) == [("resolve", "off")]


def test_offline_timer_seeded_from_wialon_last_message():
    e = engine([{"id": "off", "type": "offline", "after_minutes": 60}])
    e.seed_last_seen(7, T0)
    assert actions(e.tick(T0 + 4000)) == [("open", "off")]


def test_power_loss_with_millivolt_scale():
    e = engine(
        [{"id": "pwr", "type": "power_loss", "below_volts": 9, "scale": 0.001, "severity": "critical"}]
    )
    ev = e.on_message(msg(t=T0, pwr_ext=300))
    assert actions(ev) == [("open", "pwr")] and ev[0].severity == "critical"
    assert e.on_message(msg(t=T0 + 60, pwr_ext=9200)) == []  # 9.2 V: below 9 + 0.5 hysteresis
    assert actions(e.on_message(msg(t=T0 + 120, pwr_ext=12600))) == [("resolve", "pwr")]


def test_power_loss_skips_messages_without_the_param():
    e = engine([{"id": "pwr", "type": "power_loss"}])
    assert e.on_message(msg(t=T0)) == []


def test_gps_lost_after_duration():
    e = engine([{"id": "gps", "type": "gps_lost", "min_duration_s": 600}])
    e.on_message(msg(t=T0, lat=None, lon=None, sats=0))
    assert e.on_message(msg(t=T0 + 300, sats=1)) == []
    assert actions(e.on_message(msg(t=T0 + 600, lat=None, lon=None, sats=0))) == [("open", "gps")]
    assert actions(e.on_message(msg(t=T0 + 660, sats=9))) == [("resolve", "gps")]


ZONES = {"depot": {"type": "circle", "center": [41.23, 69.15], "radius_m": 300}}


def test_geofence_exit_only_at_night():
    e = engine(
        [
            {
                "id": "night",
                "type": "geofence",
                "zone": "depot",
                "trigger": "exit",
                "active_hours": "22:00-06:00",
            }
        ],
        zones=ZONES,
    )
    # T0 is 04:00 local: inside active hours
    assert e.on_message(msg(t=T0)) == []  # first position only sets state
    ev = e.on_message(msg(t=T0 + 60, lat=41.25))
    assert actions(ev) == [("open", "night")] and ev[0].oneshot
    assert e.on_message(msg(t=T0 + 120, lat=41.26)) == []  # still outside: no repeat
    e.on_message(msg(t=T0 + 180))  # back inside
    # 12:00 local: outside active hours
    noon = T0 + 8 * 3600
    e.on_message(msg(t=noon))
    assert e.on_message(msg(t=noon + 60, lat=41.25)) == []


def test_geofence_enter_and_bad_fix_ignored():
    e = engine([{"id": "in", "type": "geofence", "zone": "depot", "trigger": "enter"}], zones=ZONES)
    e.on_message(msg(t=T0, lat=41.25))
    assert e.on_message(msg(t=T0 + 60, sats=2)) == []  # inside but no reliable fix
    assert actions(e.on_message(msg(t=T0 + 120))) == [("open", "in")]


FUEL_UNITS = {"5": {"fuel": {"param": "lls", "table": [[0, 0], [4000, 400]]}}}


def fuel_engine(**rule):
    return engine(
        [{"id": "drain", "type": "fuel_drain", "min_litres": 20, "window_minutes": 30, **rule}],
        units=FUEL_UNITS,
    )


def feed(e, levels, start=T0, step=120, speed=0.0):
    out = []
    for i, litres in enumerate(levels):
        out += e.on_message(msg(uid=5, t=start + i * step, speed=speed, lls=litres * 10))
    return out


def test_fuel_drain_reports_full_amount_once_level_is_stable():
    e = fuel_engine()
    ev = feed(e, [300] * 6 + [290, 275, 260, 250, 250, 250, 250, 250, 250, 250])
    assert actions(ev) == [("open", "drain")]
    assert ev[0].details["litres"] == pytest.approx(50, abs=1)
    assert ev[0].details["level_before"] == pytest.approx(300, abs=1)


def test_fuel_slosh_and_refuel_are_not_drains():
    e = fuel_engine()
    assert feed(e, [300, 340, 260, 305, 298, 302, 300, 296, 301, 299, 380, 382, 381, 380, 380]) == []


def test_fuel_used_while_driving_is_not_a_drain():
    e = fuel_engine()
    assert feed(e, list(range(300, 200, -5)), speed=60) == []


def test_fuel_rule_only_for_units_with_calibration():
    e = fuel_engine()
    assert e.on_message(msg(uid=6, t=T0, lls=3000)) == []
    assert e.on_message(msg(uid=6, t=T0 + 60, lls=10)) == []


def test_unit_and_group_filters():
    e = engine(
        [{"id": "speed", "type": "overspeed", "limit_kmh": 50, "groups": ["trucks"]}],
        units={"1": {"groups": ["trucks"]}, "2": {"groups": ["cars"]}},
    )
    assert actions(e.on_message(msg(uid=1, speed=80))) == [("open", "speed")]
    assert e.on_message(msg(uid=2, speed=80)) == []


def test_duplicate_and_old_messages_are_ignored():
    e = engine([{"id": "speed", "type": "overspeed", "limit_kmh": 50}])
    assert actions(e.on_message(msg(t=T0, speed=80))) == [("open", "speed")]
    assert e.on_message(msg(t=T0, speed=10)) == []
    assert e.on_message(msg(t=T0 - 60, speed=10)) == []


def test_restore_open_lets_engine_resolve_after_restart():
    e = engine([{"id": "speed", "type": "overspeed", "limit_kmh": 50}])
    e.restore_open(1, "speed")
    assert actions(e.on_message(msg(t=T0, speed=10))) == [("resolve", "speed")]


@pytest.mark.parametrize(
    "rules, err",
    [
        ([], "no rules"),
        ([{"id": "x", "type": "teleport"}], "unknown type"),
        ([{"id": "x", "type": "offline"}, {"id": "x", "type": "offline"}], "unique"),
        ([{"id": "x", "type": "offline", "severity": "panic"}], "severity"),
        ([{"id": "x", "type": "geofence", "zone": "nowhere"}], "unknown zone"),
        ([{"id": "x", "type": "overspeed"}], "limit_kmh"),
    ],
)
def test_invalid_rules_are_rejected(rules, err):
    with pytest.raises(ConfigError, match=err):
        engine(rules)
