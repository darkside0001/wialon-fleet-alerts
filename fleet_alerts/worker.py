"""The long-running process: read messages, run rules, store alerts, send notifications."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from .notify.dispatcher import Dispatcher
from .rules import Engine
from .sources import Source
from .store import Store
from .wialon import WialonError

log = logging.getLogger("fleet_alerts.worker")

MAX_BACKOFF_S = 300


class Worker:
    def __init__(
        self,
        source: Source,
        engine: Engine,
        store: Store,
        dispatcher: Dispatcher,
        *,
        poll_interval_s: float = 5.0,
        tick_interval_s: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.source = source
        self.engine = engine
        self.store = store
        self.dispatcher = dispatcher
        self.poll_interval_s = poll_interval_s
        self.tick_interval_s = tick_interval_s
        self.sleep = sleep
        self.connected = False
        self._last_tick = 0
        self.stats = {"messages": 0, "alerts": 0, "errors": 0}

    def connect(self) -> None:
        restored = self.store.restore_engine_state()
        units = self.source.connect()
        with self.store.sessions.begin() as s:
            for u in units:
                info = self.engine.unit(u.id, u.name)
                self.store.upsert_unit(s, u.id, info.name)
                if u.last_message_at and self.engine.last_seen(u.id) is None:
                    self.engine.seed_last_seen(u.id, u.last_message_at)
        self.connected = True
        log.info("connected: %d units, %d open alerts restored", len(units), restored)

    def step(self) -> int:
        if not self.connected:
            self.connect()
        batch = self.source.poll()
        for uid, name in batch.renamed.items():
            self.engine.unit(uid, name)
        events = []
        for msg in batch.messages:
            events += self.engine.on_message(msg)
        if batch.now - self._last_tick >= self.tick_interval_s:
            events += self.engine.tick(batch.now)
            self._last_tick = batch.now
        changed = self.store.process(batch.messages, events)
        self.dispatcher.run_once()
        self.stats["messages"] += len(batch.messages)
        self.stats["alerts"] += len(changed)
        self.store.heartbeat("worker", f"ok messages={self.stats['messages']} alerts={self.stats['alerts']}")
        return len(changed)

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        failures = 0
        while not stop.is_set():
            try:
                self.step()
                failures = 0
                delay = self.poll_interval_s
            except WialonError as exc:
                failures += 1
                self.stats["errors"] += 1
                if exc.session_lost:
                    log.warning("Wialon session lost (%s), logging in again", exc)
                    self.connected = False
                    delay = 1.0
                else:
                    delay = min(MAX_BACKOFF_S, 2**failures)
                    log.error("Wialon error: %s; retry in %ss", exc, delay)
                    if exc.code in (7, 8):  # access denied / bad token: reconnecting is the only option
                        self.connected = False
            except Exception:  # noqa: BLE001 - the worker must survive one bad batch
                failures += 1
                self.stats["errors"] += 1
                delay = min(MAX_BACKOFF_S, 2**failures)
                log.exception("worker step failed; retry in %ss", delay)
            stop.wait(delay) if self.sleep is time.sleep else self.sleep(delay)
        self.source.close()
