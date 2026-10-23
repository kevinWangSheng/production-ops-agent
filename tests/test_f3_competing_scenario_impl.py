"""Implementer self-tests for F3 PR-3a (not the independent acceptance tests).

Contract: docs/tasks/2026-10-22-f3-pr3a-contract.md. Everything external is a
fake; nothing here touches kind, flagd, Prometheus, a database or the network.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from opspilot.tools import otel_demo
from opspilot.tools.registry import ToolRegistry, canonical_hash
from scripts import f3_competing_scenario as driver
from scripts import f11_u2_harness as harness
from scripts import kind_lab

ROOT = Path(__file__).resolve().parents[1]
OLD_REVISION = "otel-demo-4d7168180749"
TRACES_SCHEMA_SHA256 = (
    "d22877f860e5b8a271c58de404c11d44fb536dbd78f9d9a9b142a15ab487aaba"
)
# R-5: registered in docs/tasks/2026-10-17-f3.md; change only to tighten.
RUBRIC_SHA256 = "b58974b2c2a5ba309cb0efcf47c5af001d9f9c3a27440fec32d5d31b5cf1d956"
ANSWER_SHA256 = "a35361da83e8a314a2af54bf50aa74441ca16dcd7a533e96c22c85a71b14b37a"
UI_USER = "lab-operator"
UI_SECRET = "fake-ui-secret-for-tests"
T0 = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


# -- tool description bump ------------------------------------------------


def _schema(name: str) -> dict[str, Any]:
    return next(
        dict(item["function"])
        for item in otel_demo.TOOL_SCHEMAS
        if item["function"]["name"] == name
    )


def test_revision_bumped_by_content_hash() -> None:
    assert otel_demo.TOOL_SCHEMA_REVISION != OLD_REVISION
    # Independent recomputation from the five model-visible inputs (C3 §5);
    # comparing with ``_tool_schema_revision()`` would be always true.
    data_sources = otel_demo.otel_demo_face(FakeClock()).evidence_context("run-1")[
        "data_sources"
    ]
    digest = canonical_hash(
        {
            "tool_schemas": [dict(item) for item in otel_demo.TOOL_SCHEMAS],
            "registry": ToolRegistry(
                otel_demo.otel_demo_registrations().tools
            ).revision,
            "trace_projection": otel_demo.TRACE_PROJECTION,
            "projection_revision": otel_demo.PROJECTION_REVISION,
            "data_sources": data_sources,
        }
    )
    assert otel_demo.TOOL_SCHEMA_REVISION == f"otel-demo-{digest[:12]}"
    # #308 PR2 bumped the projection to v9 (kept over-limit refusals, slices);
    # #311 to v10 (``source_error`` on 4xx refusals).
    assert otel_demo.PROJECTION_REVISION == "m1-01-tool-view-v10"


def test_traces_description_bytes_unchanged() -> None:
    traces = next(
        item
        for item in otel_demo.TOOL_SCHEMAS
        if item["function"]["name"] != otel_demo.METRICS_TOOL
    )
    digest = hashlib.sha256(json.dumps(traces, sort_keys=True).encode()).hexdigest()
    assert digest == TRACES_SCHEMA_SHA256


def test_metrics_description_lists_only_seen_ksm_series() -> None:
    metrics = _schema(otel_demo.METRICS_TOOL)
    text = metrics["description"]
    expr = metrics["parameters"]["properties"]["expr"]["description"]
    for series in (
        "kube_deployment_status_replicas_available",
        "kube_deployment_status_replicas_ready",
        "kube_replicaset_owner",
        "kube_pod_owner",
        "kube_pod_container_info",
    ):
        assert series in text
    for label in ("namespace", "deployment", "replicaset", "pod", "owner_name"):
        assert label in expr and label in text
    assert "not service_name" in text and "instead of service_name" in expr
    assert "image digest" in text and "new ReplicaSet" in text
    for unseen in (
        "kube_replicaset_labels",
        "kube_replicaset_created",
        "kube_deployment_metadata_generation",
        "kube_deployment_status_observed_generation",
        "kube_deployment_spec_replicas",
        "kube_pod_start_time",
    ):
        assert unseen not in text and unseen not in expr


def test_ksm_expression_passes_promql_guard() -> None:
    expr = 'kube_replicaset_owner{namespace="otel-demo",owner_name="checkout"}'
    assert otel_demo.promql_problem(expr, 3600) is None


# -- frozen files ----------------------------------------------------------


def test_frozen_sha256_matches_registered_values() -> None:
    assert driver.sha256_file(driver.RUBRIC) == RUBRIC_SHA256
    assert driver.sha256_file(driver.ANSWER) == ANSWER_SHA256
    record = (ROOT / "docs/tasks/2026-10-17-f3.md").read_text()
    assert RUBRIC_SHA256 in record and ANSWER_SHA256 in record


def test_stimulus_does_not_leak_answer() -> None:
    frozen = driver.load_frozen()
    template = frozen.stimulus["follow_up_template"]
    rendered = driver.render_follow_up(
        template, driver.utc_seconds(T0), driver.release_id_for(T0)
    )
    raw = driver.STIMULUS.read_text().lower()
    for word in driver.forbidden_words(frozen.answer):
        assert word not in rendered.lower()
        assert word not in raw
    assert driver.follow_up_problems(rendered, frozen.answer) == ()
    assert driver.answer_consistent(frozen.answer)


def test_eval_scenario_set_unchanged() -> None:
    names = sorted(p.name for p in (ROOT / "evals/scenarios").iterdir())
    assert names == [
        "eval-cart-failure",
        "eval-normal-1",
        "eval-normal-2",
        "eval-payment-failure-1",
        "eval-payment-failure-2",
        "eval-product-catalog-failure",
    ]


def test_follow_up_problems_detect_leak_and_controls() -> None:
    answer = driver.load_frozen().answer
    assert "leaks:payment" in driver.follow_up_problems("Payment note", answer)
    assert "leaks:paymentfailure" in driver.follow_up_problems(
        "flag paymentFailure", answer
    )
    assert "control_character" in driver.follow_up_problems("a\x1bb", answer)
    assert "length" in driver.follow_up_problems("x" * 9000, answer)


def test_release_id_is_neutral() -> None:
    for moment in (T0, T0 + timedelta(days=37, seconds=13)):
        value = driver.release_id_for(moment)
        assert value.startswith("rel-") and value[4:].isdigit()


# -- fakes -----------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.moment = T0

    def now(self) -> datetime:
        return self.moment

    def sleep(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


@dataclass
class FakeLab:
    root: Path
    calls: list[str] = field(default_factory=list)
    reservation_error: str | None = None
    active_fault: bool = False
    flags: dict[str, str] = field(
        default_factory=lambda: {"paymentFailure": "off", "adFailure": "off"}
    )
    alerts: int = 0
    rollouts: list[str] = field(default_factory=list)
    swap: driver.SwapUsage = driver.SwapUsage(8192, 1024)
    fault_error: BaseException | None = None
    on_publish: Any = None

    def ledger_root(self) -> Path:
        return self.root

    def check_reservation(self, holder: str, batch: str) -> None:
        self.calls.append("check_reservation")
        if self.reservation_error:
            raise ValueError(self.reservation_error)

    def fault_active(self) -> bool:
        return self.active_fault

    def flag_defaults(self) -> dict[str, str]:
        return self.flags

    def firing_alert_count(self, alertname: str) -> int:
        assert alertname == driver.ALERTNAME
        return self.alerts

    def recent_rollouts(self, since: datetime) -> list[str]:
        return self.rollouts

    def swap_usage(self) -> driver.SwapUsage:
        return self.swap

    @contextmanager
    def reserved(self, holder: str, batch: str) -> Iterator[None]:
        self.calls.append("reserved_enter")
        yield
        self.calls.append("reserved_exit")

    def publish(self, ledger: Path, release_id: str) -> dict[str, Any]:
        self.calls.append(f"publish:{release_id}")
        if self.on_publish:
            self.on_publish()
        return {"release_id": release_id, "released_at": T0.isoformat()}

    def scheduled_fault(self, ledger: Path, **kwargs: Any) -> None:
        self.calls.append(f"fault:{kwargs['flag']}:{kwargs['variant']}")
        if self.fault_error:
            raise self.fault_error


@dataclass
class FakeStore:
    incidents: list[driver.AlertIncident] = field(default_factory=list)
    observations: int = 0
    appear_after_polls: int = 0
    polls: int = 0

    def alert_incidents(
        self, alertname: str, namespace: str, workload: str
    ) -> list[driver.AlertIncident]:
        self.polls += 1
        if self.polls <= self.appear_after_polls:
            return []
        return self.incidents

    def release_observations_since(self, since: datetime) -> int:
        return self.observations


@dataclass
class FakeHttp:
    status: int = 200
    response: dict[str, Any] = field(
        default_factory=lambda: {"generation": 1, "action": "follow_up"}
    )
    requests: list[tuple[str, bytes, dict[str, str]]] = field(default_factory=list)

    def post_form(
        self, url: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> tuple[int, bytes]:
        self.requests.append((url, body, dict(headers)))
        return self.status, json.dumps(self.response).encode()


def incident(**changes: Any) -> driver.AlertIncident:
    base = driver.AlertIncident(
        incident_id="11111111-1111-1111-1111-111111111111",
        run_id="22222222-2222-2222-2222-222222222222",
        lifecycle="open",
        control_generation=0,
        run_state="queued",
        step_count=0,
        alertname=driver.ALERTNAME,
        created_at=T0 + timedelta(minutes=12),
    )
    return replace(base, **changes)


def ports(tmp_path: Path, **kw: Any) -> driver.Ports:
    return driver.Ports(
        lab=kw.get("lab") or FakeLab(tmp_path / "ledgers"),
        store=kw.get("store") or FakeStore(),
        http=kw.get("http") or FakeHttp(),
        clock=FakeClock(),
        env={"F3_LAB_UI_USER": UI_USER, "F3_LAB_UI_PASSWORD": UI_SECRET},
    )


GLOBAL = ["--holder", "lead", "--batch", "f3-batch"]
RUN = [
    "run",
    "--run",
    "first",
    "--web",
    "http://127.0.0.1:8080",
    "--flagd-url",
    "http://127.0.0.1:18013",
    "--flag-method",
    "String",
    "--alert-timeout",
    "60",
    "--poll-seconds",
    "10",
]


def lines(out: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.getvalue().splitlines()]


# -- dry-run ---------------------------------------------------------------


def test_dry_run_has_no_external_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("external call under --dry-run")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(kind_lab.OPENER, "open", forbidden)
    for name in ("lab_ledgers", "write_json", "publish", "scheduled_fault", "flags"):
        monkeypatch.setattr(harness, name, forbidden)
    import psycopg

    monkeypatch.setattr(psycopg, "connect", forbidden)
    for argv in (["plan"], ["preflight"], RUN):
        out = io.StringIO()
        code = driver.main(["--dry-run", *argv], stdout=out, ports_factory=forbidden)
        assert code == 0, out.getvalue()
    events = lines(out)
    assert events[0]["event"] == "plan"
    assert events[0]["fault"]["flag"] == "paymentFailure"
    assert events[0]["fault"]["variant"] == "100%"
    assert events[0]["fault"]["offset_seconds"] >= 300
    assert all(c["status"] == "skipped" for c in events[1]["checks"])
    assert [e["name"] for e in events if e["event"] == "action"] == list(driver.ACTIONS)
    assert events[-1] == {
        "event": "finished",
        "ok": True,
        "reason": None,
        "dry_run": True,
        "ledger": None,
    }


def test_plan_timing_arithmetic() -> None:
    out = io.StringIO()
    assert driver.main(["plan"], stdout=out) == 0
    plan = json.loads(out.getvalue())
    timing = plan["timing"]
    assert timing["rollout_to_onset_ok"] and timing["release_in_alert_window"]
    assert timing["schedule_within_harness_limit"]
    assert plan["fault"]["offset_seconds"] + plan["fault"]["duration_seconds"] <= 7200
    assert plan["frozen"]["answer_sha256"] == ANSWER_SHA256


# -- preconditions ---------------------------------------------------------

FAILURES = {
    "lab_reserved": ("lab", "reservation_error", "LAB_RESERVATION_REQUIRED"),
    "no_active_fault": ("lab", "active_fault", True),
    "flags_off": ("lab", "flags", {"paymentFailure": "100%"}),
    "alert_not_firing": ("lab", "alerts", 1),
    "no_open_alert_incident": (
        "store",
        "incidents",
        [incident(created_at=T0 - timedelta(days=1))],
    ),
    "no_recent_rollout": ("lab", "rollouts", ["replicaset checkout-abc"]),
    "swap_headroom": ("lab", "swap", driver.SwapUsage(17000, 16500)),
}


@pytest.mark.parametrize("check_id", driver.PRECONDITION_IDS)
def test_each_precondition_has_its_own_reason(tmp_path: Path, check_id: str) -> None:
    lab, store = FakeLab(tmp_path / "ledgers"), FakeStore()
    target, attribute, value = FAILURES[check_id]
    setattr(lab if target == "lab" else store, attribute, value)
    out = io.StringIO()
    code = driver.main(
        [*GLOBAL, "preflight"], stdout=out, ports=ports(tmp_path, lab=lab, store=store)
    )
    report = json.loads(out.getvalue())
    assert code == 3
    assert report["reason"] == driver.PRECONDITION_REASONS[check_id]
    failed = [c["id"] for c in report["checks"] if c["status"] != "pass"]
    assert failed == [check_id]


def test_release_observation_in_flight_fails_rollout_check(tmp_path: Path) -> None:
    out = io.StringIO()
    code = driver.main(
        [*GLOBAL, "preflight"],
        stdout=out,
        ports=ports(tmp_path, store=FakeStore(observations=1)),
    )
    assert code == 3
    assert json.loads(out.getvalue())["reason"] == "PRECONDITION_RECENT_ROLLOUT"


def test_failing_port_is_unverified_not_pass(tmp_path: Path) -> None:
    lab = FakeLab(tmp_path / "ledgers")

    def broken() -> driver.SwapUsage:
        raise ValueError("SWAP_USAGE_UNPARSED")

    lab.swap_usage = broken  # type: ignore[method-assign]
    out = io.StringIO()
    code = driver.main(
        [*GLOBAL, "preflight"], stdout=out, ports=ports(tmp_path, lab=lab)
    )
    report = json.loads(out.getvalue())
    assert code == 3 and report["reason"] == "PRECONDITION_UNVERIFIED"
    assert report["checks"][-1]["status"] == "unverified"


def test_run_refuses_before_publish_when_precondition_fails(tmp_path: Path) -> None:
    lab = FakeLab(tmp_path / "ledgers", alerts=2)
    out = io.StringIO()
    code = driver.main([*GLOBAL, *RUN], stdout=out, ports=ports(tmp_path, lab=lab))
    assert code == 3
    assert not any(call.startswith("publish") for call in lab.calls)
    assert lines(out)[-1]["reason"] == "PRECONDITION_ALERT_FIRING"


# -- live run with fakes ---------------------------------------------------


def test_run_sends_neutral_follow_up_through_control_entry(tmp_path: Path) -> None:
    lab = FakeLab(tmp_path / "ledgers")
    store = FakeStore(incidents=[incident()], appear_after_polls=3)
    http = FakeHttp()
    out = io.StringIO()
    code = driver.main(
        [*GLOBAL, *RUN],
        stdout=out,
        ports=ports(tmp_path, lab=lab, store=store, http=http),
    )
    assert code == 0, out.getvalue()
    events = lines(out)
    names = [e["event"] for e in events]
    assert names == [
        "plan",
        "preflight",
        "released",
        "incident",
        "follow_up",
        "start_worker",
        "finished",
    ]
    assert lab.calls[1:3] == ["reserved_enter", "publish:rel-20261022120000"]
    assert "fault:paymentFailure:100%" in lab.calls
    assert lab.calls[-1] == "reserved_exit"
    ((url, body, headers),) = http.requests
    assert url.endswith(f"/incidents/{incident().incident_id}/control")
    fields = dict(urllib.parse.parse_qsl(body.decode()))
    assert fields["action"] == "follow_up"
    assert fields["expected_generation"] == "0"
    assert fields["idempotency_key"] == "note-rel-20261022120000"
    assert fields["text"] == (
        "Operator note: Deployment otel-demo/checkout was rolled out at "
        "2026-10-22T12:00:00Z (release rel-20261022120000)."
    )
    assert headers["Origin"] == "http://127.0.0.1:8080"
    assert headers["Authorization"].startswith("Basic ")
    answer = driver.load_frozen().answer
    for word in driver.forbidden_words(answer):
        assert word not in body.decode().lower()
    ledger = tmp_path / "ledgers" / "first"
    stored = "".join(p.read_text() for p in ledger.iterdir())
    assert UI_SECRET not in stored and UI_SECRET not in out.getvalue()
    assert headers["Authorization"] not in stored + out.getvalue()
    record = json.loads((ledger / "follow-up.json").read_text())
    assert record["fields"]["idempotency_label"] == "note-rel-20261022120000"
    assert "idempotency_key" not in record["fields"]
    assert record["request_sha256"] == hashlib.sha256(body).hexdigest()
    assert events[-1]["ok"] and events[-1]["incident_id"] == incident().incident_id


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"control_generation": 1}, "GENERATION_MOVED"),
        ({"run_state": "running"}, "WORKER_ALREADY_CLAIMED"),
        ({"step_count": 1}, "WORKER_ALREADY_CLAIMED"),
    ],
)
def test_run_does_not_follow_up_after_worker_or_other_control(
    tmp_path: Path, changes: dict[str, Any], reason: str
) -> None:
    http = FakeHttp()
    out = io.StringIO()
    code = driver.main(
        [*GLOBAL, *RUN],
        stdout=out,
        ports=ports(
            tmp_path,
            store=FakeStore(incidents=[incident(**changes)], appear_after_polls=1),
            http=http,
        ),
    )
    assert code == 5 and lines(out)[-1]["reason"] == reason
    assert http.requests == []


def test_run_reports_missing_and_ambiguous_incident(tmp_path: Path) -> None:
    out = io.StringIO()
    code = driver.main([*GLOBAL, *RUN], stdout=out, ports=ports(tmp_path))
    assert code == 5 and lines(out)[-1]["reason"] == "ALERT_INCIDENT_NOT_FOUND"
    two = [incident(), incident(incident_id="33333333-3333-3333-3333-333333333333")]
    out = io.StringIO()
    code = driver.main(
        [*GLOBAL, *RUN],
        stdout=out,
        ports=ports(
            tmp_path / "b", store=FakeStore(incidents=two, appear_after_polls=1)
        ),
    )
    assert code == 5 and lines(out)[-1]["reason"] == "ALERT_INCIDENT_AMBIGUOUS"


def test_run_reports_refusal_and_fault_failure(tmp_path: Path) -> None:
    out = io.StringIO()
    http = FakeHttp(status=409, response={"code": "CONTROL_CONFLICT"})
    code = driver.main(
        [*GLOBAL, *RUN],
        stdout=out,
        ports=ports(
            tmp_path,
            store=FakeStore(incidents=[incident()], appear_after_polls=1),
            http=http,
        ),
    )
    final = lines(out)[-1]
    assert code == 5 and final["reason"] == "FOLLOW_UP_REFUSED"
    assert final["http_status"] == 409 and final["refusal"] == "CONTROL_CONFLICT"
    lab = FakeLab(
        tmp_path / "c", fault_error=ValueError("FLAG_PROPAGATION_UNCONFIRMED")
    )
    out = io.StringIO()
    code = driver.main(
        [*GLOBAL, *RUN],
        stdout=out,
        ports=ports(
            tmp_path,
            lab=lab,
            store=FakeStore(incidents=[incident()], appear_after_polls=1),
        ),
    )
    assert code == 5 and lines(out)[-1]["reason"] == "FAULT_FAILED"


def test_run_requires_credentials_and_loopback_web(tmp_path: Path) -> None:
    bad = ports(tmp_path)
    bad.env = {}
    out = io.StringIO()
    assert driver.main([*GLOBAL, *RUN], stdout=out, ports=bad) == 2
    assert lines(out)[-1]["reason"] == "CONFIG_UI_CREDENTIALS_MISSING"
    argv = [*GLOBAL, *RUN]
    argv[argv.index("http://127.0.0.1:8080")] = "http://example.com"
    out = io.StringIO()
    assert driver.main(argv, stdout=out, ports=ports(tmp_path)) == 2
    assert lines(out)[-1]["reason"] == "CONFIG_WEB_URL_INVALID"


def test_existing_ledger_is_never_reused(tmp_path: Path) -> None:
    (tmp_path / "ledgers" / "first").mkdir(parents=True)
    lab = FakeLab(tmp_path / "ledgers")
    out = io.StringIO()
    code = driver.main([*GLOBAL, *RUN], stdout=out, ports=ports(tmp_path, lab=lab))
    assert code == 5 and lines(out)[-1]["reason"] == "RUN_LEDGER_EXISTS"
    assert not any(call.startswith("publish") for call in lab.calls)


def test_driver_never_calls_register_or_webhook() -> None:
    source = Path(driver.__file__).read_text()
    for name in driver.FORBIDDEN_HARNESS_CALLS:
        assert f"harness.{name}" not in source
    used = set(__import__("re").findall(r"harness\.([a-z_]+)", source))
    assert used <= set(driver.HARNESS_CALLS)
    assert all(hasattr(harness, name) for name in driver.HARNESS_CALLS)


@pytest.mark.parametrize("dry_run", [[], ["--dry-run"]])
def test_command_line_checks_precede_holder_and_environment(
    dry_run: list[str],
) -> None:
    argv = [*dry_run, *RUN]
    argv[argv.index("first")] = "bad!name"
    out = io.StringIO()
    assert driver.main(argv, stdout=out) == 2
    assert lines(out)[-1]["reason"] == "CONFIG_RUN_NAME_INVALID"


@pytest.mark.parametrize(
    "defaults",
    [{}, {"adFailure": "off"}, {"paymentFailure": ""}],
    ids=["empty", "missing-injected-flag", "empty-variant"],
)
def test_incomplete_flag_set_is_unverified(
    tmp_path: Path, defaults: dict[str, str]
) -> None:
    lab = FakeLab(tmp_path / "ledgers", flags=defaults)
    out = io.StringIO()
    code = driver.main(
        [*GLOBAL, "preflight"], stdout=out, ports=ports(tmp_path, lab=lab)
    )
    report = json.loads(out.getvalue())
    check = next(c for c in report["checks"] if c["id"] == "flags_off")
    assert code == 3 and report["reason"] == "PRECONDITION_UNVERIFIED"
    assert check["status"] == "unverified" and check["detail"] == "FLAG_SET_INCOMPLETE"


def test_real_lab_refuses_flag_set_other_than_eleven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = json.dumps({"flags": {"paymentFailure": {"defaultVariant": "off"}}})
    monkeypatch.setattr(harness, "flags", lambda: ({}, raw.encode()))
    with pytest.raises(ValueError, match="FLAG_SET_INCOMPLETE"):
        driver.RealLab().flag_defaults()


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "-1"])
@pytest.mark.parametrize("command", ["preflight", "run"])
@pytest.mark.parametrize("dry_run", [[], ["--dry-run"]])
def test_swap_stop_line_must_be_finite_nonnegative(
    tmp_path: Path, value: str, command: str, dry_run: list[str]
) -> None:
    lab = FakeLab(tmp_path / "ledgers")
    tail = ["preflight"] if command == "preflight" else RUN
    out = io.StringIO()
    code = driver.main(
        [*dry_run, *GLOBAL, f"--swap-min-free-mib={value}", *tail],
        stdout=out,
        ports=ports(tmp_path, lab=lab),
    )
    assert code == 2 and lines(out)[-1]["reason"] == "CONFIG_SWAP_MIN_FREE_INVALID"
    assert lab.calls == []


@pytest.mark.parametrize("value", ["nan", "inf", "-5", "0"])
@pytest.mark.parametrize(
    ("option", "reason"),
    [
        ("--alert-timeout", "CONFIG_ALERT_TIMEOUT_INVALID"),
        ("--poll-seconds", "CONFIG_POLL_SECONDS_INVALID"),
    ],
)
def test_timeout_and_poll_must_be_finite_positive(
    tmp_path: Path, value: str, option: str, reason: str
) -> None:
    lab = FakeLab(tmp_path / "ledgers")
    argv = [*GLOBAL, *RUN]
    argv[argv.index(option) + 1] = value
    out = io.StringIO()
    code = driver.main(argv, stdout=out, ports=ports(tmp_path, lab=lab))
    assert code == 2 and lines(out)[-1]["reason"] == reason
    assert lab.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:8013",
        "http://example.com:8013",
        "https://127.0.0.1:8013",
        "grpc://127.0.0.1:8013",
        "http://user:pw@127.0.0.1:8013",
        "http://127.0.0.1",
        "http://localhost",
        "http://127.0.0.1:notaport",
        "127.0.0.1:8013",
        "not a url",
        "http://127.0.0.1:8013/path",
        "",
    ],
)
@pytest.mark.parametrize("dry_run", [[], ["--dry-run"]])
def test_flagd_url_is_checked_before_publish(
    tmp_path: Path, url: str, dry_run: list[str]
) -> None:
    lab = FakeLab(tmp_path / "ledgers")
    argv = [*dry_run, *GLOBAL, *RUN]
    argv[argv.index("--flagd-url") + 1] = url
    out = io.StringIO()
    code = driver.main(argv, stdout=out, ports=ports(tmp_path, lab=lab))
    assert code == 2 and lines(out)[-1]["reason"] == "CONFIG_FLAGD_URL_INVALID"
    assert lab.calls == []


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8013", "http://localhost:8013/", "http://[::1]:8013"]
)
def test_loopback_flagd_url_is_accepted(url: str) -> None:
    assert driver.check_flagd_url(url) == url
