"""Read/ack API for dispatchers and integrations, plus health and Prometheus metrics."""

from __future__ import annotations

import hmac
import time
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from . import __version__
from .db import Alert, Notification, Unit, WorkerStatus
from .rules import Engine
from .store import Store

HEARTBEAT_STALE_S = 300


class AckBody(BaseModel):
    by: str = Field(min_length=1, max_length=100, pattern=r"^[\w .@-]+$")


def alert_json(a: Alert, unit_name: str | None = None) -> dict[str, Any]:
    return {
        "id": a.id,
        "unit": {"id": a.unit_id, "name": unit_name},
        "rule": a.rule_id,
        "severity": a.severity,
        "status": a.status,
        "title": a.title,
        "details": a.details,
        "opened_at": a.opened_at,
        "resolved_at": a.resolved_at,
        "acknowledged_at": a.acknowledged_at,
        "acknowledged_by": a.acknowledged_by,
    }


def create_app(sessions: sessionmaker, api_key: str, engine: Engine | None = None) -> FastAPI:
    if len(api_key) < 16:
        raise ValueError("API_KEY must be at least 16 characters")
    store = Store(sessions, engine=engine)
    app = FastAPI(title="wialon-fleet-alerts", version=__version__)

    def require_key(request: Request) -> None:
        given = request.headers.get("x-api-key", "")
        if not hmac.compare_digest(given.encode(), api_key.encode()):
            raise HTTPException(401, "missing or invalid X-API-Key")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp: Response = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    def heartbeat_age() -> int | None:
        with sessions() as s:
            row = s.get(WorkerStatus, "worker")
            return None if row is None else int(time.time()) - row.updated_at

    @app.get("/health")
    def health() -> dict[str, Any]:
        age = heartbeat_age()
        ok = age is not None and age < HEARTBEAT_STALE_S
        return {"status": "ok" if ok else "degraded", "worker_heartbeat_age_s": age, "version": __version__}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        now = int(time.time())
        lines = []
        with sessions() as s:
            lines.append("# TYPE fleet_alerts_units gauge")
            lines.append(f"fleet_alerts_units {s.scalar(select(func.count()).select_from(Unit))}")
            stale = s.scalar(select(func.count()).select_from(Unit).where(Unit.last_message_at < now - 3600))
            lines.append("# TYPE fleet_alerts_units_silent_1h gauge")
            lines.append(f"fleet_alerts_units_silent_1h {stale}")
            lines.append("# TYPE fleet_alerts_alerts gauge")
            for rule, sev, status, n in s.execute(
                select(Alert.rule_id, Alert.severity, Alert.status, func.count()).group_by(
                    Alert.rule_id, Alert.severity, Alert.status
                )
            ):
                lines.append(f'fleet_alerts_alerts{{rule="{rule}",severity="{sev}",status="{status}"}} {n}')
            lines.append("# TYPE fleet_alerts_notifications gauge")
            for status, n in s.execute(
                select(Notification.status, func.count()).group_by(Notification.status)
            ):
                lines.append(f'fleet_alerts_notifications{{status="{status}"}} {n}')
        age = heartbeat_age()
        lines.append("# TYPE fleet_alerts_worker_heartbeat_age_seconds gauge")
        lines.append(f"fleet_alerts_worker_heartbeat_age_seconds {age if age is not None else -1}")
        return "\n".join(lines) + "\n"

    api = [Depends(require_key)]

    @app.get("/api/units", dependencies=api)
    def units(silent_minutes: int | None = Query(None, ge=1)) -> list[dict[str, Any]]:
        with sessions() as s:
            q = select(Unit).order_by(Unit.name)
            if silent_minutes:
                q = q.where(Unit.last_message_at < int(time.time()) - silent_minutes * 60)
            return [
                {
                    "id": u.id,
                    "name": u.name,
                    "last_message_at": u.last_message_at,
                    "lat": u.lat,
                    "lon": u.lon,
                    "speed": u.speed,
                }
                for u in s.scalars(q)
            ]

    @app.get("/api/alerts", dependencies=api)
    def alerts(
        status: str | None = Query(None, pattern="^(open|acknowledged|resolved|active)$"),
        rule: str | None = Query(None, max_length=64),
        unit_id: int | None = None,
        limit: int = Query(100, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        with sessions() as s:
            q = select(Alert, Unit.name).outerjoin(Unit, Unit.id == Alert.unit_id).order_by(Alert.id.desc())
            if status == "active":
                q = q.where(Alert.status.in_(("open", "acknowledged")))
            elif status:
                q = q.where(Alert.status == status)
            if rule:
                q = q.where(Alert.rule_id == rule)
            if unit_id is not None:
                q = q.where(Alert.unit_id == unit_id)
            return [alert_json(a, name) for a, name in s.execute(q.limit(limit))]

    @app.get("/api/alerts/{alert_id}", dependencies=api)
    def alert(alert_id: int) -> dict[str, Any]:
        with sessions() as s:
            a = s.get(Alert, alert_id)
            if a is None:
                raise HTTPException(404, "alert not found")
            u = s.get(Unit, a.unit_id)
            return alert_json(a, u.name if u else None)

    @app.post("/api/alerts/{alert_id}/ack", dependencies=api)
    def ack(alert_id: int, body: AckBody) -> dict[str, Any]:
        a = store.acknowledge(alert_id, body.by)
        if a is None:
            raise HTTPException(404, "alert not found")
        return alert_json(a)

    @app.post("/api/alerts/{alert_id}/close", dependencies=api)
    def close(alert_id: int, body: AckBody) -> dict[str, Any]:
        a = store.close(alert_id, body.by)
        if a is None:
            raise HTTPException(404, "alert not found")
        return alert_json(a)

    @app.get("/api/rules", dependencies=api)
    def rules() -> list[dict[str, Any]]:
        if engine is None:
            return []
        return [
            {
                "id": r.id,
                "type": r.kind,
                "severity": r.severity,
                "oneshot": r.oneshot,
                "units": sorted(r.units),
                "groups": sorted(r.groups),
            }
            for r in engine.rules
        ]

    return app
