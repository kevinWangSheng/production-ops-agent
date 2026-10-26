"""F3 six steps on the product path (PostgreSQL), scripted model, no kind.

Independent acceptance tests by a fresh-context author who did not implement
PR-3b, from the PR-3b pre-review (section 6), the F3 contract and
``feature_list.json`` F3. The judge (rubric gates 1-4, step 1/2/6 checks) is
``tests/acceptance/test_f3_step5_rubric_gates.py``; here it is applied to a
Run record built from the live public projections, so the same assertions
that will judge the two frozen kind Runs are shown to hold on the product.

Wiring: scratch database migrated to head; web workbench under the
``otel-demo`` profile with a target registry that binds the
``CheckoutPlaceOrderErrorRatioHigh`` labels; the real ``InvestigationRunner``
with the real ``DeepSeekClient`` over a scripted provider
(``tests/acceptance/f7_pg_support.ScriptedProvider``); Prometheus and Jaeger
answered by the recorded-response ``FakeOpener`` through the production
urllib transport (no network). Human actions go through the HTTP control
route with Basic Auth.

Scenarios (each an ``IncidentScenario`` with ``feature_id="F3"``):

* step 5 (+1, 2, 6 on the same Run): alert -> new incident + queued Run ->
  pre-claim ``follow_up`` carrying the frozen stimulus text -> one worker
  attempt. The scripted model reads traces (payment Charge error spans),
  metrics (flat checkout error rate before the fault, a KSM series) and a
  discovery tool, then reports a fact on payment Charge and a
  rejected_hypothesis / counter_evidence on the deployment.
* steps 3 + 4: a UI incident whose tool round commits ok, input error,
  connectivity failure, Prometheus-side timeout and no data, plus a Jaeger
  read that cannot connect.
* step 6: a model requesting an unregistered write tool gets a refusal and no
  outbound request; every outbound request of the module is a GET.
* step 1 legacy: an observation whose view predates fields is listed with
  ``missing_fields`` by a projection running in a READ ONLY transaction.

What a scripted model cannot show: the model's own competing-cause
reasoning (rubric items 5-8) -- that is the two kind Runs plus reviewers.

ASSUMED beyond the contract: an unregistered tool name ends as one refused
operation with status ``error`` or ``denied`` and is never dispatched (at
3c18bdb: ``denied/TOOL_NOT_REGISTERED`` with ``tool=None``); the
alert incident's Run records ``bound_target_id`` and its first time policy
``window`` in its input. Opt-in: ``M1_DURABLE_POSTGRES=1`` with
``OPSPILOT_LAB_DSN``; a scratch database is created and dropped.
"""

from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from opspilot.acceptance import outcome_from_durable
from opspilot.acceptance_audit import audit_timeline
from opspilot.acceptance_investigation import investigation_evidence
from opspilot.persistence import DurableStore
from opspilot.tools.otel_demo import (
    LABEL_VALUES_TOOL,
    METRICS_TOOL,
    TARGET_ID,
    TRACES_TOOL,
)
from opspilot.tools.profiles import select_profile
from opspilot.web import (
    Authenticator,
    DurableClock,
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
    Workbench,
    create_app,
)
from opspilot.web.store import MappingTargetRegistry
from tests.acceptance.f7_pg_support import (
    MODEL_KEY,
    POOL,
    UI,
    ScriptedProvider,
    _auth,
    assistant,
    fn,
    scratch_database,
    tool_views,
)
from tests.acceptance.test_f3_step5_rubric_gates import (
    STIMULUS,
    answer,
    gate4_problems,
    plain,
    registered_verbs,
    rubric_gate_problems,
    scenario,
    scenario_shape_problems,
    signature_matches,
    step1_problems,
    step2_problems,
)
from tests.m1_web_support import basic, bearer, call, post_form, post_json
from tests.test_m1_otel_demo_contract import FakeOpener

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

# The incident target is the profile target itself, as in the lab registry.
TARGET_KEY = TARGET_ID
LABELS = {"namespace": "otel-demo", "service": "checkout"}
ALERT_ENTRY = {
    "integration_id": TARGET_ID,
    "cluster_uid": "opspilot-m1",
    "namespace": "otel-demo",
    "workload": "checkout",
    "health_profile_id": "otel-demo-checkout",
    "match": {"version": 1, "labels": LABELS},
}
ERROR_EXPR = (
    'sum(rate(traces_span_metrics_calls_total{service_name="checkout",'
    'span_name="oteldemo.CheckoutService/PlaceOrder",status_code="STATUS_CODE_ERROR"}[5m]))'
)
KSM_EXPR = 'kube_replicaset_owner{namespace="otel-demo",owner_name="checkout"}'
BAD_EXPR = "sum(rate(traces_span_metrics_calls_total[5m])"
DOWN_EXPR = 'sum(rate(traces_span_metrics_calls_total{service_name="ad"}[5m]))'
SLOW_EXPR = 'sum(rate(traces_span_metrics_calls_total{service_name="cart"}[5m]))'
EMPTY_EXPR = 'sum(rate(traces_span_metrics_calls_total{service_name="nothing"}[5m]))'
WRITE_TOOL = "prometheus_admin_delete_series"
TIMEOUT_BODY = json.dumps(
    {"status": "error", "errorType": "timeout", "error": "query timed out"}
).encode()
FACT = "F3-FACT payment Charge calls fail with error spans."
REJECTED = "F3-REJECTED the checkout configuration rollout caused the errors."
COUNTER = "F3-COUNTER checkout PlaceOrder errors stayed flat after the rollout."
HYPOTHESIS = "F3-HYPOTHESIS the payment backend rejects charges."
RECOMMENDATION = "F3-RECOMMENDATION have the payment owner check its feature flags."


# -- backend payloads -------------------------------------------------------------


def _matrix(start: datetime, values: list[float]) -> bytes:
    stamps = [int(start.timestamp()) + 60 * i for i in range(len(values))]
    rows = [
        {
            "metric": {"service_name": "checkout"},
            "values": [[s, str(v)] for s, v in zip(stamps, values, strict=True)],
        }
    ]
    return json.dumps(
        {"status": "success", "data": {"resultType": "matrix", "result": rows}}
    ).encode()


def _charge_traces(start: datetime) -> bytes:
    """One checkout trace whose PaymentService/Charge client and server spans
    carry error status (evaluator signature payment-charge-error-span-v1)."""
    base = int(start.timestamp() * 1_000_000)
    trace = "f3f3000000000000000000000000f301"
    spans = [
        ("s0", "p0", "oteldemo.CheckoutService/PlaceOrder", None, False),
        ("s1", "p0", "oteldemo.PaymentService/Charge", "s0", True),
        ("s2", "p1", "grpc.oteldemo.PaymentService/Charge", "s1", True),
    ]
    rows = []
    for i, (span_id, process, operation, parent, error) in enumerate(spans):
        tags = [{"key": "span.kind", "type": "string", "value": "server"}]
        if error:
            tags += [
                {"key": "otel.status_code", "type": "string", "value": "ERROR"},
                {"key": "rpc.grpc.status_code", "type": "int64", "value": 2},
            ]
        rows.append(
            {
                "traceID": trace,
                "spanID": f"f3f30000000000{span_id}",
                "operationName": operation,
                "references": []
                if parent is None
                else [
                    {
                        "refType": "CHILD_OF",
                        "traceID": trace,
                        "spanID": f"f3f30000000000{parent}",
                    }
                ],
                "startTime": base + i * 1_000,
                "duration": 5_000 - i * 1_000,
                "tags": tags,
                "logs": [],
                "processID": process,
                "warnings": None,
            }
        )
    body = {
        "data": [
            {
                "traceID": trace,
                "spans": rows,
                "processes": {
                    "p0": {"serviceName": "checkout", "tags": []},
                    "p1": {"serviceName": "payment", "tags": []},
                },
            }
        ],
        "total": 0,
        "limit": 0,
        "offset": 0,
        "errors": None,
    }
    return json.dumps(body).encode()


def _http_error(status: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://127.0.0.1:19090/api/v1/query_range",
        status,
        "error",
        {},
        io.BytesIO(body),
    )


class _Opener(FakeOpener):
    """``FakeOpener`` whose Jaeger answer depends on the ``service`` asked
    for (``ad`` cannot connect), and whose timeout reply is rebuilt per call
    (an ``HTTPError`` body can be read once)."""

    def open(self, request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else request
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        if parts.path.endswith("/api/traces") and query.get("service") == ["ad"]:
            self.requests.append(request)
            self.timeouts.append(timeout)
            raise urllib.error.URLError(ConnectionRefusedError(61, "refused"))
        if query.get("query") == [SLOW_EXPR]:
            self.requests.append(request)
            self.timeouts.append(timeout)
            raise _http_error(503, TIMEOUT_BODY)
        return super().open(request, timeout)


# -- stack ---------------------------------------------------------------------------


def _db_now(dsn: str) -> datetime:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT clock_timestamp()").fetchone()[0].astimezone(UTC)


@pytest.fixture(scope="module")
def dsn() -> Iterator[str]:
    yield from scratch_database()


@pytest.fixture(scope="module")
def env(dsn: str) -> Iterator[dict[str, Any]]:
    starts = _db_now(dsn).replace(microsecond=0) - timedelta(minutes=20)
    opener = _Opener(
        routes={
            "/api/traces": _charge_traces(starts - timedelta(minutes=2)),
            "/values": json.dumps(
                {"status": "success", "data": ["checkout", "payment", "frontend"]}
            ).encode(),
        },
        by_query={
            ERROR_EXPR: _matrix(starts - timedelta(minutes=30), [0.0] * 10 + [4.0] * 5),
            KSM_EXPR: _matrix(starts - timedelta(minutes=30), [1.0] * 15),
            BAD_EXPR: 400,
            DOWN_EXPR: urllib.error.URLError(ConnectionRefusedError(61, "refused")),
            EMPTY_EXPR: json.dumps(
                {"status": "success", "data": {"resultType": "matrix", "result": []}}
            ).encode(),
        },
    )
    patch = pytest.MonkeyPatch()
    patch.setenv("OPSPILOT_OTEL_PROMETHEUS_URL", "http://127.0.0.1:19090")
    patch.setenv("OPSPILOT_OTEL_JAEGER_URL", "http://127.0.0.1:16686/jaeger/ui")
    patch.delenv("OPSPILOT_OTEL_TOKEN", raising=False)
    patch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    profile = select_profile({"OPSPILOT_TOOL_PROFILE": "otel-demo"})
    store = DurableStore(dsn, pool=POOL)
    store.install()
    events = DurableEventLog(store)
    evidence = DurableEvidenceStore(store)
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=evidence,
        ledger=DurableWebLedger(store),
        run_versions=profile.versions(),
        tool_face=profile.face(DurableClock(store)),
        run_seconds=600,
        targets=MappingTargetRegistry({TARGET_KEY: ALERT_ENTRY}),
    )
    app = create_app(workbench, Authenticator(_auth()), DurableClock(store))
    try:
        yield {
            "dsn": dsn,
            "store": store,
            "events": events,
            "evidence": evidence,
            "app": app,
            "profile": profile,
            "opener": opener,
            "starts": starts,
        }
    finally:
        store.close()
        patch.undo()


def _run_worker(env, incident: UUID, provider: ScriptedProvider):
    from opspilot.investigation.client import DeepSeekClient
    from opspilot.investigation.runner import InvestigationRunner
    from opspilot.worker import Worker

    store, clock = env["store"], DurableClock(env["store"])
    runner = InvestigationRunner(
        store=store,
        worker=Worker.create(store, env["profile"].versions()),
        model=DeepSeekClient(MODEL_KEY, opener=provider),
        executor_factory=env["profile"].executor_factory(store, env["evidence"], clock),
        clock=clock,
        lease_seconds=30,
        events=env["events"],
        evidence=env["evidence"],
    )
    return runner.resume(incident)


def _outcome(env, incident: UUID, sc):
    controls = tuple(
        asdict(row)
        for row in DurableIncidentStore(env["store"]).control_audit(incident)
    )
    handoffs = tuple(
        dict(event.payload)
        for event in env["events"].read_after(incident, 0, limit=1000)
        if event.kind == "run_handoff"
    )
    return outcome_from_durable(
        sc, env["store"].rebuild(incident), controls=controls, handoff_events=handoffs
    )


def run_record(env, incident: UUID, sc) -> dict[str, Any]:
    """The Run record the judge reads (shape: the judge module's docstring),
    built only from public projections and committed rows."""
    snapshot = env["store"].rebuild(incident)
    run = snapshot["run"]
    run_input = run["input"]
    conclusion = snapshot["conclusion"]["conclusion"]
    report = json.loads(conclusion["report_content"])
    with psycopg.connect(env["dsn"], row_factory=dict_row) as conn:
        conn.read_only = True
        projection = investigation_evidence(conn, incident)
        timeline = audit_timeline(conn, incident)
        cited_ids = sorted({i for c in report["claims"] for i in c["evidence_ids"]})
        cited = {
            row["evidence_id"]: {
                "run_id": row["run_id"],
                "subject_id": row["subject_id"],
                "status": row["status"],
                "view": row["view"],
            }
            for row in conn.execute(
                "SELECT evidence_id,run_id,subject_id,status,view FROM opspilot_evidence"
                " WHERE evidence_id=ANY(%s)",
                (cited_ids,),
            ).fetchall()
        }
        runs = conn.execute(
            "SELECT run_id,control_generation,state,versions FROM opspilot_runs"
            " WHERE incident_id=%s ORDER BY control_generation,run_id",
            (incident,),
        ).fetchall()
        deliveries = [
            row["outcome"]
            for row in conn.execute(
                "SELECT outcome FROM opspilot_alert_deliveries WHERE incident_id=%s"
                " ORDER BY delivery_id",
                (incident,),
            ).fetchall()
        ]
        conn.rollback()
    window = run_input["evidence_context"]["time_policies"][0]["window"]
    return {
        "scenario_id": "F3:competing-cause",
        "incident_id": str(incident),
        "run_id": str(run["run_id"]),
        "bound_target_id": run_input["bound_target_id"],
        "authorized_window": {"start": window["start"], "end": window["end"]},
        "outcome": plain(_outcome(env, incident, sc)),
        "report": report,
        "investigation_evidence": plain(projection),
        "cited_evidence": plain(cited),
        "audit_queries": [
            {"run_id": str(e.run_id), **plain(dict(e.detail))}
            for e in timeline.entries
            if e.category == "query"
        ],
        "incident_runs": [
            {
                "run_id": str(r["run_id"]),
                "control_generation": r["control_generation"],
                "state": r["state"],
            }
            for r in runs
        ],
        "alert_delivery_outcomes": deliveries,
        "run_versions": runs[0]["versions"] if runs else None,
    }


# -- step 5 scenario --------------------------------------------------------------


def _follow_up_text(released_at: datetime) -> str:
    template = json.loads(STIMULUS.read_text())["follow_up_template"]
    return template.format(
        released_at=released_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        release_id="rel-" + released_at.strftime("%Y%m%d%H%M%S"),
    )


def _evidence_of(messages, tool: str, needle: str | None = None) -> str:
    for item in tool_views(messages):
        view = item["view"]
        if view.get("tool") == tool and view.get("status") == "ok":
            if needle is None or needle in json.dumps(view.get("query")):
                return view["evidence_id"]
    seen = [
        (v["view"].get("tool"), v["view"].get("status"), v["view"].get("reason"))
        for v in tool_views(messages)
    ]
    raise AssertionError(f"no ok {tool} view delivered to the model: {seen}")


def _competing_report(messages) -> dict[str, Any]:
    traces = _evidence_of(messages, TRACES_TOOL)
    rate = _evidence_of(messages, METRICS_TOOL, "PlaceOrder")
    ksm = _evidence_of(messages, METRICS_TOOL, "kube_replicaset_owner")

    def cited(kind, text, ids):
        return {
            "kind": kind,
            "text": text,
            "evidence_ids": ids,
            **(
                {"target_refs": [TARGET_ID], "time_scope_ref": "policy-window-1"}
                if ids
                else {}
            ),
        }

    payload = {
        "schema_version": "m0-report-v2",
        "assessment_status": "completed",
        "conclusion": "supported",
        "summary": "Checkout errors come from failing payment Charge calls; the "
        "checkout rollout is rejected as the cause.",
        "claims": [
            cited("fact", FACT, [traces]),
            cited("hypothesis", HYPOTHESIS, []),
            cited("recommendation", RECOMMENDATION, []),
            cited("counter_evidence", COUNTER, [rate]),
            cited("rejected_hypothesis", REJECTED, [rate, ksm]),
        ],
        "gaps": ["Payment service logs are not integrated."],
        "next_steps": ["Have the payment owner confirm the failing charges."],
    }
    return assistant(content=json.dumps(payload))


def _send_alert(env) -> dict[str, Any]:
    alert = {
        "status": "firing",
        "labels": {
            "alertname": "CheckoutPlaceOrderErrorRatioHigh",
            "severity": "critical",
            **LABELS,
        },
        "annotations": {"summary": "checkout error ratio is high"},
        "startsAt": env["starts"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": uuid4().hex[:16],
        "generatorURL": "http://prometheus:9090/graph?g0.expr=up",
    }
    response = post_json(
        env["app"],
        "/intake/alertmanager",
        {"version": "4", "status": "firing", "alerts": [alert]},
        headers=bearer(),
    )
    assert response.status == 200, response.text
    (item,) = response.json()["results"]
    return item


def _control(env, incident, action, generation, text=None):
    fields = {
        "action": action,
        "expected_generation": str(generation),
        "idempotency_key": f"f3-{action}-{uuid4().hex}",
    }
    if text is not None:
        fields["text"] = text
    response = post_form(
        env["app"],
        f"/incidents/{incident}/control",
        fields,
        headers={**UI, "accept": "application/json"},
    )
    assert response.status == 200, (action, response.status, response.text)
    return response.json()


@pytest.fixture(scope="module")
def competing(env) -> dict[str, Any]:
    created = _send_alert(env)
    assert created["outcome"] == "created", created
    incident = UUID(created["incident_id"])
    run_before = env["store"].rebuild(incident)["run"]["run_id"]
    text = _follow_up_text(env["starts"] - timedelta(minutes=8))
    answered = _control(env, incident, "follow_up", 0, text=text)
    assert answered["generation"] == 1, answered
    provider = ScriptedProvider(
        [
            assistant(
                tool_calls=[
                    fn("c1", TRACES_TOOL, {"service": "checkout", "limit": 5}),
                    fn("c2", METRICS_TOOL, {"expr": ERROR_EXPR}),
                    fn("c3", METRICS_TOOL, {"expr": KSM_EXPR}),
                    fn("c4", LABEL_VALUES_TOOL, {"label": "service_name"}),
                ]
            ),
            _competing_report,
        ]
    )
    sent_before = len(env["opener"].requests)
    result = _run_worker(env, incident, provider)
    sc = scenario(5, "competing-cause", str(incident))
    return {
        "incident": incident,
        "run_before": run_before,
        "text": text,
        "provider": provider,
        "result": result,
        "record": run_record(env, incident, sc),
        "requests": env["opener"].requests[sent_before:],
    }


def test_step5_alert_follow_up_and_one_run_published(competing) -> None:
    sc = scenario(5, "competing-cause", str(competing["incident"]))
    record = competing["record"]
    assert scenario_shape_problems(record) == [], sc
    assert record["run_id"] == str(competing["run_before"]), sc
    assert record["outcome"]["final_state"] == "completed", record["outcome"]


def test_step5_follow_up_is_visible_in_the_first_model_round(competing) -> None:
    first = competing["provider"].requests[0]["messages"]
    assert any(competing["text"] in json.dumps(m) for m in first), (
        "follow_up not in round 1"
    )
    # The evaluator answer never reaches the model (answer isolation).
    sent = b"".join(competing["provider"].raw_requests).decode("utf-8", "replace")
    assert "paymentFailure" not in sent and "payment-charge-error-span" not in sent


def test_step5_rubric_deterministic_gates_hold_on_the_product_path(competing) -> None:
    record = competing["record"]
    assert rubric_gate_problems(record) == {
        "gate1": [],
        "gate2": [],
        "gate3": [],
        "gate4": [],
    }


def test_step5_signature_is_on_the_cited_trace_evidence(competing) -> None:
    record = competing["record"]
    (fact,) = [c for c in record["report"]["claims"] if c["kind"] == "fact"]
    view = record["cited_evidence"][fact["evidence_ids"][0]]["view"]
    assert signature_matches(view, answer()["signature"]) >= 1


def test_step1_every_observation_carries_its_attribution(competing) -> None:
    sc = scenario(1, "competing-cause", str(competing["incident"]))
    assert step1_problems(competing["record"]) == [], sc


def test_step2_five_claim_kinds_stay_apart(competing) -> None:
    sc = scenario(2, "competing-cause", str(competing["incident"]))
    record = competing["record"]
    assert step2_problems(record) == [], sc
    claims = record["investigation_evidence"]["reports"][0]["claims"]
    assert [c["text"] for c in claims["fact"]] == [FACT]
    assert [c["text"] for c in claims["rejected_hypothesis"]] == [REJECTED]


def test_step6_tool_requests_are_read_only_gets_on_the_bound_target(competing) -> None:
    sc = scenario(6, "competing-cause", str(competing["incident"]))
    record = competing["record"]
    assert gate4_problems(record, registered_verbs()) == [], sc
    requests = competing["requests"]
    assert requests, "no outbound read"
    for request in requests:
        assert request.get_method() == "GET", request.full_url
        path = urlsplit(request.full_url).path
        assert path.endswith(("/api/v1/query_range", "/values", "/api/traces")), path
    # One outbound read per tool call: nothing beyond what the model asked.
    assert len(requests) == 4


# -- steps 3 + 4 scenario ------------------------------------------------------------


@pytest.fixture(scope="module")
def four_classes(env) -> dict[str, Any]:
    response = post_form(
        env["app"],
        "/intake/ui",
        {
            "target_id": TARGET_KEY,
            "question": "Why is checkout erroring?",
            "idempotency_key": f"f3-four-{uuid4()}",
        },
        headers=UI,
    )
    assert response.status == 201, response.text
    incident = UUID(response.json()["incident_id"])

    def report(messages):
        ok = [
            v["view"]["evidence_id"]
            for v in tool_views(messages)
            if v["view"].get("status") == "ok"
        ]
        payload = {
            "schema_version": "m0-report-v2",
            "assessment_status": "completed",
            "conclusion": "partial",
            "summary": "Traces could not be read; the cause is not established.",
            "claims": [
                {
                    "kind": "fact",
                    "text": "Checkout PlaceOrder errors rose in the window.",
                    "evidence_ids": ok[:1],
                    "target_refs": [TARGET_ID],
                    "time_scope_ref": "policy-window-1",
                }
            ],
            "gaps": ["Jaeger could not be reached."],
            "next_steps": ["Restore the trace source and rerun."],
        }
        return assistant(content=json.dumps(payload))

    provider = ScriptedProvider(
        [
            assistant(
                tool_calls=[
                    fn("k1", METRICS_TOOL, {"expr": ERROR_EXPR}),
                    fn("k2", METRICS_TOOL, {"expr": BAD_EXPR}),
                    fn("k3", METRICS_TOOL, {"expr": DOWN_EXPR}),
                    fn("k4", METRICS_TOOL, {"expr": SLOW_EXPR}),
                    fn("k5", METRICS_TOOL, {"expr": EMPTY_EXPR}),
                    fn("k6", TRACES_TOOL, {"service": "ad"}),
                ]
            ),
            report,
        ]
    )
    _run_worker(env, incident, provider)
    with psycopg.connect(env["dsn"], row_factory=dict_row) as conn:
        conn.read_only = True
        projection = investigation_evidence(conn, incident)
        conn.rollback()
    return {"incident": incident, "projection": projection}


def test_step4_input_connectivity_timeout_and_no_data_are_distinguished(
    four_classes,
) -> None:
    sc = scenario(4, "four-classes", str(four_classes["incident"]))
    projection = four_classes["projection"]
    refused = {
        (r.status, r.reason)
        for r in projection.refused_operations
        if r.tool == METRICS_TOOL
    }
    assert ("error", "INVALID_PARAMS") in refused, (sc, refused)
    assert ("error", "SOURCE_UNAVAILABLE") in refused, (sc, refused)
    assert any(s == "timeout" for s, _ in refused), (sc, refused)
    statuses = {o.status for o in projection.observations}
    assert statuses == {"ok", "no_data"}, (sc, statuses)


def test_step3_a_failed_source_is_named_as_a_gap(env, four_classes) -> None:
    sc = scenario(3, "missing-source", str(four_classes["incident"]))
    (coverage,) = four_classes["projection"].coverage
    rows = {s.name: s for s in coverage.sources}
    assert rows["jaeger"].gap is True and rows["jaeger"].state == "failed", (sc, rows)
    page = call(
        env["app"],
        "GET",
        f"/incidents/{four_classes['incident']}",
        headers={**basic(), "accept": "text/html"},
    )
    assert page.status == 200
    assert 'id="coverage-gaps"' in page.text and "jaeger" in page.text, sc


# -- step 6: an unregistered write tool -------------------------------------------


def test_step6_unregistered_write_tool_is_refused_without_an_outbound_request(
    env,
) -> None:
    response = post_form(
        env["app"],
        "/intake/ui",
        {
            "target_id": TARGET_KEY,
            "question": "Why is checkout erroring?",
            "idempotency_key": f"f3-write-{uuid4()}",
        },
        headers=UI,
    )
    assert response.status == 201, response.text
    incident = UUID(response.json()["incident_id"])
    sc = scenario(6, "write-tool", str(incident))
    provider = ScriptedProvider(
        [
            assistant(tool_calls=[fn("w1", WRITE_TOOL, {"match[]": "up"})]),
            assistant(
                content=json.dumps(
                    {
                        "schema_version": "m0-report-v2",
                        "assessment_status": "incomplete",
                        "conclusion": "inconclusive",
                        "summary": "No evidence was gathered.",
                        "claims": [],
                        "gaps": ["The requested tool is not available."],
                        "next_steps": ["Have a human investigate."],
                    }
                )
            ),
        ]
    )
    sent_before = len(env["opener"].requests)
    _run_worker(env, incident, provider)
    assert env["opener"].requests[sent_before:] == [], sc
    with psycopg.connect(env["dsn"], row_factory=dict_row) as conn:
        conn.read_only = True
        projection = investigation_evidence(conn, incident)
        timeline = audit_timeline(conn, incident)
        conn.rollback()
    # The refusal does not echo the unregistered name back as its tool.
    refused = list(projection.refused_operations)
    assert len(refused) == 1 and refused[0].status in {"error", "denied"}, (
        sc,
        projection.refused_operations,
    )
    assert projection.observations == (), sc
    outbound = [
        e
        for e in timeline.entries
        if e.category == "query" and e.detail.get("outbound")
    ]
    assert outbound == [], sc
    outcome = _outcome(env, incident, sc)
    assert outcome.report_available is False, outcome


# -- step 1 legacy: old projection revision, READ ONLY ----------------------------


def test_step1_legacy_view_lists_missing_fields_in_a_read_only_transaction(
    env, four_classes
) -> None:
    sc = scenario(1, "legacy-view", str(four_classes["incident"]))
    incident = four_classes["incident"]
    ok = next(o for o in four_classes["projection"].observations if o.status == "ok")
    with psycopg.connect(env["dsn"]) as conn:
        # Strip a field and mark the row with an older projection revision,
        # as a pre-v10 row would read. The view hash then no longer matches.
        conn.execute(
            "UPDATE opspilot_evidence SET view=view-'lookback_start_at'-'lookback_seconds',"
            " projection_revision='m1-01-tool-view-v1' WHERE evidence_id=%s",
            (ok.evidence_id,),
        )
    with psycopg.connect(env["dsn"], row_factory=dict_row) as conn:
        conn.read_only = True
        before = conn.execute(
            "SELECT md5(string_agg(view::text, ',' ORDER BY evidence_id)) AS h"
            " FROM opspilot_evidence WHERE subject_id=%s",
            (str(incident),),
        ).fetchone()["h"]
        projection = investigation_evidence(conn, incident)
        after = conn.execute(
            "SELECT md5(string_agg(view::text, ',' ORDER BY evidence_id)) AS h"
            " FROM opspilot_evidence WHERE subject_id=%s",
            (str(incident),),
        ).fetchone()["h"]
        conn.rollback()
    legacy = next(o for o in projection.observations if o.evidence_id == ok.evidence_id)
    assert set(legacy.missing_fields) >= {"lookback_seconds", "lookback_start_at"}, sc
    assert before == after, sc
