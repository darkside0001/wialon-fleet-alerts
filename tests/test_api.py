import time

import pytest
from fastapi.testclient import TestClient

from fleet_alerts.api import create_app
from fleet_alerts.db import make_sessionmaker
from fleet_alerts.models import AlertEvent
from fleet_alerts.store import Store
from tests.helpers import T0, engine, msg

KEY = "test-api-key-0123456789"


@pytest.fixture
def ctx():
    eng = engine(
        [{"id": "speed", "type": "overspeed", "limit_kmh": 90}, {"id": "drain", "type": "fuel_drain"}]
    )
    sessions = make_sessionmaker("sqlite://")
    store = Store(sessions, [], 1800, eng)
    store.process([msg(uid=1, t=T0)], [AlertEvent("open", 1, "speed", "warning", T0, "Speed 120", False, {})])
    store.process(
        [msg(uid=2, t=int(time.time()))],
        [AlertEvent("open", 2, "drain", "critical", T0, "Fuel drop 40 L", True, {"litres": 40})],
    )
    store.heartbeat("worker", "ok")
    return TestClient(create_app(sessions, KEY, eng)), store


H = {"X-API-Key": KEY}


def test_api_requires_key(ctx):
    client, _ = ctx
    assert client.get("/api/alerts").status_code == 401
    assert client.get("/api/alerts", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/alerts", headers=H).status_code == 200


def test_short_api_key_is_refused():
    with pytest.raises(ValueError):
        create_app(make_sessionmaker("sqlite://"), "short")


def test_list_and_filter_alerts(ctx):
    client, _ = ctx
    data = client.get("/api/alerts", headers=H).json()
    assert [a["rule"] for a in data] == ["drain", "speed"]
    assert data[0]["details"] == {"litres": 40}
    assert [a["rule"] for a in client.get("/api/alerts?rule=speed", headers=H).json()] == ["speed"]
    assert client.get("/api/alerts?status=resolved", headers=H).json() == []
    assert client.get("/api/alerts?status=bogus", headers=H).status_code == 422


def test_ack_and_close(ctx):
    client, _ = ctx
    alert_id = client.get("/api/alerts?rule=drain", headers=H).json()[0]["id"]
    r = client.post(f"/api/alerts/{alert_id}/ack", json={"by": "dispatcher.anna"}, headers=H)
    assert r.json()["status"] == "acknowledged" and r.json()["acknowledged_by"] == "dispatcher.anna"
    assert (
        client.post(f"/api/alerts/{alert_id}/close", json={"by": "anna"}, headers=H).json()["status"]
        == "resolved"
    )
    assert client.post("/api/alerts/999/ack", json={"by": "x"}, headers=H).status_code == 404
    assert client.post(f"/api/alerts/{alert_id}/ack", json={"by": "<script>"}, headers=H).status_code == 422


def test_units_silent_filter(ctx):
    client, _ = ctx
    assert len(client.get("/api/units", headers=H).json()) == 2
    silent = client.get("/api/units?silent_minutes=60", headers=H).json()
    assert [u["id"] for u in silent] == [1]


def test_health_metrics_rules_and_headers(ctx):
    client, _ = ctx
    r = client.get("/health")
    assert r.json()["status"] == "ok" and r.headers["x-content-type-options"] == "nosniff"
    m = client.get("/metrics").text
    assert "fleet_alerts_units 2" in m
    assert 'fleet_alerts_alerts{rule="speed",severity="warning",status="open"} 1' in m
    assert [r["id"] for r in client.get("/api/rules", headers=H).json()] == ["speed", "drain"]


def test_health_degraded_without_worker():
    client = TestClient(create_app(make_sessionmaker("sqlite://"), KEY))
    assert client.get("/health").json()["status"] == "degraded"
