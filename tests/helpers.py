from __future__ import annotations

from fleet_alerts.config import parse_rules
from fleet_alerts.models import Message

T0 = 1_763_420_400  # 2025-11-18 04:00 Asia/Tashkent


def msg(uid=1, t=T0, lat=41.23, lon=69.15, speed=0.0, sats=12, **params) -> Message:
    return Message(uid, t, lat, lon, speed, sats, params)


def cfg(rules, **extra):
    raw = {"timezone": "Asia/Tashkent", "rules": rules, **extra}
    return parse_rules(raw, expand_env=False)


def engine(rules, **extra):
    return cfg(rules, **extra).engine
