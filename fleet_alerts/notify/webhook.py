"""Signed JSON webhook, for CRMs, ticket systems or a dispatcher's own tools.

Every request carries::

    X-Fleet-Alerts-Timestamp: 1763800000
    X-Fleet-Alerts-Signature: sha256=<hex HMAC-SHA256 of "<timestamp>.<body>" with the shared secret>

The receiver recomputes the HMAC and rejects old timestamps, which stops
forged and replayed calls. See :func:`verify_signature`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx

from ..wialon import USER_AGENT
from . import AlertView, NotifyError


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify_signature(
    secret: str, timestamp: str, body: bytes, signature: str, max_age_s: int = 300, now: float | None = None
) -> bool:
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > max_age_s:
        return False
    return hmac.compare_digest(sign(secret, ts, body), signature)


class WebhookChannel:
    def __init__(self, name: str, url: str, secret: str, http: httpx.Client | None = None):
        if not url.startswith("https://") and not url.startswith("http://localhost"):
            raise ValueError(f"notifier {name}: webhook url must use https")
        if len(secret or "") < 16:
            raise ValueError(f"notifier {name}: webhook secret must be at least 16 characters")
        self.name = name
        self.url = url
        self._secret = secret
        self.http = http or httpx.Client(timeout=15)

    def send(self, alert: AlertView) -> None:
        body = json.dumps(alert.as_dict(), separators=(",", ":"), ensure_ascii=False).encode()
        ts = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "X-Fleet-Alerts-Timestamp": str(ts),
            "X-Fleet-Alerts-Signature": sign(self._secret, ts, body),
        }
        try:
            resp = self.http.post(self.url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise NotifyError(f"webhook: {type(exc).__name__}") from None
        if resp.status_code < 300:
            return
        raise NotifyError(
            f"webhook: HTTP {resp.status_code}",
            permanent=400 <= resp.status_code < 500 and resp.status_code not in (408, 429),
        )
