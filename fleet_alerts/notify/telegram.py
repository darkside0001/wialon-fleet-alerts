"""Telegram Bot API channel.

The bot token is a password. It is read from the environment through the
rules file (``token: ${TELEGRAM_BOT_TOKEN}``), lives only in server memory and
is masked in every error. The Bot API puts the token in the URL path, so this
module never logs request URLs.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import httpx

from ..wialon import USER_AGENT, redact
from . import AlertView, NotifyError, render_text

API = "https://api.telegram.org"


class TelegramChannel:
    def __init__(
        self,
        name: str,
        token: str,
        chat_id: str | int,
        tz: ZoneInfo,
        http: httpx.Client | None = None,
        api: str = API,
    ):
        if not token or not chat_id:
            raise ValueError(f"notifier {name}: token and chat_id are required")
        self.name = name
        self._token = token
        self.chat_id = str(chat_id)
        self.tz = tz
        self.api = api.rstrip("/")
        self.http = http or httpx.Client(timeout=15, headers={"User-Agent": USER_AGENT})

    def send(self, alert: AlertView) -> None:
        try:
            resp = self.http.post(
                f"{self.api}/bot{self._token}/sendMessage",
                data={
                    "chat_id": self.chat_id,
                    "text": render_text(alert, self.tz)[:4096],
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
            )
        except httpx.HTTPError as exc:
            raise NotifyError(redact(f"telegram: {type(exc).__name__}: {exc}", self._token)) from None
        if resp.status_code == 200:
            return
        try:
            body = resp.json()
        except ValueError:
            body = {}
        desc = redact(str(body.get("description", resp.text[:200])), self._token)
        if resp.status_code == 429:
            retry = (body.get("parameters") or {}).get("retry_after", 30)
            raise NotifyError(f"telegram: rate limited: {desc}", retry_after=float(retry))
        if 400 <= resp.status_code < 500:
            # wrong chat id, bot blocked or removed from the group: retrying will not help
            raise NotifyError(f"telegram: HTTP {resp.status_code}: {desc}", permanent=True)
        raise NotifyError(f"telegram: HTTP {resp.status_code}: {desc}")
