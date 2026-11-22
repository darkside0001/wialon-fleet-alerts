"""Turns engine events into alert rows and queues notifications (outbox)."""

from __future__ import annotations

import time
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .config import NotifierConfig
from .db import Alert, Notification, Unit, WorkerStatus
from .models import AlertEvent, Message, severity_rank
from .rules import Engine

ACTIVE = ("open", "acknowledged")


class Store:
    def __init__(
        self,
        sessions: sessionmaker,
        notifiers: Iterable[NotifierConfig] = (),
        cooldown_s: int = 1800,
        engine: Engine | None = None,
    ):
        self.sessions = sessions
        self.notifiers = list(notifiers)
        self.cooldown_s = cooldown_s
        self.engine = engine

    # -- units -----------------------------------------------------------------
    def upsert_unit(
        self, s: Session, unit_id: int, name: str | None = None, msg: Message | None = None
    ) -> None:
        unit = s.get(Unit, unit_id)
        if unit is None:
            unit = Unit(id=unit_id, name=name or f"unit {unit_id}")
            s.add(unit)
        if name:
            unit.name = name
        if msg is not None and (unit.last_message_at is None or msg.t > unit.last_message_at):
            unit.last_message_at = msg.t
            if msg.lat is not None:
                unit.lat, unit.lon, unit.speed = msg.lat, msg.lon, msg.speed
        unit.updated_at = int(time.time())

    # -- alerts ----------------------------------------------------------------
    def _groups(self, unit_id: int) -> tuple[str, ...]:
        if self.engine and unit_id in self.engine.units:
            return self.engine.units[unit_id].groups
        return ()

    def _route(self, alert: Alert, event: str) -> list[NotifierConfig]:
        groups = set(self._groups(alert.unit_id))
        out = []
        for n in self.notifiers:
            if severity_rank(alert.severity) < severity_rank(n.min_severity):
                continue
            if n.groups and not groups.intersection(n.groups):
                continue
            if event == "resolve" and not n.notify_resolve:
                continue
            out.append(n)
        return out

    def _enqueue(self, s: Session, alert: Alert, event: str, only: set[str] | None = None) -> None:
        now = int(time.time())
        for n in self._route(alert, event):
            if only is not None and n.name not in only:
                continue
            s.add(
                Notification(
                    alert_id=alert.id,
                    notifier=n.name,
                    event=event,
                    status="pending",
                    attempts=0,
                    next_attempt_at=0,
                    created_at=now,
                )
            )  # 0 = due now

    def apply(self, s: Session, ev: AlertEvent) -> Alert | None:
        """Apply one engine event. Returns the alert that was created or resolved."""
        if ev.action == "open":
            if ev.oneshot:
                recent = s.scalar(
                    select(Alert.id)
                    .where(
                        Alert.unit_id == ev.unit_id,
                        Alert.rule_id == ev.rule_id,
                        Alert.opened_at > ev.t - self.cooldown_s,
                    )
                    .limit(1)
                )
                if recent is not None:
                    return None
            else:
                active = s.scalar(
                    select(Alert.id)
                    .where(Alert.unit_id == ev.unit_id, Alert.rule_id == ev.rule_id, Alert.status.in_(ACTIVE))
                    .limit(1)
                )
                if active is not None:
                    return None
            alert = Alert(
                unit_id=ev.unit_id,
                rule_id=ev.rule_id,
                severity=ev.severity,
                status="open",
                oneshot=ev.oneshot,
                title=ev.title,
                details=ev.details,
                opened_at=ev.t,
            )
            s.add(alert)
            s.flush()
            self._enqueue(s, alert, "open")
            return alert
        if ev.action == "resolve":
            alert = s.scalar(
                select(Alert)
                .where(
                    Alert.unit_id == ev.unit_id,
                    Alert.rule_id == ev.rule_id,
                    Alert.oneshot.is_(False),
                    Alert.status.in_(ACTIVE),
                )
                .order_by(Alert.id.desc())
                .limit(1)
            )
            if alert is None:
                return None
            alert.status = "resolved"
            alert.resolved_at = ev.t
            notified = set(
                s.scalars(
                    select(Notification.notifier).where(
                        Notification.alert_id == alert.id, Notification.event == "open"
                    )
                )
            )
            self._enqueue(s, alert, "resolve", only=notified)
            return alert
        raise ValueError(f"unknown action {ev.action!r}")

    def process(self, messages: Iterable[Message] = (), events: Iterable[AlertEvent] = ()) -> list[Alert]:
        """Persist messages and engine events in one transaction."""
        changed: list[Alert] = []
        with self.sessions.begin() as s:
            for msg in messages:
                info = self.engine.units.get(msg.unit_id) if self.engine else None
                name = info.name if info and info.name else None
                self.upsert_unit(s, msg.unit_id, name, msg)
            for ev in events:
                alert = self.apply(s, ev)
                if alert is not None:
                    changed.append(alert)
        return changed

    def acknowledge(self, alert_id: int, by: str) -> Alert | None:
        with self.sessions.begin() as s:
            alert = s.get(Alert, alert_id)
            if alert is None:
                return None
            if alert.status == "open":
                alert.status = "acknowledged"
                alert.acknowledged_at = int(time.time())
                alert.acknowledged_by = by
            return alert

    def close(self, alert_id: int, by: str) -> Alert | None:
        """Manually close a one-shot alert (fuel drain checked, geofence exit explained)."""
        with self.sessions.begin() as s:
            alert = s.get(Alert, alert_id)
            if alert is None:
                return None
            if alert.status != "resolved":
                alert.status = "resolved"
                alert.resolved_at = int(time.time())
                if alert.acknowledged_by is None:
                    alert.acknowledged_by = by
                    alert.acknowledged_at = alert.resolved_at
            return alert

    def restore_engine_state(self) -> int:
        """After a restart, tell the engine which condition alerts are still open."""
        if self.engine is None:
            return 0
        with self.sessions() as s:
            rows = s.execute(
                select(Alert.unit_id, Alert.rule_id).where(Alert.status.in_(ACTIVE), Alert.oneshot.is_(False))
            ).all()
            for unit in s.scalars(select(Unit)):
                self.engine.unit(unit.id, unit.name)
                self.engine.seed_last_seen(unit.id, unit.last_message_at)
        for unit_id, rule_id in rows:
            self.engine.restore_open(unit_id, rule_id)
        return len(rows)

    def heartbeat(self, key: str, value: str) -> None:
        with self.sessions.begin() as s:
            row = s.get(WorkerStatus, key)
            now = int(time.time())
            if row is None:
                s.add(WorkerStatus(key=key, value=value, updated_at=now))
            else:
                row.value, row.updated_at = value, now
