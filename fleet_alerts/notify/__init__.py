"""Notification channels and the outbox dispatcher."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from typing import Any, Protocol
from zoneinfo import ZoneInfo


class NotifyError(RuntimeError):
    def __init__(self, message: str, *, permanent: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.permanent = permanent
        self.retry_after = retry_after


@dataclass
class AlertView:
    """Everything a channel needs to render one alert, detached from the database."""

    id: int
    event: str  # open | resolve
    unit_id: int
    unit_name: str
    rule_id: str
    severity: str
    status: str
    title: str
    opened_at: int
    resolved_at: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event": self.event,
            "unit": {"id": self.unit_id, "name": self.unit_name},
            "rule": self.rule_id,
            "severity": self.severity,
            "status": self.status,
            "title": self.title,
            "opened_at": self.opened_at,
            "resolved_at": self.resolved_at,
            "details": self.details,
        }


class Channel(Protocol):
    name: str

    def send(self, alert: AlertView) -> None: ...


ICONS = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}


def render_text(alert: AlertView, tz: ZoneInfo) -> str:
    """Telegram HTML message. Unit names come from Wialon and are escaped."""
    when = datetime.fromtimestamp(
        alert.resolved_at if alert.event == "resolve" and alert.resolved_at else alert.opened_at, tz
    ).strftime("%d.%m %H:%M")
    head = (
        "✅ RESOLVED"
        if alert.event == "resolve"
        else f"{ICONS.get(alert.severity, '')} {alert.severity.upper()}"
    )
    lines = [
        f"<b>{head}</b> · {escape(alert.rule_id)}",
        f"<b>{escape(alert.unit_name)}</b>",
        escape(alert.title),
        f"🕒 {when}",
    ]
    lat, lon = alert.details.get("lat"), alert.details.get("lon")
    if alert.event == "open" and lat is not None and lon is not None:
        lines.append(f'📍 <a href="https://maps.google.com/?q={lat:.6f},{lon:.6f}">{lat:.5f}, {lon:.5f}</a>')
    lines.append(f"#alert{alert.id}")
    return "\n".join(lines)
