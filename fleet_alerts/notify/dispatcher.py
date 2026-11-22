"""Sends queued notifications with retries and exponential backoff.

Notifications are rows in the ``notifications`` table, written in the same
transaction as the alert (outbox pattern). If Telegram is down or the worker
restarts, nothing is lost: the dispatcher picks up due rows on the next run.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from ..config import NotifierConfig
from ..db import Alert, Notification, Unit
from . import AlertView, Channel, NotifyError, render_text
from .telegram import TelegramChannel
from .webhook import WebhookChannel

log = logging.getLogger("fleet_alerts.notify")

MAX_ATTEMPTS = 8
BASE_DELAY_S = 30
MAX_DELAY_S = 3600


class LogChannel:
    """Writes alerts to the log. Used by the demo and as a safe default."""

    def __init__(self, name: str, tz: ZoneInfo):
        self.name = name
        self.tz = tz
        self.sent: list[AlertView] = []

    def send(self, alert: AlertView) -> None:
        self.sent.append(alert)
        text = re.sub(r"<[^>]+>", "", render_text(alert, self.tz))
        log.info("ALERT %s", text.replace("\n", " | "))


def build_channels(
    configs: list[NotifierConfig], tz: ZoneInfo, http: httpx.Client | None = None
) -> dict[str, Channel]:
    channels: dict[str, Channel] = {}
    for c in configs:
        if c.name in channels:
            raise ValueError(f"duplicate notifier name {c.name!r}")
        if c.type == "telegram":
            channels[c.name] = TelegramChannel(
                c.name, c.options.get("token", ""), c.options.get("chat_id", ""), tz, http=http
            )
        elif c.type == "webhook":
            channels[c.name] = WebhookChannel(
                c.name, c.options.get("url", ""), c.options.get("secret", ""), http=http
            )
        else:
            channels[c.name] = LogChannel(c.name, tz)
    return channels


def backoff(attempts: int) -> int:
    return min(MAX_DELAY_S, BASE_DELAY_S * 2 ** max(0, attempts - 1))


class Dispatcher:
    def __init__(self, sessions: sessionmaker, channels: Mapping[str, Channel], batch: int = 50):
        self.sessions = sessions
        self.channels = channels
        self.batch = batch

    def run_once(self, now: int | None = None) -> dict[str, int]:
        now = int(now if now is not None else time.time())
        stats = {"sent": 0, "retry": 0, "failed": 0}
        with self.sessions.begin() as s:
            due = s.scalars(
                select(Notification)
                .where(Notification.status == "pending", Notification.next_attempt_at <= now)
                .order_by(Notification.id)
                .limit(self.batch)
            ).all()
            for n in due:
                alert = s.get(Alert, n.alert_id)
                channel = self.channels.get(n.notifier)
                if alert is None or channel is None:
                    n.status, n.last_error = "failed", "alert or notifier no longer exists"
                    stats["failed"] += 1
                    continue
                unit = s.get(Unit, alert.unit_id)
                view = AlertView(
                    alert.id,
                    n.event,
                    alert.unit_id,
                    unit.name if unit else f"unit {alert.unit_id}",
                    alert.rule_id,
                    alert.severity,
                    alert.status,
                    alert.title,
                    alert.opened_at,
                    alert.resolved_at,
                    dict(alert.details or {}),
                )
                n.attempts += 1
                try:
                    channel.send(view)
                except NotifyError as exc:
                    n.last_error = str(exc)[:500]
                    if exc.permanent or n.attempts >= MAX_ATTEMPTS:
                        n.status = "failed"
                        stats["failed"] += 1
                        log.error("notification %s to %s failed: %s", n.id, n.notifier, exc)
                    else:
                        delay = exc.retry_after if exc.retry_after else backoff(n.attempts)
                        n.next_attempt_at = now + int(delay)
                        stats["retry"] += 1
                        log.warning(
                            "notification %s to %s: %s, retry in %ss", n.id, n.notifier, exc, int(delay)
                        )
                    continue
                n.status, n.sent_at, n.last_error = "sent", now, None
                stats["sent"] += 1
        return stats
