from pathlib import Path

from fleet_alerts.config import NotifierConfig
from fleet_alerts.db import Alert, WorkerStatus, make_sessionmaker
from fleet_alerts.notify.dispatcher import Dispatcher, LogChannel
from fleet_alerts.sources import Batch, TraceSource, UnitSnapshot
from fleet_alerts.store import Store
from fleet_alerts.wialon import WialonError
from fleet_alerts.worker import Worker
from tests.helpers import T0, engine, msg

ROOT = Path(__file__).resolve().parents[1]


class ScriptedSource:
    def __init__(self, batches):
        self.batches = list(batches)
        self.connects = 0
        self.closed = False

    def connect(self):
        self.connects += 1
        return [UnitSnapshot(1, "Truck 01", T0 - 60)]

    def poll(self):
        item = self.batches.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


def build(source, rules):
    eng = engine(rules)
    sessions = make_sessionmaker("sqlite://")
    ch = LogChannel("ops", eng.tz)
    store = Store(sessions, [NotifierConfig("log", "ops", "info")], 1800, eng)
    return (
        Worker(
            source,
            eng,
            store,
            Dispatcher(sessions, {"ops": ch}),
            poll_interval_s=0,
            tick_interval_s=60,
            sleep=lambda s: None,
        ),
        ch,
        sessions,
    )


SPEED = [{"id": "speed", "type": "overspeed", "limit_kmh": 90}]


def test_step_runs_full_pipeline_and_sends_notification():
    src = ScriptedSource([Batch([msg(t=T0, speed=120)], {}, T0)])
    w, ch, sessions = build(src, SPEED)
    assert w.step() == 1
    assert [a.title for a in ch.sent] == ["Speed 120 km/h (limit 90)"]
    assert ch.sent[0].unit_name == "Truck 01"
    with sessions() as s:
        assert s.get(WorkerStatus, "worker").value.startswith("ok")


def test_reconnects_after_session_loss_and_survives_errors():
    import threading

    stop = threading.Event()
    batches = [
        Batch([], {}, T0),
        WialonError("invalid session", 1),
        RuntimeError("bad batch"),
        Batch([msg(t=T0, speed=120)], {}, T0),
    ]
    src = ScriptedSource(batches)

    w, ch, _ = build(src, SPEED)

    def sleep(_):
        if not src.batches:
            stop.set()

    w.sleep = sleep
    w.run(stop)
    assert src.connects == 2  # initial + after invalid session
    assert w.stats["errors"] == 2
    assert len(ch.sent) == 1 and src.closed


def test_offline_detected_from_wialon_last_message_time():
    src = ScriptedSource([Batch([], {}, T0 + 4000)])
    w, ch, _ = build(src, [{"id": "off", "type": "offline", "after_minutes": 60}])
    w.step()
    assert [a.rule_id for a in ch.sent] == ["off"]


def test_demo_trace_runs_through_worker():
    src = TraceSource(ROOT / "samples" / "demo-trace.jsonl", speedup=3600, poll_interval_s=1)
    from fleet_alerts.config import load_rules

    cfg = load_rules(ROOT / "samples" / "rules.yaml", expand_env=False)
    sessions = make_sessionmaker("sqlite://")
    ch = LogChannel("log", cfg.engine.tz)
    store = Store(sessions, cfg.notifiers, cfg.cooldown_s, cfg.engine)
    w = Worker(
        src, cfg.engine, store, Dispatcher(sessions, {"log": ch}), poll_interval_s=0, tick_interval_s=60
    )
    while not src.finished:
        w.step()
    with sessions() as s:
        rules = sorted({a.rule_id for a in s.query(Alert)})
    assert rules == ["depot-night-exit", "fuel-drain", "gps-lost", "offline", "overspeed", "power-cut"]
    assert len(ch.sent) >= 6
