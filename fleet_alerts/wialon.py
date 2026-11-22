"""Wialon Remote API client for a long-running service.

How the worker gets data in real time (no per-unit polling):

1. ``token/login`` -> session id (``eid``).
2. ``core/update_data_flags`` subscribes the session to all units with the
   flags it needs (base, last message, position).
3. ``avl_evts`` is called every few seconds and returns only what changed
   since the previous call: new messages (``t: "m"``), item updates
   (``t: "u"``) and deletions (``t: "d"``). Calling it also keeps the
   session alive; Wialon drops idle sessions after ~5 minutes.

The token and session id travel in POST bodies, never in URLs, and are masked
in every error message. Plain HTTP is refused except for localhost.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from . import __version__

USER_AGENT = f"wialon-fleet-alerts/{__version__}"

FLAG_BASE = 0x1
FLAG_POSITION = 0x400  # last message and position
FLAG_ADVANCED = 0x100
SUBSCRIBE_FLAGS = FLAG_BASE | FLAG_POSITION

SESSION_ERRORS = {1, 1011}  # invalid session / IP changed or session expired
RETRYABLE_ERRORS = {5, 1003}  # error performing request / one request at a time


class WialonError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code

    @property
    def session_lost(self) -> bool:
        return self.code in SESSION_ERRORS


def redact(text: str, *secrets: str | None) -> str:
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, "***" + secret[-4:])
    return text


class WialonClient:
    def __init__(
        self,
        host: str,
        token: str,
        *,
        http: httpx.Client | None = None,
        timeout: float = 30.0,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        host = host.rstrip("/")
        if not (
            host.startswith("https://")
            or host.startswith("http://localhost")
            or host.startswith("http://127.0.0.1")
        ):
            raise WialonError("refusing to send a Wialon token over plain HTTP; use an https:// host")
        if not token:
            raise WialonError("WIALON_TOKEN is empty")
        self.host = host
        self._token = token
        self.http = http or httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT})
        self.retries = retries
        self.sleep = sleep
        self.sid: str | None = None

    def _mask(self, text: str) -> str:
        return redact(text, self._token, self.sid)

    def _post(self, path: str, data: dict[str, str], label: str) -> Any:
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.http.post(f"{self.host}{path}", data=data)
                resp.raise_for_status()
                result = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                if attempt < self.retries:
                    self.sleep(attempt)
                    continue
                raise WialonError(self._mask(f"{label}: {type(exc).__name__}: {exc}")) from None
            if isinstance(result, dict) and result.get("error"):
                code = int(result["error"])
                if code in RETRYABLE_ERRORS and attempt < self.retries:
                    self.sleep(attempt)
                    continue
                reason = result.get("reason", "")
                raise WialonError(self._mask(f"{label}: Wialon error {code} {reason}".strip()), code)
            return result
        raise AssertionError("unreachable")

    def call(self, svc: str, params: dict[str, Any] | None = None) -> Any:
        data = {"svc": svc, "params": json.dumps(params or {}, separators=(",", ":"))}
        if self.sid:
            data["sid"] = self.sid
        return self._post("/wialon/ajax.html", data, svc)

    # -- session -------------------------------------------------------------
    def login(self) -> dict[str, Any]:
        self.sid = None
        result = self.call("token/login", {"token": self._token, "fl": 1})
        self.sid = result.get("eid")
        if not self.sid:
            raise WialonError("token/login: no session id in the response")
        return result

    def logout(self) -> None:
        if self.sid:
            try:
                self.call("core/logout")
            except WialonError:
                pass
            self.sid = None

    # -- data ----------------------------------------------------------------
    def search_units(self, flags: int = SUBSCRIBE_FLAGS) -> list[dict[str, Any]]:
        result = self.call(
            "core/search_items",
            {
                "spec": {
                    "itemsType": "avl_unit",
                    "propName": "sys_name",
                    "propValueMask": "*",
                    "sortType": "sys_name",
                },
                "force": 1,
                "flags": flags,
                "from": 0,
                "to": 0,
            },
        )
        return result.get("items", [])

    def subscribe_units(self, flags: int = SUBSCRIBE_FLAGS) -> Any:
        return self.call(
            "core/update_data_flags",
            {
                "spec": [{"type": "type", "data": "avl_unit", "flags": flags, "mode": 0}],
            },
        )

    def events(self) -> dict[str, Any]:
        """One ``avl_evts`` call: ``{"tm": <server time>, "events": [...]}``."""
        if not self.sid:
            raise WialonError("not logged in", 1)
        return self._post("/avl_evts", {"sid": self.sid}, "avl_evts")
