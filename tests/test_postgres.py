"""Runs the store against a real PostgreSQL when TEST_DATABASE_URL is set (CI job "postgres")."""

import os

import pytest
from sqlalchemy import text

from fleet_alerts.config import NotifierConfig
from fleet_alerts.db import Base, make_sessionmaker
from fleet_alerts.models import AlertEvent
from fleet_alerts.notify.dispatcher import Dispatcher, LogChannel
from fleet_alerts.store import Store
from tests.helpers import T0, msg

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


def test_full_cycle_on_postgres():
    sessions = make_sessionmaker(URL)
    engine = sessions.kw["bind"]
    with engine.begin() as c:
        for t in reversed(Base.metadata.sorted_tables):
            c.execute(text(f"TRUNCATE {t.name} RESTART IDENTITY CASCADE"))
    store = Store(sessions, [NotifierConfig("log", "ops", "info")])
    store.process(
        [msg(uid=1, t=T0)], [AlertEvent("open", 1, "speed", "warning", T0, "Speed", False, {"v": 1})]
    )
    store.process(events=[AlertEvent("open", 1, "speed", "warning", T0 + 5, "Speed", False, {})])
    store.process(events=[AlertEvent("resolve", 1, "speed", "warning", T0 + 60, "ok", False, {})])
    ch = LogChannel("ops", None)
    assert Dispatcher(sessions, {"ops": ch}).run_once(now=T0)["sent"] == 2
    with sessions() as s:
        assert s.execute(text("select count(*) from alerts")).scalar() == 1
