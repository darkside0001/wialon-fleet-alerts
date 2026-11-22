from pathlib import Path

import pytest

from fleet_alerts.cli import main, replay
from fleet_alerts.config import ConfigError, load_rules, parse_rules

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "samples" / "rules.yaml"
TRACE = ROOT / "samples" / "demo-trace.jsonl"


def test_sample_rules_are_valid():
    cfg = load_rules(RULES)
    assert {r.kind for r in cfg.engine.rules} == {
        "offline",
        "overspeed",
        "power_loss",
        "gps_lost",
        "fuel_drain",
        "geofence",
    }
    assert cfg.engine.units[202].fuel.litres(4060) == 400


def test_env_references_are_expanded(monkeypatch):
    raw = {
        "rules": [{"id": "o", "type": "offline"}],
        "notifiers": [{"type": "telegram", "token": "${TG_TOKEN}", "chat_id": "${TG_CHAT}"}],
    }
    with pytest.raises(ConfigError, match="TG_TOKEN"):
        parse_rules(raw)
    monkeypatch.setenv("TG_TOKEN", "123:abc")
    monkeypatch.setenv("TG_CHAT", "-100")
    n = parse_rules(raw).notifiers[0]
    assert n.options == {"token": "123:abc", "chat_id": "-100"}


def test_bad_notifier_and_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="unknown type"):
        parse_rules({"rules": [{"id": "o", "type": "offline"}], "notifiers": [{"type": "sms"}]})
    with pytest.raises(ConfigError, match="cannot read"):
        load_rules(tmp_path / "missing.yaml")


def test_replay_of_demo_trace_finds_every_scenario():
    alerts = replay(TRACE, RULES)
    got = {(a["unit_id"], a["rule"]) for a in alerts}
    assert got == {
        (202, "fuel-drain"),
        (201, "overspeed"),
        (205, "gps-lost"),
        (204, "power-cut"),
        (206, "offline"),
        (203, "depot-night-exit"),
    }
    drain = next(a for a in alerts if a["rule"] == "fuel-drain")
    assert 40 <= drain["details"]["litres"] <= 50  # 45 L were drained in the trace
    assert next(a for a in alerts if a["rule"] == "overspeed")["status"] == "resolved"


def test_cli_commands(capsys):
    assert main(["check-config", "--rules", str(RULES)]) == 0
    assert "6 rules" in capsys.readouterr().out
    assert main(["replay", str(TRACE), "--rules", str(RULES), "--format", "md"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("| Opened |") and "fuel-drain" in out


def test_cli_reports_config_errors(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("rules: []\n")
    assert main(["check-config", "--rules", str(bad)]) == 2
    assert "no rules" in capsys.readouterr().err
