"""Independent, offline acceptance checks for issue #414 (F8 step 1 PR-2).

These tests inspect the lab manifests and the frozen drill summary only.  They
do not start a cluster, contact Prometheus, or invoke the implementation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
VALUES = ROOT / "scripts/kind_lab/values.yaml"
ALERTMANAGER_VALUES = ROOT / "scripts/kind_lab/alertmanager-values.yaml"
DRILL_EGRESS = ROOT / "deploy/compose/compose.drill-egress.yaml"
EVIDENCE = ROOT / "docs/evidence/f8-step1-drills/summary.json"

EXPECTED_TARGETS = {
    "192.168.5.2:19464": "web",
    "192.168.5.2:19465": "worker",
    "192.168.5.2:19466": "observer",
}
EXPECTED_RULES = {
    "OpsPilotAgentAbsent": {
        "expr": 'up{job="opspilot"} == 0 or absent(up{job="opspilot",role="worker"})',
        "for": "2m",
    },
    "OpsPilotModelCallsFailing": {
        "expr": "opspilot_model_consecutive_failures >= 2",
        "for": "1m",
    },
}
EXPECTED_SELF_LABELS = {"severity": "critical", "opspilot_self": "true"}
EXPECTED_EXISTING_EXPR = """label_replace(label_replace(
  (sum by (k8s_namespace_name, service_name) (rate(traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_SERVER",span_name=~".*CheckoutService/PlaceOrder",status_code="STATUS_CODE_ERROR"}[5m])))
  /
  (sum by (k8s_namespace_name, service_name) (rate(traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_SERVER",span_name=~".*CheckoutService/PlaceOrder"}[5m])))
, "namespace", "$1", "k8s_namespace_name", "(.*)"), "service", "$1", "service_name", "(.*)")
> 0.5"""


def _yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def _scrape_jobs(values: dict[str, Any]) -> list[dict[str, Any]]:
    raw = values["prometheus"]["extraScrapeConfigs"]
    if isinstance(raw, str):
        parsed = yaml.safe_load(raw)
    else:
        parsed = raw
    if isinstance(parsed, dict):
        parsed = parsed.get("scrape_configs", [parsed])
    assert isinstance(parsed, list)
    return parsed


def _routes(config: dict[str, Any]) -> list[dict[str, Any]]:
    route = config["route"]
    return list(route.get("routes", []))


def _route_matches_self(route: dict[str, Any]) -> bool:
    match = route.get("match", {})
    if match.get("opspilot_self") == "true":
        return True
    match_re = route.get("match_re", {})
    if match_re.get("opspilot_self") == "true":
        return True
    for matcher in route.get("matchers", []):
        if isinstance(matcher, str) and matcher.replace(" ", "") in {
            'opspilot_self="true"',
            "opspilot_self=true",
        }:
            return True
    return False


def test_opspilot_scrape_job_has_exact_lab_targets_roles_and_interval() -> None:
    jobs = [
        job for job in _scrape_jobs(_yaml(VALUES)) if job.get("job_name") == "opspilot"
    ]
    assert len(jobs) == 1
    job = jobs[0]
    assert job["scrape_interval"] == "15s"

    targets: dict[str, str] = {}
    for static in job["static_configs"]:
        labels = static.get("labels", {})
        assert set(labels) == {"role"}
        for target in static["targets"]:
            assert target not in targets
            targets[target] = labels["role"]
    assert targets == EXPECTED_TARGETS


def test_self_alert_rules_are_exact_and_existing_lab_rule_remains() -> None:
    values = _yaml(VALUES)
    groups = values["prometheus"]["serverFiles"]["alerting_rules.yml"]["groups"]
    rules = [rule for group in groups for rule in group["rules"]]
    by_name = {rule["alert"]: rule for rule in rules}

    assert set(EXPECTED_RULES) <= set(by_name)
    for name, expected in EXPECTED_RULES.items():
        rule = by_name[name]
        assert rule["expr"].strip() == expected["expr"]
        assert rule["for"] == expected["for"]
        assert rule["labels"] == EXPECTED_SELF_LABELS

    # M1-04's lab rule is retained verbatim in its public contract fields.
    existing = by_name["CheckoutPlaceOrderErrorRatioHigh"]
    assert existing["for"] == "2m"
    assert existing["labels"] == {
        "severity": "critical",
        "cluster": "opspilot-m1",
        "opspilot_integration_id": "m0-otel-20260909",
    }
    assert existing["annotations"] == {
        "summary": "checkout PlaceOrder error ratio above 50%",
        "description": "{{ $value | humanizePercentage }} of checkout PlaceOrder server spans ended in error over the last 5 minutes.",
    }
    assert " ".join(existing["expr"].split()) == " ".join(
        EXPECTED_EXISTING_EXPR.split()
    )


def test_self_alerts_route_to_empty_receiver_without_any_notifier_or_token() -> None:
    config = _yaml(ALERTMANAGER_VALUES)["config"]
    receivers = {receiver["name"]: receiver for receiver in config["receivers"]}
    assert "opspilot-self" in receivers
    assert receivers["opspilot-self"] == {"name": "opspilot-self"}

    self_routes = [route for route in _routes(config) if _route_matches_self(route)]
    assert len(self_routes) == 1
    assert self_routes[0]["receiver"] == "opspilot-self"

    # The only pre-existing outbound receiver is the M1-04 workbench hook.
    # Any newly introduced receiver must remain an empty sink.
    assert set(receivers) <= {"opspilot", "opspilot-self"}
    for key, value in receivers["opspilot-self"].items():
        assert key == "name"
        assert not re.search(
            r"token|secret|password|credential|webhook|slack|email", str(value), re.I
        )


def test_drill_egress_override_changes_only_worker_extra_hosts() -> None:
    assert DRILL_EGRESS.exists()
    assert _yaml(DRILL_EGRESS) == {
        "services": {
            "worker": {
                "extra_hosts": ["api.deepseek.com:127.0.0.1"],
            }
        }
    }


def _assert_no_secret_looking_values(value: Any, path: str = "summary") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            assert not re.search(
                r"token|secret|password|credential|authorization|api[_-]?key", key, re.I
            ), f"secret-bearing key at {path}.{key}"
            _assert_no_secret_looking_values(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_secret_looking_values(child, f"{path}[{index}]")
    elif isinstance(value, str):
        assert not re.search(
            r"(?:sk-|bearer\s+|gh[ps]_|AKIA[0-9A-Z]{12,})", value, re.I
        ), path


def test_drill_summary_records_both_contractual_drills_when_present() -> None:
    if not EVIDENCE.exists():
        pytest.skip("F8 drill evidence summary is produced by the opt-in lab drills")
    summary = json.loads(EVIDENCE.read_text())
    _assert_no_secret_looking_values(summary)
    assert summary["promtool"]["rules_ok"] is True
    assert "recorded_at" in summary
    drills = {drill["name"]: drill for drill in summary["drills"]}
    assert set(drills) == {"stop_worker", "model_egress"}
    for drill in drills.values():
        assert {"name", "started_at", "ended_at", "alert", "run", "metrics"} <= set(
            drill
        )
        assert {"name", "firing_observed_at", "receiver"} <= set(drill["alert"])
        assert {"run_id", "final_state", "handoff_reason"} <= set(drill["run"])
        assert isinstance(drill["metrics"], dict)
        assert drill["alert"]["receiver"] == "opspilot-self"

    stopped = drills["stop_worker"]
    stopped_run = stopped["run"]
    assert stopped_run["handoff_reason"] == "DEADLINE_EXCEEDED" or stopped_run.get(
        "worker_return_sweep"
    )

    egress = drills["model_egress"]
    assert egress["run"]["final_state"] == "waiting_human"
    assert egress["run"]["handoff_reason"] == "MODEL_UNAVAILABLE"
