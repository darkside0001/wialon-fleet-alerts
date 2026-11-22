"""Command line entry point: ``fleet-alerts <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import __version__
from .config import ConfigError, Settings, load_rules
from .db import Alert, Unit, make_sessionmaker
from .models import Message
from .sources import TraceSource, WialonSource, read_trace
from .store import Store

log = logging.getLogger("fleet_alerts")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs URLs; the Telegram URL holds the token


# -- check-config -----------------------------------------------------------
def cmd_check_config(args: argparse.Namespace) -> int:
    cfg = load_rules(args.rules, expand_env=not args.no_env)
    e = cfg.engine
    print(f"OK  {args.rules}")
    print(f"    timezone {cfg.timezone}, cooldown {cfg.cooldown_s // 60} min")
    print(f"    {len(e.rules)} rules: " + ", ".join(f"{r.id} ({r.kind})" for r in e.rules))
    print(
        f"    {len(e.zones)} zones, {len(e.units)} configured units, "
        f"{sum(1 for u in e.units.values() if u.fuel)} with fuel calibration"
    )
    print(
        f"    {len(cfg.notifiers)} notifiers: "
        + ", ".join(f"{n.name} ({n.type}, >= {n.min_severity})" for n in cfg.notifiers)
    )
    return 0


# -- replay -------------------------------------------------------------------
def replay(trace: str | Path, rules: str | Path, tail_s: int = 0, tick_s: int = 60) -> list[dict]:
    """Run a recorded trace through the rules offline. Nothing is sent anywhere."""
    cfg = load_rules(rules, expand_env=False)
    engine = cfg.engine
    sessions = make_sessionmaker("sqlite://")
    store = Store(sessions, [], cfg.cooldown_s, engine)
    rows = sorted(read_trace(trace), key=lambda r: r[2]["t"])
    for uid, nm, _ in rows:
        if nm:
            engine.unit(uid, nm)
    next_tick = None
    for uid, _, d in rows:
        msg = Message.from_wialon(uid, d)
        if next_tick is None:
            next_tick = msg.t
        while next_tick <= msg.t:
            store.process(events=engine.tick(next_tick))
            next_tick += tick_s
        store.process([msg], engine.on_message(msg))
    if rows and next_tick is not None:
        end = rows[-1][2]["t"] + tail_s
        while next_tick <= end:
            store.process(events=engine.tick(next_tick))
            next_tick += tick_s
    with sessions() as s:
        out = []
        for a, name in s.execute(
            select(Alert, Unit.name)
            .outerjoin(Unit, Unit.id == Alert.unit_id)
            .order_by(Alert.opened_at, Alert.id)
        ):
            out.append(
                {
                    "id": a.id,
                    "opened_at": a.opened_at,
                    "resolved_at": a.resolved_at,
                    "unit": name,
                    "unit_id": a.unit_id,
                    "rule": a.rule_id,
                    "severity": a.severity,
                    "status": a.status,
                    "title": a.title,
                    "details": a.details,
                }
            )
    return out


def _fmt(t: int | None, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d %H:%M") if t else "-"


def cmd_replay(args: argparse.Namespace) -> int:
    alerts = replay(args.trace, args.rules, tail_s=int(args.tail_hours * 3600))
    tz = ZoneInfo(load_rules(args.rules, expand_env=False).timezone)
    if args.format == "json":
        print(json.dumps(alerts, indent=2, ensure_ascii=False))
        return 0
    header = ["Opened", "Resolved", "Unit", "Rule", "Severity", "Alert"]
    table = [
        [
            _fmt(a["opened_at"], tz),
            _fmt(a["resolved_at"], tz),
            a["unit"] or str(a["unit_id"]),
            a["rule"],
            a["severity"],
            a["title"],
        ]
        for a in alerts
    ]
    if args.format == "md":
        print("| " + " | ".join(header) + " |")
        print("|" + "|".join("---" for _ in header) + "|")
        for row in table:
            print("| " + " | ".join(c.replace("|", "/") for c in row) + " |")
    else:
        widths = [max(len(h), *(len(r[i]) for r in table)) if table else len(h) for i, h in enumerate(header)]
        print("  ".join(h.ljust(w) for h, w in zip(header, widths, strict=True)))
        print("  ".join("-" * w for w in widths))
        for row in table:
            print("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)))
    print(f"\n{len(alerts)} alerts from {args.trace}", file=sys.stderr)
    return 0


# -- worker / api ---------------------------------------------------------------
def build_worker(settings: Settings, demo_trace: str | None = None, speedup: float = 600):
    from .notify.dispatcher import Dispatcher, build_channels
    from .wialon import WialonClient
    from .worker import Worker

    cfg = load_rules(settings.rules_file)
    sessions = make_sessionmaker(settings.database_url)
    store = Store(sessions, cfg.notifiers, cfg.cooldown_s, cfg.engine)
    channels = build_channels(cfg.notifiers, ZoneInfo(cfg.timezone))
    if demo_trace:
        source = TraceSource(demo_trace, speedup=speedup, poll_interval_s=settings.poll_interval_s)
    else:
        token = os.environ.get("WIALON_TOKEN", "").strip()
        if not token:
            raise ConfigError("WIALON_TOKEN is not set (or run with --demo samples/demo-trace.jsonl)")
        source = WialonSource(WialonClient(settings.wialon_host, token))
    return Worker(
        source,
        cfg.engine,
        store,
        Dispatcher(sessions, channels),
        poll_interval_s=settings.poll_interval_s,
        tick_interval_s=settings.tick_interval_s,
    )


def cmd_worker(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if args.rules:
        settings.rules_file = args.rules
    worker = build_worker(settings, args.demo, args.speedup)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log.info(
        "wialon-fleet-alerts %s worker starting (%s)", __version__, "demo trace" if args.demo else "Wialon"
    )
    worker.run(stop)
    return 0


def cmd_api(args: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app

    settings = Settings.from_env()
    if args.rules:
        settings.rules_file = args.rules
    cfg = load_rules(settings.rules_file, expand_env=False)
    app = create_app(make_sessionmaker(settings.database_url), settings.api_key, cfg.engine)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", server_header=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fleet-alerts", description="Rule-based alerting for Wialon fleets.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check-config", help="validate a rules file")
    c.add_argument("--rules", default=os.environ.get("RULES_FILE", "samples/rules.yaml"))
    c.add_argument("--no-env", action="store_true", help="do not require ${ENV} references to be set")
    c.set_defaults(fn=cmd_check_config)

    r = sub.add_parser("replay", help="run a recorded trace through the rules and print the alerts")
    r.add_argument("trace")
    r.add_argument("--rules", default="samples/rules.yaml")
    r.add_argument("--format", choices=("table", "md", "json"), default="table")
    r.add_argument(
        "--tail-hours", type=float, default=0.0, help="keep the clock running after the trace ends"
    )
    r.set_defaults(fn=cmd_replay)

    w = sub.add_parser("worker", help="run the alerting worker")
    w.add_argument("--rules")
    w.add_argument("--demo", metavar="TRACE", help="play a recorded trace instead of connecting to Wialon")
    w.add_argument("--speedup", type=float, default=600, help="demo clock speed (default 600x)")
    w.set_defaults(fn=cmd_worker)

    a = sub.add_parser("api", help="run the HTTP API")
    a.add_argument("--rules")
    a.add_argument("--host", default="127.0.0.1")
    a.add_argument("--port", type=int, default=8080)
    a.set_defaults(fn=cmd_api)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.fn(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
