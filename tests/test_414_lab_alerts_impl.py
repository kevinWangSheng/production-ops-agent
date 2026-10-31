"""#414 implementer tests: the lab scrape job, the two self-alerts and their
route (F8-R5, F8-R6, F8-Q3), and the drill overrides (F8-R8, F8-Q4).

Rule expressions, labels, targets and ``extra_hosts`` are the contract's
code-block text (docs/tasks/2026-10-27-m3-f8-operability-contract.md).
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from scripts import kind_lab

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path(kind_lab.CONFIG)
COMPOSE_DIR = ROOT / "deploy" / "compose"
PROMETHEUS = yaml.safe_load((CONFIG / "values.yaml").read_text())["prometheus"]
ALERTMANAGER = yaml.safe_load((CONFIG / "alertmanager-values.yaml").read_text())


def _rules() -> dict[str, dict]:
    groups = PROMETHEUS["serverFiles"]["alerting_rules.yml"]["groups"]
    return {rule["alert"]: rule for group in groups for rule in group["rules"]}


def test_scrape_job_reaches_the_three_listeners_through_the_host_alias():
    (job,) = yaml.safe_load(PROMETHEUS["extraScrapeConfigs"])
    assert job == {
        "job_name": "opspilot",
        "scrape_interval": "15s",
        "static_configs": [
            {"targets": ["192.168.5.2:19464"], "labels": {"role": "web"}},
            {"targets": ["192.168.5.2:19465"], "labels": {"role": "worker"}},
            {"targets": ["192.168.5.2:19466"], "labels": {"role": "observer"}},
        ],
    }
    # the ports the lab override publishes, in the same role order
    lab = yaml.safe_load((COMPOSE_DIR / "compose.lab.yaml").read_text())["services"]
    assert [
        lab[role]["ports"][0]["published"] for role in ("web", "worker", "observer")
    ] == [
        "19464",
        "19465",
        "19466",
    ]


def test_the_self_alerts_are_the_contract_text():
    rules = _rules()
    labels = {"severity": "critical", "opspilot_self": "true"}
    absent = rules["OpsPilotAgentAbsent"]
    assert absent["expr"] == (
        'up{job="opspilot"} == 0 or absent(up{job="opspilot",role="worker"})'
    )
    assert absent["for"] == "2m"
    assert absent["labels"] == labels
    failing = rules["OpsPilotModelCallsFailing"]
    assert failing["expr"] == "opspilot_model_consecutive_failures >= 2"
    assert failing["for"] == "1m"
    assert failing["labels"] == labels


def test_the_lab_alert_keeps_going_to_the_workbench():
    rule = _rules()["CheckoutPlaceOrderErrorRatioHigh"]
    assert "opspilot_self" not in rule["labels"]


def test_self_alerts_route_to_an_integration_less_receiver():
    config = ALERTMANAGER["config"]
    receivers = {receiver["name"]: receiver for receiver in config["receivers"]}
    # no webhook, email or any other integration, so no token and no call
    assert receivers["opspilot-self"] == {"name": "opspilot-self"}
    route = config["route"]
    assert route["receiver"] == "opspilot"
    assert route["routes"] == [
        {"receiver": "opspilot-self", "matchers": ['opspilot_self="true"']}
    ]
    # no new outbound notification (PRODUCT-CONSTRAINTS:16): the only
    # integration is still the workbench webhook
    integrations = [
        key
        for receiver in config["receivers"]
        for key in receiver
        if key.endswith("_configs")
    ]
    assert integrations == ["webhook_configs"]


def test_egress_override_only_points_the_model_host_at_the_worker_loopback():
    override = yaml.safe_load((COMPOSE_DIR / "compose.drill-egress.yaml").read_text())
    assert override == {
        "services": {"worker": {"extra_hosts": ["api.deepseek.com:127.0.0.1"]}}
    }
    client = (ROOT / "opspilot/investigation/client.py").read_text()
    assert '"https://api.deepseek.com/v1/chat/completions"' in client


def test_run_wall_override_only_shortens_the_web_run_wall():
    override = yaml.safe_load((COMPOSE_DIR / "compose.drill-run-wall.yaml").read_text())
    assert list(override["services"]) == ["web"]
    environment = override["services"]["web"]["environment"]
    assert list(environment) == ["OPSPILOT_RUN_SECONDS"]
    from opspilot.investigation.limits import RUN_WALL_SECONDS

    assert 0 < float(environment["OPSPILOT_RUN_SECONDS"]) <= RUN_WALL_SECONDS


def test_the_installer_never_uses_a_drill_override():
    install = (COMPOSE_DIR / "install.sh").read_text()
    assert "drill" not in install


class _Proc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _rules_check(monkeypatch, receivers: str, rc: int = 0) -> tuple[int, list]:
    execs: list = []
    data = {
        "alerting_rules.yml": "groups: []",
        "prometheus.yml": "scrape_configs: []",
        "alertmanager.yml": "route: {}",
    }
    monkeypatch.setattr(kind_lab, "capture", lambda cmd, **kw: _Proc(stdout="kind: X"))

    def fake_subprocess_run(cmd, **kw):
        if "create" in cmd:
            return _Proc(stdout=json.dumps({"data": data}))
        execs.append((cmd, kw["input"]))
        out = receivers if "routes test" in cmd[-1] else "SUCCESS"
        return _Proc(returncode=rc, stdout=out)

    monkeypatch.setattr(kind_lab.subprocess, "run", fake_subprocess_run)
    return kind_lab.cmd_rules_check(None), execs


def test_rules_check_runs_each_tool_on_its_rendered_file(monkeypatch):
    code, execs = _rules_check(monkeypatch, "opspilot-self\n")
    assert code == 0
    scripts = [(cmd[-1], content) for cmd, content in execs]
    assert any("promtool check rules" in s and c == "groups: []" for s, c in scripts)
    assert any(
        "promtool check config --syntax-only" in s and c == "scrape_configs: []"
        for s, c in scripts
    )
    assert any("amtool check-config" in s and c == "route: {}" for s, c in scripts)
    assert any("'opspilot_self=\"true\"'" in s for s, _ in scripts)


def test_rules_check_fails_when_a_self_alert_reaches_the_workbench(monkeypatch):
    code, _ = _rules_check(monkeypatch, "opspilot-self,opspilot\n")
    assert code == 1
    code, _ = _rules_check(monkeypatch, "opspilot\n")
    assert code == 1


def test_rules_check_fails_on_a_tool_failure(monkeypatch):
    code, _ = _rules_check(monkeypatch, "opspilot-self\n", rc=1)
    assert code == 1
