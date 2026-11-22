from sqlalchemy import select

from fleet_alerts.config import NotifierConfig
from fleet_alerts.db import Alert, Notification, make_sessionmaker
from fleet_alerts.models import AlertEvent
from fleet_alerts.store import Store
from tests.helpers import T0, engine, msg


def make_store(notifiers=(), cooldown=1800, eng=None):
    return Store(make_sessionmaker("sqlite://"), list(notifiers), cooldown, eng)


def ev(action="open", rule="speed", t=T0, severity="warning", oneshot=False, unit=1):
    return AlertEvent(action, unit, rule, severity, t, f"{rule} {action}", oneshot, {})


def rows(store, model):
    with store.sessions() as s:
        return list(s.scalars(select(model).order_by(model.id)))


def test_condition_alert_is_not_duplicated_and_resolves():
    st = make_store([NotifierConfig("log", "ops", "info")])
    st.process(events=[ev()])
    st.process(events=[ev(t=T0 + 60)])  # same condition again
    assert len(rows(st, Alert)) == 1
    st.process(events=[ev("resolve", t=T0 + 120)])
    a = rows(st, Alert)[0]
    assert a.status == "resolved" and a.resolved_at == T0 + 120
    assert [(n.event, n.notifier) for n in rows(st, Notification)] == [("open", "ops"), ("resolve", "ops")]
    st.process(events=[ev(t=T0 + 300)])  # new episode after resolve
    assert len(rows(st, Alert)) == 2


def test_oneshot_alerts_respect_cooldown():
    st = make_store(cooldown=1800)
    st.process(events=[ev(rule="drain", oneshot=True)])
    st.process(events=[ev(rule="drain", oneshot=True, t=T0 + 600)])
    st.process(events=[ev(rule="drain", oneshot=True, t=T0 + 1801)])
    assert len(rows(st, Alert)) == 2


def test_resolve_does_not_touch_oneshot_or_unknown_alerts():
    st = make_store()
    st.process(events=[ev(rule="drain", oneshot=True)])
    assert st.process(events=[ev("resolve", rule="drain")]) == []
    assert st.process(events=[ev("resolve", rule="nothing")]) == []
    assert rows(st, Alert)[0].status == "open"


def test_routing_by_severity_and_group():
    eng = engine([{"id": "x", "type": "offline"}], units={"1": {"groups": ["mining"]}})
    notifiers = [
        NotifierConfig("log", "all", "info"),
        NotifierConfig("log", "critical-only", "critical"),
        NotifierConfig("log", "mining", "info", groups=("mining",), notify_resolve=False),
        NotifierConfig("log", "retail", "info", groups=("retail",)),
    ]
    st = make_store(notifiers, eng=eng)
    st.process(events=[ev(severity="warning")])
    st.process(events=[ev("resolve", t=T0 + 10)])
    got = [(n.event, n.notifier) for n in rows(st, Notification)]
    assert got == [("open", "all"), ("open", "mining"), ("resolve", "all")]


def test_ack_and_close():
    st = make_store()
    alert = st.process(events=[ev(rule="drain", oneshot=True)])[0]
    a = st.acknowledge(alert.id, "dispatcher")
    assert a.status == "acknowledged" and a.acknowledged_by == "dispatcher"
    a = st.close(alert.id, "dispatcher")
    assert a.status == "resolved"
    assert st.acknowledge(999, "x") is None and st.close(999, "x") is None


def test_acknowledged_condition_alert_still_auto_resolves():
    st = make_store()
    alert = st.process(events=[ev()])[0]
    st.acknowledge(alert.id, "op")
    st.process(events=[ev("resolve", t=T0 + 60)])
    assert rows(st, Alert)[0].status == "resolved"


def test_units_are_upserted_with_latest_position():
    st = make_store()
    st.process([msg(uid=3, t=T0, lat=41.0, speed=10), msg(uid=3, t=T0 + 60, lat=41.1, speed=20)])
    st.process([msg(uid=3, t=T0 + 30, lat=99.0)])  # late history message does not move the unit back
    from fleet_alerts.db import Unit

    u = rows(st, Unit)[0]
    assert (u.last_message_at, u.lat, u.speed) == (T0 + 60, 41.1, 20)


def test_restore_engine_state_after_restart():
    eng = engine([{"id": "speed", "type": "overspeed", "limit_kmh": 50}])
    st = make_store(eng=eng)
    st.process([msg(uid=1, t=T0)], [ev()])
    eng2 = engine([{"id": "speed", "type": "overspeed", "limit_kmh": 50}])
    st2 = Store(st.sessions, [], 1800, eng2)
    assert st2.restore_engine_state() == 1
    assert eng2.last_seen(1) == T0
    out = eng2.on_message(msg(uid=1, t=T0 + 60, speed=10))
    assert [e.action for e in out] == ["resolve"]
