import json
from urllib.parse import parse_qs

import httpx
import pytest

from fleet_alerts.sources import WialonSource
from fleet_alerts.wialon import WialonClient, WialonError

TOKEN = "a" * 60 + "TOKN"


class FakeWialon:
    """Answers like hst-api.wialon.com, records every request."""

    def __init__(self):
        self.requests = []
        self.queue: dict[str, list] = {}

    def add(self, svc, *responses):
        self.queue.setdefault(svc, []).extend(responses)

    def __call__(self, req: httpx.Request):
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        svc = form.get("svc") or req.url.path.strip("/")
        self.requests.append((str(req.url), form))
        resp = self.queue.get(svc, [{}]).pop(0) if self.queue.get(svc) else {}
        if isinstance(resp, Exception):
            raise resp
        return httpx.Response(200, json=resp)


def make(fake, **kw):
    return WialonClient(
        "https://hst-api.wialon.com",
        TOKEN,
        sleep=lambda s: None,
        http=httpx.Client(transport=httpx.MockTransport(fake)),
        **kw,
    )


def test_login_subscribe_and_events_flow():
    fake = FakeWialon()
    fake.add("token/login", {"eid": "sid-1234567890", "user": {"nm": "demo"}})
    fake.add("core/search_items", {"items": [{"id": 11, "nm": "Truck", "lmsg": {"t": 1700000000}}]})
    fake.add(
        "avl_evts",
        {
            "tm": 1700000100,
            "events": [
                {
                    "i": 11,
                    "t": "m",
                    "d": {
                        "t": 1700000090,
                        "tp": "ud",
                        "pos": {"y": 41.2, "x": 69.1, "s": 50, "sc": 9},
                        "p": {"pwr_ext": 27.9},
                    },
                },
                {"i": 11, "t": "m", "d": {"t": 1700000095, "tp": "evt", "et": "some event"}},
                {"i": 11, "t": "u", "d": {"nm": "Truck renamed"}},
            ],
        },
    )
    src = WialonSource(make(fake))
    units = src.connect()
    assert [(u.id, u.name, u.last_message_at) for u in units] == [(11, "Truck", 1700000000)]
    batch = src.poll()
    assert batch.now == 1700000100
    assert [(m.unit_id, m.t, m.speed, m.params["pwr_ext"]) for m in batch.messages] == [
        (11, 1700000090, 50, 27.9)
    ]
    assert batch.renamed == {11: "Truck renamed"}

    svcs = [f.get("svc") or url.rsplit("/", 1)[-1] for url, f in fake.requests]
    assert svcs == ["token/login", "core/update_data_flags", "core/search_items", "avl_evts"]
    # token and sid travel in the POST body, never in the URL
    for url, _ in fake.requests:
        assert TOKEN not in url and "sid-1234567890" not in url
    assert json.loads(fake.requests[0][1]["params"])["token"] == TOKEN
    assert fake.requests[-1][1]["sid"] == "sid-1234567890"


def test_retry_on_busy_session_then_success():
    fake = FakeWialon()
    fake.add("token/login", {"error": 1003}, {"eid": "sid-xyz12345"})
    assert make(fake).login()["eid"] == "sid-xyz12345"


def test_session_errors_are_flagged():
    fake = FakeWialon()
    fake.add("token/login", {"eid": "sid-abcdefgh"})
    fake.add("avl_evts", {"error": 1})
    c = make(fake)
    c.login()
    with pytest.raises(WialonError) as exc:
        c.events()
    assert exc.value.session_lost


def test_bad_token_error_is_masked():
    fake = FakeWialon()
    fake.add("token/login", {"error": 8, "reason": f"invalid token {TOKEN}"})
    with pytest.raises(WialonError) as exc:
        make(fake).login()
    assert exc.value.code == 8
    assert TOKEN not in str(exc.value) and "***TOKN" in str(exc.value)


def test_network_errors_retry_then_raise_masked():
    fake = FakeWialon()
    fake.add("token/login", *[httpx.ConnectError(f"boom {TOKEN}")] * 3)
    with pytest.raises(WialonError) as exc:
        make(fake).login()
    assert TOKEN not in str(exc.value)
    assert len(fake.requests) == 3


def test_plain_http_is_refused():
    with pytest.raises(WialonError, match="plain HTTP"):
        WialonClient("http://hosting.example.com", TOKEN)
    WialonClient("http://localhost:8080", TOKEN)  # local test servers are fine


def test_events_require_login():
    with pytest.raises(WialonError):
        make(FakeWialon()).events()
