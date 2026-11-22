"""Where messages come from: a live Wialon session or a recorded trace (demo)."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .models import Message
from .wialon import WialonClient


@dataclass
class UnitSnapshot:
    id: int
    name: str
    last_message_at: int | None


@dataclass
class Batch:
    messages: list[Message]
    renamed: dict[int, str]
    now: int


class Source(Protocol):
    def connect(self) -> list[UnitSnapshot]: ...
    def poll(self) -> Batch: ...
    def close(self) -> None: ...


class WialonSource:
    def __init__(self, client: WialonClient):
        self.client = client
        self._server_time = 0

    def connect(self) -> list[UnitSnapshot]:
        self.client.login()
        self.client.subscribe_units()
        units = []
        for item in self.client.search_units():
            lmsg = item.get("lmsg") or {}
            units.append(UnitSnapshot(int(item["id"]), item.get("nm", ""), lmsg.get("t")))
        return units

    def poll(self) -> Batch:
        result = self.client.events()
        self._server_time = int(result.get("tm") or time.time())
        messages, renamed = [], {}
        for ev in result.get("events", []):
            kind, uid, d = ev.get("t"), ev.get("i"), ev.get("d") or {}
            if kind == "m" and d.get("tp", "ud") == "ud" and "t" in d:
                messages.append(Message.from_wialon(uid, d))
            elif kind == "u" and "nm" in d:
                renamed[int(uid)] = d["nm"]
        messages.sort(key=lambda m: (m.t, m.unit_id))
        return Batch(messages, renamed, self._server_time)

    def close(self) -> None:
        self.client.logout()


def read_trace(path: str | Path) -> Iterator[tuple[int, str | None, dict[str, Any]]]:
    """JSON lines: ``{"i": <unit id>, "nm": <name, optional>, "d": <Wialon message>}``."""
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                row = json.loads(line)
                yield int(row["i"]), row.get("nm"), row["d"]
            except (ValueError, KeyError) as exc:
                raise ValueError(f"{path}:{n}: bad trace line: {exc}") from None


class TraceSource:
    """Plays a recorded trace on a simulated clock, ``speedup`` times faster than real time.

    Used by ``--demo`` and docker compose, so the whole stack runs without a
    Wialon account. The clock keeps running after the trace ends, which is
    how the demo shows a tracker going offline.
    """

    def __init__(self, path: str | Path, speedup: float = 600, poll_interval_s: float = 1.0):
        rows = list(read_trace(path))
        rows.sort(key=lambda r: r[2]["t"])
        self.rows = rows
        self.speedup = speedup
        self.step = poll_interval_s * speedup
        self.clock = rows[0][2]["t"] - 1 if rows else int(time.time())
        self._i = 0

    def connect(self) -> list[UnitSnapshot]:
        names: dict[int, str] = {}
        for uid, nm, _ in self.rows:
            if nm:
                names.setdefault(uid, nm)
        return [
            UnitSnapshot(uid, names.get(uid, f"unit {uid}"), None)
            for uid in sorted({r[0] for r in self.rows})
        ]

    def poll(self) -> Batch:
        self.clock += int(self.step)
        out = []
        while self._i < len(self.rows) and self.rows[self._i][2]["t"] <= self.clock:
            uid, _, d = self.rows[self._i]
            out.append(Message.from_wialon(uid, d))
            self._i += 1
        return Batch(out, {}, self.clock)

    @property
    def finished(self) -> bool:
        return self._i >= len(self.rows)

    def close(self) -> None:
        pass
