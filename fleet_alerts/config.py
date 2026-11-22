"""Configuration.

Two sources, on purpose:

* **environment variables** for secrets and deployment settings (tokens,
  database URL, API key). They are never written to the rules file or logs.
* a **rules file** (YAML) for everything an operator tunes: rules, zones,
  tank calibrations, where alerts go. It is safe to keep in git; secrets are
  referenced as ``${ENV_NAME}``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .fuel import Calibration
from .geo import Zone
from .models import SEVERITIES
from .rules import RULE_TYPES, Engine, UnitInfo

_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)\}")


class ConfigError(ValueError):
    pass


def _expand(value: Any) -> Any:
    if isinstance(value, str):

        def sub(m: re.Match) -> str:
            name = m.group(1)
            if name not in os.environ:
                raise ConfigError(f"environment variable {name} is referenced in the rules file but not set")
            return os.environ[name]

        return _ENV_REF.sub(sub, value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


@dataclass
class NotifierConfig:
    type: str  # "telegram" | "webhook" | "log"
    name: str
    min_severity: str = "warning"
    groups: tuple[str, ...] = ()
    notify_resolve: bool = True
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class RulesConfig:
    engine: Engine
    notifiers: list[NotifierConfig]
    cooldown_s: int
    timezone: str


def load_rules(path: str | Path, *, expand_env: bool = True) -> RulesConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read rules file {path}: {exc}") from None
    return parse_rules(raw, expand_env=expand_env)


def parse_rules(raw: dict[str, Any], *, expand_env: bool = True) -> RulesConfig:
    try:
        timezone = raw.get("timezone", "UTC")
        zones = {name: Zone.from_config(name, z) for name, z in (raw.get("zones") or {}).items()}

        units: dict[int, UnitInfo] = {}
        for uid, u in (raw.get("units") or {}).items():
            u = u or {}
            fuel = u.get("fuel")
            units[int(uid)] = UnitInfo(
                id=int(uid),
                groups=tuple(u.get("groups", [])),
                fuel_param=fuel["param"] if fuel else None,
                fuel=Calibration([tuple(p) for p in fuel["table"]]) if fuel else None,
            )

        rules = []
        for r in raw.get("rules") or []:
            kind = r.get("type")
            if kind not in RULE_TYPES:
                raise ConfigError(f"rule {r.get('id')}: unknown type {kind!r}; known: {sorted(RULE_TYPES)}")
            rules.append(RULE_TYPES[kind](r))
        if not rules:
            raise ConfigError("the rules file defines no rules")
        engine = Engine(rules, units, zones, timezone)

        notifiers = []
        for i, n in enumerate(raw.get("notifiers") or []):
            n = _expand(n) if expand_env else n
            kind = n.get("type")
            if kind not in ("telegram", "webhook", "log"):
                raise ConfigError(f"notifier #{i}: unknown type {kind!r}")
            sev = n.get("min_severity", "warning")
            if sev not in SEVERITIES:
                raise ConfigError(f"notifier #{i}: min_severity must be one of {SEVERITIES}")
            opts = {
                k: v
                for k, v in n.items()
                if k not in ("type", "name", "min_severity", "groups", "notify_resolve")
            }
            notifiers.append(
                NotifierConfig(
                    kind,
                    n.get("name", f"{kind}-{i}"),
                    sev,
                    tuple(n.get("groups", [])),
                    bool(n.get("notify_resolve", True)),
                    opts,
                )
            )
        cooldown = int(float(raw.get("cooldown_minutes", 30)) * 60)
    except ConfigError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"invalid rules file: {exc}") from None
    return RulesConfig(engine, notifiers, cooldown, timezone)


@dataclass
class Settings:
    database_url: str = "sqlite:///fleet-alerts.db"
    rules_file: str = "samples/rules.yaml"
    wialon_host: str = "https://hst-api.wialon.com"
    poll_interval_s: float = 5.0
    tick_interval_s: float = 60.0
    api_key: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        e = os.environ
        return cls(
            database_url=e.get("DATABASE_URL", cls.database_url),
            rules_file=e.get("RULES_FILE", cls.rules_file),
            wialon_host=e.get("WIALON_HOST", cls.wialon_host),
            poll_interval_s=float(e.get("POLL_INTERVAL_S", cls.poll_interval_s)),
            tick_interval_s=float(e.get("TICK_INTERVAL_S", cls.tick_interval_s)),
            api_key=e.get("API_KEY", ""),
        )
