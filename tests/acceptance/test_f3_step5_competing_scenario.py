"""Independent acceptance checks for the F3 PR-3a scenario driver.

The driver is deliberately imported through its documented entry point.  The
implementation is a stub in this worktree, so the driver-facing tests are
expected to fail with ``NotImplementedError`` until the implementation is
present; the frozen-file and OTel contract checks are independent of it.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest

import scripts.f3_competing_scenario as driver
from opspilot.investigation.loop import investigation_versions
from opspilot.tools.otel_demo import (
    LABEL_VALUES_TOOL,
    METRIC_NAMES_TOOL,
    TOOL_SCHEMA_REVISION,
    TOOL_SCHEMAS,
    TRACE_PROJECTION,
    otel_demo_registrations,
    otel_demo_versions,
    promql_problem,
)
from opspilot.tools.outcomes import PROJECTION_REVISION
from opspilot.tools.registry import ToolRegistry, canonical_hash

ROOT = Path(__file__).resolve().parents[2]
RUBRIC_SHA256 = "b58974b2c2a5ba309cb0efcf47c5af001d9f9c3a27440fec32d5d31b5cf1d956"
ANSWER_SHA256 = "a35361da83e8a314a2af54bf50aa74441ca16dcd7a533e96c22c85a71b14b37a"
# #307 A's two discovery tools after merge main 6ff9d68; recomputed from the
# registered model-visible content and frozen here for F3 step 5 evidence.
# F3 PR-2 (#311 + #310) contract change: #311 moves the revision again (source_error, metrics limits text,
# projection v10). The new value is not pasted (the independent test author
# does not read the implementation); this is now the previous freeze and the
# live value must differ from it and equal the recomputation below.
PR2_PREVIOUS_TOOL_SCHEMA_REVISION = "otel-demo-2a944edd110a"
# #310 (same PR) moves prompt_revision; compared for inequality only.
PR2_PREVIOUS_PROMPT_REVISION = "prompt-replay-candidate-dd9018031487"
# PR-3a's pre-#307 A freeze, retained as the incompatibility boundary.
PR3A_TOOL_SCHEMA_REVISION = "otel-demo-c7737da709ee"
OLD_TOOL_SCHEMA_REVISION = "otel-demo-4d7168180749"
EXPECTED_TRACES_DESCRIPTION_SHA256 = (
    "559e4ed81e5c0f8624b5fd96f8434e164ce403d22545381253223bec893b55c3"
)
EXPECTED_SCENARIOS = {
    "eval-cart-failure",
    "eval-normal-1",
    "eval-normal-2",
    "eval-payment-failure-1",
    "eval-payment-failure-2",
    "eval-product-catalog-failure",
}
KSM_SERIES = {
    "kube_deployment_status_replicas_available",
    "kube_deployment_status_replicas_ready",
    "kube_replicaset_owner",
    "kube_pod_owner",
    "kube_pod_container_info",
}
KSM_LABELS = {"namespace", "deployment", "replicaset", "pod", "owner_name"}


def _schema(name: str) -> dict[str, Any]:
    (schema,) = [item for item in TOOL_SCHEMAS if item["function"]["name"] == name]
    return dict(schema)


def _injected_answer_strings(answer: dict[str, Any]) -> set[str]:
    injected = answer["injected"]
    values = {
        injected["service"],
        *injected["service_aliases"],
        injected["operation"],
        *injected["operation_aliases"],
        injected["flag"],
        injected["normal_variant"],
        injected["variant"],
    }
    return {value.casefold() for value in values}


def test_tool_schema_revision_is_recomputed_and_other_versions_are_unchanged() -> None:
    """Assertion 1: only the model-visible OTel schema revision moves."""

    registrations = otel_demo_registrations()
    recomputed = (
        "otel-demo-"
        + canonical_hash(
            {
                "tool_schemas": [dict(item) for item in TOOL_SCHEMAS],
                "registry": ToolRegistry(registrations.tools).revision,
                "trace_projection": TRACE_PROJECTION,
                "projection_revision": PROJECTION_REVISION,
            }
        )[:12]
    )

    assert TOOL_SCHEMA_REVISION != OLD_TOOL_SCHEMA_REVISION
    assert TOOL_SCHEMA_REVISION != PR3A_TOOL_SCHEMA_REVISION
    assert TOOL_SCHEMA_REVISION == recomputed
    assert TOOL_SCHEMA_REVISION != PR2_PREVIOUS_TOOL_SCHEMA_REVISION
    assert otel_demo_versions() == {
        **investigation_versions(),
        "tool_schema_revision": TOOL_SCHEMA_REVISION,
    }
    assert investigation_versions()["prompt_revision"] != (PR2_PREVIOUS_PROMPT_REVISION)
    assert investigation_versions()["context_policy_revision"] == (
        "ctx-ctx-policy-v1-a30e65ea52f4"
    )
    assert PROJECTION_REVISION == "m1-01-tool-view-v10"


def test_metrics_description_has_only_the_frozen_ksm_surface_and_traces_are_unchanged() -> (
    None
):
    """Assertion 2: D3's KSM names/labels are visible and trace text is byte-stable."""

    metrics_description = _schema("metrics_range_query")["function"]["description"]
    traces_description = _schema("traces_search")["function"]["description"]
    lowered = metrics_description.casefold()

    assert all(token in metrics_description for token in KSM_SERIES)
    assert all(token in metrics_description for token in KSM_LABELS)
    assert "not service_name" in lowered
    assert "image digest" in lowered
    assert "new replicaset" in lowered
    assert "kube_pod_owner" in lowered and "owner_name" in lowered
    assert "kube_replicaset_created" not in lowered
    assert "kube_deployment_metadata_generation" not in lowered
    assert hashlib.sha256(traces_description.encode()).hexdigest() == (
        EXPECTED_TRACES_DESCRIPTION_SHA256
    )


def test_registry_remains_read_only_and_ksm_promql_is_allowed() -> None:
    """Assertion 3: no write verb, connector, or credential is added."""

    registrations = otel_demo_registrations()
    assert {registration.name for registration in registrations.tools} == {
        "metrics_range_query",
        "traces_search",
        METRIC_NAMES_TOOL,
        LABEL_VALUES_TOOL,
        "read_evidence",  # #308 PR2
    }
    assert {
        registration.name: registration.verb for registration in registrations.tools
    } == {
        "metrics_range_query": "query",
        "traces_search": "query",
        METRIC_NAMES_TOOL: "list",
        LABEL_VALUES_TOOL: "list",
        "read_evidence": "get",  # #308 PR2 (contract R3)
    }
    assert all(
        not registration.may_contain_secrets for registration in registrations.tools
    )
    assert len(registrations.targets) == 1
    assert registrations.targets[0].source == "otel-demo"
    assert (
        promql_problem(
            'kube_replicaset_owner{namespace="otel-demo",owner_name="checkout"}',
            3600,
        )
        is None
    )


def test_frozen_answer_is_isolated_and_scenario_collection_is_unchanged() -> None:
    """Assertions 4 and 5: evaluator material stays out of model stimuli."""

    stimulus_path = ROOT / "evals/f3/competing-cause/stimulus.json"
    answer_path = ROOT / "evals/f3/competing-cause/answer.json"
    stimulus = json.loads(stimulus_path.read_text())
    answer = json.loads(answer_path.read_text())
    template = stimulus["follow_up_template"]
    rendered = template.format(
        released_at="2026-10-22T12:34:56Z", release_id="rel-20261022123456"
    )
    visible = json.dumps(stimulus, ensure_ascii=False) + rendered
    visible_folded = visible.casefold()

    assert not any(
        token in visible_folded for token in _injected_answer_strings(answer)
    )
    assert all(
        word not in visible_folded
        for word in ("payment", "charge", "f3", "unrelated", "decoy")
    )
    assert (
        hashlib.sha256(
            (ROOT / "docs/testing/f3-competing-cause-rubric.md").read_bytes()
        ).hexdigest()
        == RUBRIC_SHA256
    )
    assert hashlib.sha256(answer_path.read_bytes()).hexdigest() == ANSWER_SHA256
    assert {
        path.name for path in (ROOT / "evals/scenarios").iterdir() if path.is_dir()
    } == EXPECTED_SCENARIOS
    for scenario in EXPECTED_SCENARIOS:
        assert {
            path.name for path in (ROOT / "evals/scenarios" / scenario).iterdir()
        } == {
            "answer.json",
            "scenario.json",
        }


class _NoCall:
    def __init__(self) -> None:
        self.called = False

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.called = True
        raise AssertionError(f"external call in dry-run: {args!r} {kwargs!r}")


@pytest.mark.parametrize(
    "argv",
    [
        ["--dry-run", "plan", "--flag-method", "Int"],
        ["--dry-run", "preflight"],
        [
            "--dry-run",
            "run",
            "--run",
            "dry-run",
            "--web",
            "http://127.0.0.1:9999",
            "--flagd-url",
            "http://127.0.0.1:8013",
            "--flag-method",
            "Int",
        ],
    ],
)
def test_dry_run_has_no_ports_external_calls_or_writes(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Assertions 6 and 7: dry-run is offline and leaves no lab ledger."""

    forbidden_subprocess = _NoCall()
    forbidden_http = _NoCall()
    factory_called = False

    def forbidden_factory(_: dict[str, str]) -> Any:
        nonlocal factory_called
        factory_called = True
        raise AssertionError("ports_factory must not be built by dry-run")

    monkeypatch.setattr(driver.subprocess, "run", forbidden_subprocess)
    monkeypatch.setattr(driver.urllib.request, "urlopen", forbidden_http)
    monkeypatch.setattr(driver.harness, "LEDGERS", tmp_path / "ledgers")
    out = io.StringIO()
    assert driver.main(argv, stdout=out, ports_factory=forbidden_factory) == 0
    assert not factory_called
    assert not forbidden_subprocess.called
    assert not forbidden_http.called
    assert not (tmp_path / "ledgers").exists()
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines
    if argv[-1] == "Int" and "run" not in argv:
        assert lines[0]["command"] == "plan"
    if "preflight" in argv:
        assert lines[0]["ok"] is None
        assert {check["status"] for check in lines[0]["checks"]} == {"skipped"}
    if "run" in argv:
        assert {line["event"] for line in lines} >= {
            "plan",
            "preflight",
            "action",
        }
        assert all(line["skipped"] for line in lines if line["event"] == "action")


def test_plan_output_matches_frozen_schedule() -> None:
    """Assertion 6: plan output binds checkout, the fault, and safe timing."""

    out = io.StringIO()
    assert driver.main(["plan", "--flag-method", "Int"], stdout=out) == 0
    plan = json.loads(out.getvalue())
    assert plan["deployment"]["namespace"] == "otel-demo"
    assert plan["deployment"]["deployment"] == "checkout"
    assert plan["fault"] == {
        **plan["fault"],
        "flag": "paymentFailure",
        "normal_variant": "off",
        "variant": "100%",
    }
    assert plan["fault"]["offset_seconds"] >= 300
    assert plan["fault"]["offset_seconds"] + plan["fault"]["duration_seconds"] <= 7200
    assert plan["timing"]["rollout_to_onset_ok"] is True
    assert plan["timing"]["release_in_alert_window"] is True
    assert plan["timing"]["schedule_within_harness_limit"] is True
    assert plan["follow_up"]["fields"] == [
        "action",
        "expected_generation",
        "idempotency_key",
        "text",
    ]
    assert "checkout" in out.getvalue()
    assert "paymentFailure" in out.getvalue()


def test_invalid_run_name_has_configuration_exit_and_reason() -> None:
    """The documented configuration refusal is observable and safe to parse."""

    out = io.StringIO()
    assert (
        driver.main(
            [
                "run",
                "--run",
                "contains!bang",
                "--web",
                "http://127.0.0.1:9999",
                "--flagd-url",
                "http://127.0.0.1:8013",
                "--flag-method",
                "Int",
            ],
            stdout=out,
        )
        == 2
    )
    assert "CONFIG_RUN_NAME_INVALID" in out.getvalue()


class _PreflightLab:
    def __init__(self, failed: str | None, root: Path) -> None:
        self.failed = failed
        self.root = root

    def ledger_root(self) -> Path:
        return self.root

    def check_reservation(self, holder: str, batch: str) -> None:
        if self.failed == "lab_reserved":
            raise ValueError("LAB_NOT_RESERVED")

    def fault_active(self) -> bool:
        if self.failed == "no_active_fault":
            return True
        if self.failed == "unverified":
            raise RuntimeError("fault state unreadable")
        return False

    def flag_defaults(self) -> dict[str, str]:
        return {"paymentFailure": "on" if self.failed == "flags_off" else "off"}

    def firing_alert_count(self, alertname: str) -> int:
        return 1 if self.failed == "alert_not_firing" else 0

    def recent_rollouts(self, since: datetime) -> list[str]:
        return ["checkout"] if self.failed == "no_recent_rollout" else []

    def swap_usage(self) -> driver.SwapUsage:
        return driver.SwapUsage(
            total_mib=4096, used_mib=4096 if self.failed == "swap_headroom" else 0
        )

    @contextmanager
    def reserved(self, holder: str, batch: str):
        yield

    def publish(self, ledger: Path, release_id: str) -> dict[str, str]:
        raise AssertionError("preflight must not publish")

    def scheduled_fault(self, **kwargs: Any) -> None:
        raise AssertionError("preflight must not schedule a fault")


class _PreflightStore:
    def __init__(self, failed: str | None) -> None:
        self.failed = failed

    def alert_incidents(self, alertname: str, namespace: str, workload: str):
        if self.failed == "no_open_alert_incident":
            return [
                driver.AlertIncident(
                    incident_id="incident",
                    run_id=None,
                    lifecycle="open",
                    control_generation=0,
                    run_state=None,
                    step_count=0,
                    alertname=alertname,
                    created_at=datetime(2026, 10, 22, tzinfo=UTC),
                )
            ]
        return []

    def release_observations_since(self, since: datetime) -> int:
        return 1 if self.failed == "no_recent_rollout" else 0


class _PreflightHttp:
    def post_form(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
        raise AssertionError("preflight must not send HTTP")


class _PreflightClock:
    def now(self) -> datetime:
        return datetime(2026, 10, 22, tzinfo=UTC)

    def sleep(self, seconds: float) -> None:
        raise AssertionError("preflight must not sleep")


@pytest.mark.parametrize(
    ("failed", "reason"),
    [
        ("lab_reserved", "PRECONDITION_LAB_NOT_RESERVED"),
        ("no_active_fault", "PRECONDITION_FAULT_ACTIVE"),
        ("flags_off", "PRECONDITION_FLAGS_NOT_OFF"),
        ("alert_not_firing", "PRECONDITION_ALERT_FIRING"),
        ("no_open_alert_incident", "PRECONDITION_OPEN_ALERT_INCIDENT"),
        ("no_recent_rollout", "PRECONDITION_RECENT_ROLLOUT"),
        ("swap_headroom", "PRECONDITION_SWAP_LOW"),
    ],
)
def test_each_precondition_has_its_own_reason_code(
    failed: str, reason: str, tmp_path: Path
) -> None:
    """Assertion 10: each R-6 failure refuses before any run action."""

    ports = driver.Ports(
        lab=_PreflightLab(failed, tmp_path),
        store=_PreflightStore(failed),
        http=_PreflightHttp(),
        clock=_PreflightClock(),
    )
    out = io.StringIO()
    assert (
        driver.main(
            ["--holder", "holder", "--batch", "batch", "preflight"],
            ports=ports,
            stdout=out,
        )
        == 3
    )
    report = json.loads(out.getvalue())
    assert report["ok"] is False
    assert report["reason"] == reason
    failed_checks = [check for check in report["checks"] if check["status"] == "fail"]
    assert len(failed_checks) == 1
    assert failed_checks[0]["reason"] == reason


def test_port_failure_is_unverified_and_refuses(tmp_path: Path) -> None:
    """R-6 port errors are fail-closed with the documented unverified code."""

    ports = driver.Ports(
        lab=_PreflightLab("unverified", tmp_path),
        store=_PreflightStore(None),
        http=_PreflightHttp(),
        clock=_PreflightClock(),
    )
    out = io.StringIO()
    assert (
        driver.main(
            ["--holder", "holder", "--batch", "batch", "preflight"],
            ports=ports,
            stdout=out,
        )
        == 3
    )
    report = json.loads(out.getvalue())
    check = next(item for item in report["checks"] if item["id"] == "no_active_fault")
    assert check["status"] == "unverified"
    assert check["reason"] == "PRECONDITION_UNVERIFIED"


def test_driver_does_not_call_forbidden_harness_actions() -> None:
    """Assertion 9: register/webhook are forbidden even as direct calls."""

    tree = ast.parse(Path(driver.__file__).read_text())
    forbidden_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in {"register", "webhook"}:
            forbidden_calls.append((node.lineno, node.func.attr))
    assert forbidden_calls == []


class _RunLab(_PreflightLab):
    def __init__(self, root: Path, clock: "_RunClock", store: "_RunStore") -> None:
        super().__init__(None, root)
        self.clock = clock
        self.store = store
        self.published: list[tuple[Path, str]] = []

    def publish(self, ledger: Path, release_id: str) -> dict[str, str]:
        self.published.append((ledger, release_id))
        return {"released_at": self.clock.now().isoformat().replace("+00:00", "Z")}

    def scheduled_fault(
        self,
        ledger: Path,
        *,
        offset: int,
        duration: int,
        flag: str,
        variant: str,
        normal: str,
        url: str,
        method: str,
    ) -> None:
        self.store.expose_incident()


class _RunStore(_PreflightStore):
    def __init__(self, incident: driver.AlertIncident) -> None:
        super().__init__(None)
        self.incident = incident
        self.visible = False

    def expose_incident(self) -> None:
        self.visible = True

    def alert_incidents(self, alertname: str, namespace: str, workload: str):
        return [self.incident] if self.visible else []


class _RunHttp:
    def __init__(self) -> None:
        self.requests: list[tuple[str, bytes, dict[str, str], float]] = []

    def post_form(self, url: str, body: bytes, headers: dict[str, str], timeout: float):
        self.requests.append((url, body, headers, timeout))
        return 200, b'{"generation": 1}'


class _RunClock(_PreflightClock):
    pass


def test_live_driver_form_and_ledger_keep_answer_and_credentials_outside_payload(
    tmp_path: Path,
) -> None:
    """Assertions 4 and 9: captured follow-up body is neutral and auditable."""

    clock = _RunClock()
    incident = driver.AlertIncident(
        incident_id="incident-1",
        run_id="run-1",
        lifecycle="open",
        control_generation=0,
        run_state="queued",
        step_count=0,
        alertname="CheckoutPlaceOrderErrorRatioHigh",
        created_at=clock.now(),
    )
    store = _RunStore(incident)
    lab = _RunLab(tmp_path / "ledgers", clock, store)
    http = _RunHttp()
    ports = driver.Ports(
        lab=lab,
        store=store,
        http=http,
        clock=clock,
        env={"F3_LAB_UI_USER": "ui-user", "F3_LAB_UI_PASSWORD": "ui-secret"},
    )
    out = io.StringIO()
    assert (
        driver.main(
            [
                "--holder",
                "holder",
                "--batch",
                "batch",
                "run",
                "--run",
                "run-1",
                "--web",
                "http://127.0.0.1:9999",
                "--flagd-url",
                "http://127.0.0.1:8013",
                "--flag-method",
                "Int",
            ],
            ports=ports,
            stdout=out,
        )
        == 0
    )
    assert len(http.requests) == 1
    url, body, headers, _ = http.requests[0]
    assert url.endswith("/incidents/incident-1/control")
    fields = parse_qs(body.decode(), strict_parsing=True)
    assert fields["action"] == ["follow_up"]
    assert fields["expected_generation"] == ["0"]
    assert fields["idempotency_key"][0].startswith("note-rel-")
    answer = json.loads((ROOT / "evals/f3/competing-cause/answer.json").read_text())
    body_folded = body.decode().casefold()
    assert not any(token in body_folded for token in _injected_answer_strings(answer))
    assert all(
        word not in body_folded
        for word in ("payment", "charge", "f3", "unrelated", "decoy")
    )
    assert "authorization" in {key.casefold() for key in headers}
    assert "ui-secret" not in body_folded

    ledger_files = list((tmp_path / "ledgers").rglob("*"))
    ledger_text = "\n".join(path.read_text() for path in ledger_files if path.is_file())
    assert "idempotency_label" in ledger_text
    assert "Authorization" not in ledger_text
    assert "ui-secret" not in ledger_text
    assert "ui-user" not in ledger_text
    assert "ui-secret" not in out.getvalue()
    events = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [event["event"] for event in events][-1] == "finished"
    assert {event["event"] for event in events} >= {
        "plan",
        "preflight",
        "released",
        "incident",
        "follow_up",
        "start_worker",
        "finished",
    }
    follow_up_event = next(event for event in events if event["event"] == "follow_up")
    assert set(follow_up_event) >= {
        "status",
        "generation",
        "request_sha256",
    }
