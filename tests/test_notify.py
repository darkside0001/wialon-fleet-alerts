import json
from zoneinfo import ZoneInfo

import httpx
import pytest

from fleet_alerts.config import NotifierConfig
from fleet_alerts.db import Notification, make_sessionmaker
from fleet_alerts.models import AlertEvent
from fleet_alerts.notify import AlertView, NotifyError, render_text
from fleet_alerts.notify.dispatcher import MAX_ATTEMPTS, Dispatcher, LogChannel, backoff, build_channels
from fleet_alerts.notify.telegram import TelegramChannel
from fleet_alerts.notify.webhook import WebhookChannel, verify_signature
from fleet_alerts.store import Store
from tests.helpers import T0

TZ = ZoneInfo("Asia/Tashkent")
TOKEN = "123456:AAH-secret-bot-token-value"


def view(**kw):
    base = dict(
        id=7,
        event="open",
        unit_id=1,
        unit_name="Truck <01>",
        rule_id="speed",
        severity="warning",
        status="open",
        title="Speed 111 km/h",
        opened_at=T0,
        details={"lat": 41.2, "lon": 69.1},
    )
    base.update(kw)
    return AlertView(**base)


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_render_escapes_html_and_links_map():
    text = render_text(view(), TZ)
    assert "Truck &lt;01&gt;" in text
    assert "maps.google.com/?q=41.200000,69.100000" in text
    assert "04:00" in text  # local time
    assert "RESOLVED" in render_text(view(event="resolve", resolved_at=T0 + 60), TZ)


def test_telegram_success_posts_html():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"ok": True})

    TelegramChannel("tg", TOKEN, "-100123", TZ, http=client(handler)).send(view())
    assert "sendMessage" in seen["url"] and "parse_mode=HTML" in seen["body"]


@pytest.mark.parametrize(
    "status, body, permanent, retry_after",
    [
        (
            429,
            {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 17}},
            False,
            17,
        ),
        (400, {"ok": False, "description": "Bad Request: chat not found"}, True, None),
        (502, {"ok": False, "description": "Bad Gateway"}, False, None),
    ],
)
def test_telegram_errors(status, body, permanent, retry_after):
    ch = TelegramChannel("tg", TOKEN, "1", TZ, http=client(lambda r: httpx.Response(status, json=body)))
    with pytest.raises(NotifyError) as exc:
        ch.send(view())
    assert exc.value.permanent is permanent
    assert exc.value.retry_after == retry_after


def test_telegram_token_never_in_errors():
    def boom(req):
        raise httpx.ConnectError(f"cannot connect to {req.url}")

    with pytest.raises(NotifyError) as exc:
        TelegramChannel("tg", TOKEN, "1", TZ, http=client(boom)).send(view())
    assert TOKEN not in str(exc.value)


def test_webhook_signature_roundtrip():
    seen = {}

    def handler(req):
        seen.update(
            body=req.content,
            ts=req.headers["x-fleet-alerts-timestamp"],
            sig=req.headers["x-fleet-alerts-signature"],
        )
        return httpx.Response(204)

    secret = "s3cret-s3cret-s3cret"
    WebhookChannel("crm", "https://crm.example.com/hook", secret, http=client(handler)).send(view())
    assert json.loads(seen["body"])["unit"]["name"] == "Truck <01>"
    assert verify_signature(secret, seen["ts"], seen["body"], seen["sig"])
    assert not verify_signature("wrong-secret-wrong", seen["ts"], seen["body"], seen["sig"])
    assert not verify_signature(secret, seen["ts"], seen["body"] + b" ", seen["sig"])
    assert not verify_signature(secret, seen["ts"], seen["body"], seen["sig"], now=int(seen["ts"]) + 3600)


def test_webhook_requires_https_and_long_secret():
    with pytest.raises(ValueError, match="https"):
        WebhookChannel("x", "http://crm.example.com", "s" * 20)
    with pytest.raises(ValueError, match="16"):
        WebhookChannel("x", "https://crm.example.com", "short")


def test_backoff_grows_and_caps():
    assert [backoff(n) for n in (1, 2, 3)] == [30, 60, 120]
    assert backoff(20) == 3600


class Flaky:
    def __init__(self, name, errors):
        self.name, self.errors, self.sent = name, list(errors), []

    def send(self, alert):
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(alert)


def setup(channel):
    sessions = make_sessionmaker("sqlite://")
    store = Store(sessions, [NotifierConfig("log", channel.name, "info")])
    store.process(events=[AlertEvent("open", 1, "speed", "warning", T0, "Speed", False, {})])
    return sessions, Dispatcher(sessions, {channel.name: channel})


def notif(sessions):
    with sessions() as s:
        return s.query(Notification).one()


def test_dispatcher_retries_then_sends():
    ch = Flaky("ops", [NotifyError("timeout")])
    sessions, d = setup(ch)
    assert d.run_once(now=1000) == {"sent": 0, "retry": 1, "failed": 0}
    n = notif(sessions)
    assert n.status == "pending" and n.next_attempt_at == 1030
    assert d.run_once(now=1010)["sent"] == 0  # not due yet
    assert d.run_once(now=1030)["sent"] == 1
    assert notif(sessions).status == "sent" and len(ch.sent) == 1


def test_dispatcher_honours_retry_after_and_gives_up():
    ch = Flaky("ops", [NotifyError("429", retry_after=5)] + [NotifyError("down")] * MAX_ATTEMPTS)
    sessions, d = setup(ch)
    d.run_once(now=0)
    assert notif(sessions).next_attempt_at == 5
    now = 5
    for _ in range(MAX_ATTEMPTS):
        d.run_once(now=now)
        now += 4000
    n = notif(sessions)
    assert n.status == "failed" and n.attempts == MAX_ATTEMPTS


def test_dispatcher_permanent_error_fails_immediately():
    sessions, d = setup(Flaky("ops", [NotifyError("chat not found", permanent=True)]))
    assert d.run_once(now=0)["failed"] == 1


def test_build_channels():
    chans = build_channels(
        [
            NotifierConfig("log", "a"),
            NotifierConfig("telegram", "b", options={"token": TOKEN, "chat_id": "1"}),
        ],
        TZ,
    )
    assert isinstance(chans["a"], LogChannel) and isinstance(chans["b"], TelegramChannel)
    with pytest.raises(ValueError, match="duplicate"):
        build_channels([NotifierConfig("log", "a"), NotifierConfig("log", "a")], TZ)
    with pytest.raises(ValueError, match="required"):
        build_channels([NotifierConfig("telegram", "c")], TZ)
