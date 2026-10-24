"""F3 step 3 (PR-2) on the product path: coverage-gap section and truncation.

Independent acceptance tests from the contract pre-review
``~/.agent/handoffs/opspilot-prereview-f3-pr2-2026-10-24.md`` section 7,
items 1-16 (item 17's revision guard is in
``tests/acceptance/test_f3_step3_coverage.py``; its "no coverage text in the
model request" half is asserted here). D1-D9 as recommended, R-1..R-16.

Each test submits an incident through the real web intake, runs one worker
attempt with the real ``InvestigationRunner`` + ``DeepSeekClient`` whose HTTPS
opener is a scripted provider, against Prometheus/Jaeger served by one local
HTTP server through the production urllib transport (urllib is not mocked;
"Jaeger refused" is a loopback port nobody listens on). It then reads the
two public surfaces: the incident page (``#coverage-gaps``,
``#collection-truncated``, ``#not-integrated``) and
``investigation_evidence(conn, incident_id)`` in a READ ONLY transaction.
Before the coverage assertions each test checks, through the store's
``rebuild``, that the scenario produced the stored results it is about, so a
red test on ``main`` is the missing feature and not a broken scenario.

ASSUMED beyond the contract (also listed in the acceptance module):

* page markup/wording: see the acceptance module's docstring;
* ``RunCoverage.run_id`` and ``ReportClaims.run_id`` compare equal to the Run
  id by ``str()``; ``SourceCoverage.reasons`` is a mapping reason -> count that
  lists only non-ok reasons;
* a fixture-profile ``not_integrated`` entry (``traces``) and the otel ones
  (``kubernetes_state``, ``pod_logs``, ``change_history``) appear as
  ``SourceCoverage`` entries named exactly like the recorded entry;
* a Run without ``data_sources`` names its rows by tool name;
* the internal ``read_evidence`` row (R-4) is a ``SourceCoverage`` whose name
  is neither ``prometheus`` nor ``jaeger``;
* "Prometheus timeout" is produced by Prometheus's own ``503`` +
  ``errorType=timeout`` answer (classified ``timeout/TOOL_TIMEOUT``) instead of
  a 30-second socket hang -- same reason code, no 30 s wait;
* ``TOOL_NOT_IN_SCOPE`` cannot be produced on the product path (the otel
  executor's scope is every registered tool, pre-review section 1 item 3); the
  "denied result counted on its source" half of item 6 is not covered here.

Run with ``M1_DURABLE_POSTGRES=1`` and ``OPSPILOT_LAB_DSN``; a scratch
database is created and dropped.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
from collections.abc import Callable, Iterator
from io import BytesIO
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from opspilot.acceptance_investigation import investigation_evidence
from opspilot.investigation.inputs import ToolFace
from opspilot.knowledge import KnowledgeStore
from opspilot.persistence import DurableStore
from opspilot.tools.fixture import FIXTURE_EXPR, FIXTURE_TARGET, FIXTURE_TOOL
from opspilot.tools.otel_demo import (
    METRICS_TOOL,
    TARGET_ID,
    TRACES_TOOL,
    otel_demo_face,
)
from opspilot.tools.profiles import PROFILE_ENV, select_profile
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
from tests.acceptance.f7_hostile_support import jaeger_payload, prometheus_payload
from tests.acceptance.f7_pg_support import (
    POOL,
    UI,
    Harness,
    ScriptedProvider,
    _auth,
    assistant,
    fn,
    scratch_database,
    tool_views,
)
from tests.acceptance.test_308_pr2_read_evidence import _matrix, _series_rows
from tests.acceptance.test_311_source_errors import _SourceStub
from tests.acceptance.test_f3_step3_coverage import (
    HEALTHY_WORDING,
    STATES,
    duplicate_count,
    element,
    reason_count,
    row_for,
    state_on_page,
    text,
)
from tests.m1_tool_support import FakeClock
from tests.m1_web_support import post_form
from tests.test_m1_otel_demo_contract import WINDOW_END

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

QUESTION = "Why is checkout erroring?"
OK_EXPR = 'sum(rate(calls_total{service_name="checkout"}[5m]))'
OK_EXPR_2 = 'sum(rate(calls_total{service_name="payment"}[5m]))'
EMPTY_EXPR = 'sum(rate(calls_total{service_name="nothing"}[5m]))'
BAD_EXPR = "sum(rate(http_requests_total[5m])"
TIMEOUT_EXPR = 'sum(rate(calls_total{service_name="slow"}[5m]))'
BIG_EXPR = 'sum by (pod) (rate(calls_total{service_name="checkout"}[5m]))'
EMPTY_MATRIX = _matrix([])
TIMEOUT_BODY = json.dumps(
    {"status": "error", "errorType": "timeout", "error": "query timed out"}
).encode()
EMPTY_TRACES = json.dumps({"data": [], "total": 0, "errors": None}).encode()
NOT_INTEGRATED = ("kubernetes_state", "pod_logs", "change_history")
# Model-visible text a coverage section would leak (item 17, R-14).
COVERAGE_MARKERS = (
    "coverage-gaps",
    "collection-truncated",
    "not queried this Run",
    "not evidence of health",
    "no_data_only",
    "not_queried",
    "partial_failure",
    "duplicates_skipped",
    "collection_truncated",
)


# -- fixtures -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dsn() -> Iterator[str]:
    yield from scratch_database()


@pytest.fixture
def stub() -> Iterator[_SourceStub]:
    server = _SourceStub()
    try:
        yield server
    finally:
        server.close()


def _refused_url() -> str:
    """A loopback port with no listener: a real connection refusal."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/jaeger/ui"


def _serve(
    stub: _SourceStub,
    *,
    metrics: dict[str, tuple[int, bytes]] | None = None,
    traces: dict[str, tuple[int, bytes]] | tuple[int, bytes] | None = None,
) -> None:
    """Prometheus answers by ``query``; Jaeger by ``service`` (or always)."""
    metrics = metrics or {}

    def respond(path: str, query: dict[str, list[str]]):
        if path.endswith("/api/v1/query_range"):
            expr = (query.get("query") or [""])[0]
            return metrics.get(expr, (200, prometheus_payload()))
        if path.endswith("/api/traces"):
            if isinstance(traces, tuple):
                return traces
            service = (query.get("service") or [""])[0]
            return (traces or {}).get(service, (200, jaeger_payload()))
        return 404, b"{}"

    stub.respond = respond


def _harness(
    dsn: str,
    monkeypatch,
    stub: _SourceStub | None,
    *,
    profile_name: str = "otel-demo",
    jaeger_url: str | None = None,
    budget: int | None = None,
    face_filter: Callable[[ToolFace], ToolFace] | None = None,
) -> Harness:
    monkeypatch.delenv("OPSPILOT_OTEL_TOKEN", raising=False)
    env = {PROFILE_ENV: profile_name}
    if profile_name == "otel-demo":
        assert stub is not None
        env["OPSPILOT_OTEL_PROMETHEUS_URL"] = stub.url
        env["OPSPILOT_OTEL_JAEGER_URL"] = jaeger_url or f"{stub.url}/jaeger/ui"
    profile = select_profile(env)
    store = DurableStore(dsn, pool=POOL)
    store.install()
    events = DurableEventLog(store)
    evidence = DurableEvidenceStore(store)
    knowledge = KnowledgeStore(dsn, pool=POOL)
    if profile_name == "otel-demo":
        # As f7_pg_support: the recorded window ends where the stub's bodies
        # answer for; the worker keeps real time.
        face = otel_demo_face(FakeClock(start=WINDOW_END))
    else:
        face = profile.face(DurableClock(store))
    if face_filter is not None:
        face = face_filter(face)
    extra = {} if budget is None else {"budget_limit": budget}
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=evidence,
        ledger=DurableWebLedger(store),
        run_versions=profile.versions(),
        run_seconds=600,
        tool_face=face,
        knowledge=knowledge,
        **extra,
    )
    app = create_app(workbench, Authenticator(_auth()), DurableClock(store))
    return Harness(
        dsn=dsn,
        store=store,
        events=events,
        evidence=evidence,
        knowledge=knowledge,
        app=app,
        workbench=workbench,
        profile=profile,
        closers=[knowledge, store],
    )


@pytest.fixture
def opened() -> Iterator[list[Harness]]:
    harnesses: list[Harness] = []
    try:
        yield harnesses
    finally:
        for harness in harnesses:
            for closer in harness.closers:
                closer.close()


# -- scripted model -------------------------------------------------------------------


def _report_payload(evidence_id: str, target: str, *, incomplete: bool) -> str:
    payload = {
        "schema_version": "m0-report-v2",
        "assessment_status": "incomplete" if incomplete else "completed",
        "conclusion": "inconclusive" if incomplete else "supported",
        "summary": "Checkout calls were read from the authorized source.",
        "claims": [
            {
                "kind": "fact",
                "text": "The authorized read returned checkout data.",
                "evidence_ids": [evidence_id],
                "target_refs": [target],
                "time_scope_ref": "policy-window-1",
            }
        ],
        "gaps": ["Trace evidence was not available for this Run."],
        "next_steps": ["Have a human check the trace backend."],
    }
    return json.dumps(payload)


def report(
    target: str = TARGET_ID, *, incomplete: bool = False
) -> Callable[[list[dict[str, Any]]], dict[str, Any]]:
    """A valid report citing the first adopted ``ok`` view the model received."""

    def build(messages):
        for item in tool_views(messages):
            view = item["view"]
            if view.get("status") == "ok" and view.get("evidence_id"):
                return assistant(
                    content=_report_payload(
                        view["evidence_id"], target, incomplete=incomplete
                    )
                )
        raise AssertionError("scenario delivered no ok view to cite")

    return build


def tools(*calls: dict[str, Any]) -> dict[str, Any]:
    return assistant(tool_calls=list(calls))


def metrics(call_id: str, expr: str) -> dict[str, Any]:
    return fn(call_id, METRICS_TOOL, {"expr": expr})


def traces(call_id: str, service: str) -> dict[str, Any]:
    return fn(call_id, TRACES_TOOL, {"service": service})


def provider_down(_messages):
    raise urllib.error.HTTPError(
        "https://api.deepseek.test/chat/completions",
        503,
        "unavailable",
        {},  # type: ignore[arg-type]
        BytesIO(b'{"error":"overloaded"}'),
    )


# -- observation helpers -------------------------------------------------------------


def projection(dsn: str, incident: UUID):
    """Item 16: always read inside a READ ONLY transaction."""
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.read_only = True
        value = investigation_evidence(conn, incident)
        conn.rollback()
    return value


def run_coverage(value, run: UUID):
    found = [c for c in value.coverage if str(c.run_id) == str(run)]
    assert len(found) == 1, [str(c.run_id) for c in value.coverage]
    return found[0]


def source(coverage, name: str):
    found = [s for s in coverage.sources if s.name == name]
    assert len(found) == 1, [s.name for s in coverage.sources]
    return found[0]


def report_claims(value, run: UUID):
    return [r for r in value.reports if str(r.run_id) == str(run)]


def page(harness: Harness, incident: UUID) -> str:
    response = harness.get(f"/incidents/{incident}", html=True)
    assert response.status == 200, response.text
    return response.text


def coverage_section(page_html: str) -> str:
    section = element(page_html, "coverage-gaps")
    assert section is not None, "page has no #coverage-gaps"
    return section


def stored_results(harness: Harness, incident: UUID) -> list[dict[str, Any]]:
    """Committed (non-late) tool results of the current Run, in order."""
    rebuilt = harness.store.rebuild(incident)
    found = []
    for step in rebuilt["steps"]:
        if step["status"] == "late_result":
            continue
        for item in step.get("tool_results") or []:
            found.append(item["result"])
    return found


def statuses(results: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
    return [(r["status"], r.get("reason")) for r in results]


def conclusion_of(harness: Harness, incident: UUID) -> dict[str, Any]:
    rebuilt = harness.store.rebuild(incident)
    for step in reversed(rebuilt["steps"]):
        response = step.get("response") or {}
        if step["status"] != "late_result" and response.get("kind") == "conclusion":
            return response["conclusion"]
    raise AssertionError("no conclusion step")


def assert_page_matches_projection(section: str, coverage) -> None:
    for item in coverage.sources:
        assert item.state in STATES, item
        assert item.gap is (item.state != "all_ok"), item
        assert state_on_page(section, item.name) == item.state, item.name
        row = row_for(section, item.name)
        for reason, count in item.reasons.items():
            assert reason_count(row, reason) == count, (item.name, reason, row)


def assert_no_coverage_text_sent(provider: ScriptedProvider) -> None:
    sent = b"\n".join(provider.raw_requests).decode("utf-8")
    leaked = [marker for marker in COVERAGE_MARKERS if marker in sent]
    assert leaked == []


# -- items 1, 5, 17: refused Jaeger, healthy Prometheus --------------------------------


def test_items_1_5_17_refused_jaeger_is_failed_and_listed(
    dsn, monkeypatch, stub, opened
):
    _serve(stub)
    harness = _harness(dsn, monkeypatch, stub, jaeger_url=_refused_url())
    opened.append(harness)
    incident, run = harness.submit(QUESTION, target_id=TARGET_ID)
    provider = ScriptedProvider(
        [
            tools(
                traces("t-checkout", "checkout"),
                traces("t-payment", "payment"),
                metrics("m-ok", OK_EXPR),
            ),
            report(),
        ]
    )
    harness.run(incident, provider)

    results = stored_results(harness, incident)
    assert sorted(statuses(results)) == sorted(
        [
            ("error", "SOURCE_UNAVAILABLE"),
            ("error", "SOURCE_UNAVAILABLE"),
            ("ok", None),
        ]
    )
    assert harness.store.rebuild(incident)["conclusion"] is not None  # published
    recorded = harness.store.rebuild(incident)["run"]["input"]["evidence_context"]
    assert tuple(recorded["data_sources"]["not_integrated"]) == NOT_INTEGRATED

    html_page = page(harness, incident)
    section = coverage_section(html_page)
    jaeger_row = row_for(section, "jaeger")
    assert state_on_page(section, "jaeger") == "failed"
    assert reason_count(jaeger_row, "SOURCE_UNAVAILABLE") == 2
    assert state_on_page(section, "prometheus") == "all_ok"

    value = projection(harness.dsn, incident)
    coverage = run_coverage(value, run)
    assert coverage.sources_recorded is True
    jaeger = source(coverage, "jaeger")
    assert (jaeger.ok, jaeger.no_data, jaeger.non_ok) == (0, 0, 2)
    assert dict(jaeger.reasons) == {"SOURCE_UNAVAILABLE": 2}
    assert (jaeger.state, jaeger.gap) == ("failed", True)
    prometheus = source(coverage, "prometheus")
    assert (prometheus.ok, prometheus.no_data, prometheus.non_ok) == (1, 0, 0)
    assert dict(prometheus.reasons) == {}
    assert (prometheus.state, prometheus.gap) == ("all_ok", False)

    # Item 5: the three not-integrated sources are in the section, and the
    # #311 paragraph is still there with its main-branch text.
    for name in NOT_INTEGRATED:
        entry = source(coverage, name)
        assert (entry.state, entry.gap) == ("not_integrated", True)
        assert state_on_page(section, name) == "not_integrated"
    paragraph = element(html_page, "not-integrated")
    assert paragraph is not None
    assert text(paragraph).replace(" ,", ",") == (
        "Not integrated for this Run (cannot be queried): "
        "kubernetes_state, pod_logs, change_history"
    )

    assert_page_matches_projection(section, coverage)
    # Item 17: nothing of the coverage section reached the model.
    assert_no_coverage_text_sent(provider)
    assert text(section) not in b"\n".join(provider.raw_requests).decode()


# -- item 2: 400, timeout and 5xx are counted apart ------------------------------------


def test_item2_input_error_timeout_and_5xx_are_counted_apart(
    dsn, monkeypatch, stub, opened
):
    _serve(
        stub,
        metrics={
            BAD_EXPR: (
                400,
                b'{"status":"error","errorType":"bad_data","error":"parse error"}',
            ),
            TIMEOUT_EXPR: (503, TIMEOUT_BODY),
        },
        traces=(500, b'{"errors":[{"code":500,"msg":"boom"}]}'),
    )
    harness = _harness(dsn, monkeypatch, stub)
    opened.append(harness)
    incident, run = harness.submit(QUESTION, target_id=TARGET_ID)
    provider = ScriptedProvider(
        [
            tools(
                metrics("m-ok", OK_EXPR),
                metrics("m-bad", BAD_EXPR),
                metrics("m-slow", TIMEOUT_EXPR),
                traces("t-down", "checkout"),
            ),
            report(),
        ]
    )
    harness.run(incident, provider)

    assert sorted(statuses(stored_results(harness, incident))) == sorted(
        [
            ("ok", None),
            ("error", "INVALID_PARAMS"),
            ("timeout", "TOOL_TIMEOUT"),
            ("error", "SOURCE_UNAVAILABLE"),
        ]
    )

    coverage = run_coverage(projection(harness.dsn, incident), run)
    prometheus = source(coverage, "prometheus")
    assert (prometheus.ok, prometheus.no_data, prometheus.non_ok) == (1, 0, 2)
    assert dict(prometheus.reasons) == {"INVALID_PARAMS": 1, "TOOL_TIMEOUT": 1}
    assert (prometheus.state, prometheus.gap) == ("partial_failure", True)
    jaeger = source(coverage, "jaeger")
    assert (jaeger.ok, jaeger.no_data, jaeger.non_ok) == (0, 0, 1)
    assert dict(jaeger.reasons) == {"SOURCE_UNAVAILABLE": 1}
    assert (jaeger.state, jaeger.gap) == ("failed", True)

    section = coverage_section(page(harness, incident))
    prometheus_row = row_for(section, "prometheus")
    assert reason_count(prometheus_row, "INVALID_PARAMS") == 1
    assert reason_count(prometheus_row, "TOOL_TIMEOUT") == 1
    assert reason_count(row_for(section, "jaeger"), "SOURCE_UNAVAILABLE") == 1
    assert_page_matches_projection(section, coverage)


# -- items 3, 4: no data only; authorized but not queried -----------------------------


def test_items_3_4_no_data_only_and_not_queried_are_gaps(
    dsn, monkeypatch, stub, opened
):
    _serve(
        stub, metrics={EMPTY_EXPR: (200, EMPTY_MATRIX), OK_EXPR: (200, EMPTY_MATRIX)}
    )
    harness = _harness(dsn, monkeypatch, stub)
    opened.append(harness)
    incident, run = harness.submit(QUESTION, target_id=TARGET_ID)
    provider = ScriptedProvider(
        [
            tools(metrics("m-empty", EMPTY_EXPR), metrics("m-empty-2", OK_EXPR)),
            assistant(
                content=json.dumps(
                    {
                        "schema_version": "m0-report-v2",
                        "assessment_status": "incomplete",
                        "conclusion": "inconclusive",
                        "summary": "No series were returned.",
                        "claims": [],
                        "gaps": ["No data was returned; traces were not read."],
                        "next_steps": ["Have a human check the sources."],
                    }
                )
            ),
        ]
    )
    harness.run(incident, provider)
    assert statuses(stored_results(harness, incident)) == [
        ("no_data", None),
        ("no_data", None),
    ]

    coverage = run_coverage(projection(harness.dsn, incident), run)
    prometheus = source(coverage, "prometheus")
    assert (prometheus.ok, prometheus.no_data, prometheus.non_ok) == (0, 2, 0)
    assert (prometheus.state, prometheus.gap) == ("no_data_only", True)
    jaeger = source(coverage, "jaeger")
    assert (jaeger.ok, jaeger.no_data, jaeger.non_ok) == (0, 0, 0)
    assert (jaeger.state, jaeger.gap) == ("not_queried", True)

    section = coverage_section(page(harness, incident))
    assert state_on_page(section, "prometheus") == "no_data_only"
    assert state_on_page(section, "jaeger") == "not_queried"
    lowered = text(section).lower()
    assert [w for w in HEALTHY_WORDING if w in lowered] == []
    assert_page_matches_projection(section, coverage)


# -- item 6: fixture profile (traces not integrated); tool withheld from the Run ------


def test_item6_fixture_profile_lists_traces_as_not_integrated(dsn, monkeypatch, opened):
    harness = _harness(dsn, monkeypatch, None, profile_name="fixture")
    opened.append(harness)
    incident, run = harness.submit(QUESTION, target_id=FIXTURE_TARGET)
    provider = ScriptedProvider(
        [
            tools(
                fn("f-ok", FIXTURE_TOOL, {"expr": FIXTURE_EXPR}),
                fn("f-bad", FIXTURE_TOOL, {}),  # schema refusal, before any source read
            ),
            report(FIXTURE_TARGET),
        ]
    )
    harness.run(incident, provider)
    results = stored_results(harness, incident)
    assert [r["status"] for r in results][0] == "ok"
    failed = [r for r in results if r["status"] != "ok"]
    assert len(failed) == 1
    failed_reason = failed[0]["reason"]
    recorded = harness.store.rebuild(incident)["run"]["input"]["evidence_context"]
    assert "traces" in recorded["data_sources"]["not_integrated"]

    coverage = run_coverage(projection(harness.dsn, incident), run)
    prometheus = source(coverage, "prometheus")
    assert (prometheus.ok, prometheus.non_ok) == (1, 1)
    assert dict(prometheus.reasons) == {failed_reason: 1}
    assert (prometheus.state, prometheus.gap) == ("partial_failure", True)
    for name in recorded["data_sources"]["not_integrated"]:
        entry = source(coverage, name)
        assert (entry.state, entry.gap) == ("not_integrated", True)

    section = coverage_section(page(harness, incident))
    assert state_on_page(section, "traces") == "not_integrated"
    assert_page_matches_projection(section, coverage)


def _without_traces(face: ToolFace) -> ToolFace:
    schemas = tuple(
        s for s in face.tool_schemas if s["function"]["name"] != TRACES_TOOL
    )
    assert len(schemas) == len(face.tool_schemas) - 1
    return ToolFace(
        tool_schemas=schemas,
        variant_id=face.variant_id,
        evidence_context=face.evidence_context,
    )


def test_item6_source_with_no_tool_in_the_run_is_not_authorized(
    dsn, monkeypatch, stub, opened
):
    _serve(stub)
    harness = _harness(dsn, monkeypatch, stub, face_filter=_without_traces)
    opened.append(harness)
    incident, run = harness.submit(QUESTION, target_id=TARGET_ID)
    recorded = harness.store.rebuild(incident)["run"]["input"]
    assert TRACES_TOOL not in [s["function"]["name"] for s in recorded["tool_schemas"]]
    assert (
        TRACES_TOOL
        in recorded["evidence_context"]["data_sources"]["available"]["jaeger"]
    )
    provider = ScriptedProvider([tools(metrics("m-ok", OK_EXPR)), report()])
    harness.run(incident, provider)

    coverage = run_coverage(projection(harness.dsn, incident), run)
    jaeger = source(coverage, "jaeger")
    assert (jaeger.state, jaeger.gap) == ("not_authorized", True)
    assert source(coverage, "prometheus").state == "all_ok"
    section = coverage_section(page(harness, incident))
    assert state_on_page(section, "jaeger") == "not_authorized"
    assert_page_matches_projection(section, coverage)


# -- item 7: B5 duplicates ----------------------------------------------------------------


def test_item7_duplicate_call_is_not_a_source_failure(dsn, monkeypatch, stub, opened):
    _serve(stub)
    harness = _harness(dsn, monkeypatch, stub)
    opened.append(harness)
    incident, run = harness.submit(QUESTION, target_id=TARGET_ID)
    provider = ScriptedProvider(
        [
            tools(metrics("m-1", OK_EXPR)),
            tools(metrics("m-2", OK_EXPR)),
            report(),
        ]
    )
    harness.run(incident, provider)
    assert statuses(stored_results(harness, incident)) == [
        ("ok", None),
        ("denied", "DUPLICATE_TOOL_CALL"),
    ]

    coverage = run_coverage(projection(harness.dsn, incident), run)
    assert coverage.duplicates_skipped == 1
    prometheus = source(coverage, "prometheus")
    assert (prometheus.ok, prometheus.non_ok) == (1, 0)
    assert (prometheus.state, prometheus.gap) == ("all_ok", False)
    assert all("DUPLICATE_TOOL_CALL" not in s.reasons for s in coverage.sources)
    assert all(s.non_ok == 0 for s in coverage.sources)

    section = coverage_section(page(harness, incident))
    assert duplicate_count(section) == 1
    assert "DUPLICATE_TOOL_CALL" not in text(section)
    assert_page_matches_projection(section, coverage)


# -- item 8: read_evidence --------------------------------------------------------------


def _read_first_spill(messages):
    for item in tool_views(messages):
        view = item["view"]
        if view.get("reason") == "RESULT_TOO_LARGE":
            return tools(
                fn(
                    "read-1",
                    "read_evidence",
                    {"evidence_id": view["evidence_id"], "offset": 0, "limit": 5},
                )
            )
    raise AssertionError("no RESULT_TOO_LARGE view delivered")


def test_item8_read_evidence_fragment_counts_for_its_parent_source(
    dsn, monkeypatch, stub, opened
):
    _serve(stub, metrics={BIG_EXPR: (200, _matrix(_series_rows(400)))})
    harness = _harness(dsn, monkeypatch, stub)
    opened.append(harness)
    incident, run = harness.submit(QUESTION, target_id=TARGET_ID)
    provider = ScriptedProvider(
        [tools(metrics("m-big", BIG_EXPR)), _read_first_spill, report()]
    )
    harness.run(incident, provider)
    results = stored_results(harness, incident)
    assert statuses(results) == [("error", "RESULT_TOO_LARGE"), ("ok", None)]
    assert results[1].get("parent_tool") == METRICS_TOOL

    coverage = run_coverage(projection(harness.dsn, incident), run)
    prometheus = source(coverage, "prometheus")
    assert (prometheus.ok, prometheus.non_ok) == (1, 1)
    assert dict(prometheus.reasons) == {"RESULT_TOO_LARGE": 1}
    assert prometheus.state == "partial_failure"
    section = coverage_section(page(harness, incident))
    assert_page_matches_projection(section, coverage)


def test_item8_unreadable_evidence_is_its_own_row_not_a_prometheus_gap(
    dsn, monkeypatch, stub, opened
):
    _serve(stub)
    harness = _harness(dsn, monkeypatch, stub)
    opened.append(harness)
    incident, run = harness.submit(QUESTION, target_id=TARGET_ID)
    bogus = f"step-x-t0:{uuid4()}"
    provider = ScriptedProvider(
        [
            tools(
                metrics("m-ok", OK_EXPR),
                fn(
                    "read-bogus",
                    "read_evidence",
                    {"evidence_id": bogus, "offset": 0, "limit": 5},
                ),
            ),
            report(),
        ]
    )
    harness.run(incident, provider)
    assert sorted(statuses(stored_results(harness, incident))) == sorted(
        [("ok", None), ("denied", "EVIDENCE_NOT_READABLE")]
    )

    coverage = run_coverage(projection(harness.dsn, incident), run)
    prometheus = source(coverage, "prometheus")
    assert (prometheus.ok, prometheus.non_ok) == (1, 0)
    assert (prometheus.state, prometheus.gap) == ("all_ok", False)
    internal = [s for s in coverage.sources if "EVIDENCE_NOT_READABLE" in s.reasons]
    assert len(internal) == 1
    assert internal[0].name not in {"prometheus", "jaeger"}
    assert internal[0].gap is False
    assert dict(internal[0].reasons) == {"EVIDENCE_NOT_READABLE": 1}

    section = coverage_section(page(harness, incident))
    assert state_on_page(section, "prometheus") == "all_ok"
    assert (
        reason_count(row_for(section, internal[0].name), "EVIDENCE_NOT_READABLE") == 1
    )


# -- item 9: late results ---------------------------------------------------------------


def test_item9_late_result_is_not_counted(dsn, monkeypatch, stub, opened):
    _serve(stub, traces=(500, b'{"errors":[{"code":500,"msg":"boom"}]}'))
    harness = _harness(dsn, monkeypatch, stub)
    opened.append(harness)
    incident, run = harness.submit(QUESTION, target_id=TARGET_ID)
    cancelled: list[int] = []

    def cancel_once():
        if cancelled or not any("/api/traces" in p for p in stub.requests):
            return
        response = post_form(
            harness.app,
            f"/incidents/{incident}/control",
            {
                "action": "cancel",
                "expected_generation": "0",
                "idempotency_key": f"cancel-{uuid4()}",
            },
            headers=UI,
        )
        cancelled.append(response.status)

    stub.on_request = cancel_once
    provider = ScriptedProvider(
        [
            tools(metrics("m-ok", OK_EXPR)),
            tools(traces("t-late", "checkout")),
            report(),
        ]
    )
    harness.run(incident, provider)
    assert cancelled == [200]
    rebuilt = harness.store.rebuild(incident)
    late = [s for s in rebuilt["steps"] if s["status"] == "late_result"]
    assert late, "the traces result did not land as a late result"
    assert statuses(stored_results(harness, incident)) == [("ok", None)]

    coverage = run_coverage(projection(harness.dsn, incident), run)
    jaeger = source(coverage, "jaeger")
    assert (jaeger.ok, jaeger.no_data, jaeger.non_ok) == (0, 0, 0)
    assert dict(jaeger.reasons) == {}
    assert jaeger.state == "not_queried"
    section = coverage_section(page(harness, incident))
    assert state_on_page(section, "jaeger") == "not_queried"
    assert_page_matches_projection(section, coverage)


# -- item 10: in progress, handed off, history ----------------------------------------


def test_item10_section_in_every_state_and_only_for_the_current_run(
    dsn, monkeypatch, stub, opened
):
    _serve(stub, traces=(500, b'{"errors":[{"code":500,"msg":"boom"}]}'))
    harness = _harness(dsn, monkeypatch, stub)
    opened.append(harness)
    incident, first_run = harness.submit(QUESTION, target_id=TARGET_ID)

    # Run 1: Jaeger fails, Prometheus ok, published.
    first = ScriptedProvider(
        [tools(traces("t-down", "checkout"), metrics("m-ok", OK_EXPR)), report()]
    )
    harness.run(incident, first)
    assert harness.store.rebuild(incident)["conclusion"] is not None
    published = coverage_section(page(harness, incident))
    assert state_on_page(published, "jaeger") == "failed"

    # A follow-up on the concluded incident starts Run 2.
    follow = post_form(
        harness.app,
        f"/incidents/{incident}/control",
        {
            "action": "follow_up",
            "expected_generation": "0",
            "idempotency_key": f"follow-{uuid4()}",
            "text": "Look again at checkout only.",
        },
        headers=UI,
    )
    assert follow.status == 200, follow.text
    second_run = UUID(follow.json()["run_id"])
    assert second_run != first_run

    in_progress: list[str] = []

    def look_then_hand_off(messages):
        in_progress.append(page(harness, incident))
        return report(incomplete=True)(messages)

    second = ScriptedProvider(
        [tools(metrics("m-again", OK_EXPR_2)), look_then_hand_off]
    )
    harness.run(incident, second)
    rebuilt = harness.store.rebuild(incident)
    assert str(rebuilt["run"]["run_id"]) == str(second_run)
    assert rebuilt["conclusion"] is None
    assert conclusion_of(harness, incident)["handoff"] is True

    # In progress (Run 2 running, its first round committed).
    assert len(in_progress) == 1
    running = coverage_section(in_progress[0])
    assert state_on_page(running, "prometheus") == "all_ok"
    assert state_on_page(running, "jaeger") == "not_queried"

    # Handed off: the handoff report is shown, and so is the section.
    handed_off_page = page(harness, incident)
    assert element(handed_off_page, "handoff-report") is not None
    handed_off = coverage_section(handed_off_page)
    # Only the current Run: Run 1's Jaeger failure is not on this page.
    assert state_on_page(handed_off, "jaeger") == "not_queried"
    assert reason_count(row_for(handed_off, "jaeger"), "SOURCE_UNAVAILABLE") is None

    value = projection(harness.dsn, incident)
    assert {str(c.run_id) for c in value.coverage} >= {str(first_run), str(second_run)}
    old = source(run_coverage(value, first_run), "jaeger")
    assert (old.non_ok, old.state) == (1, "failed")
    new = run_coverage(value, second_run)
    assert source(new, "jaeger").state == "not_queried"
    assert (source(new, "prometheus").ok, source(new, "prometheus").state) == (
        1,
        "all_ok",
    )
    assert_page_matches_projection(handed_off, new)


# -- items 11-14: truncation at the limit -----------------------------------------------


def test_item11_forced_final_round_at_the_limit_is_truncated(
    dsn, monkeypatch, stub, opened
):
    _serve(stub)
    harness = _harness(dsn, monkeypatch, stub, budget=3)
    opened.append(harness)
    incident, run = harness.submit(QUESTION, target_id=TARGET_ID)
    provider = ScriptedProvider(
        [
            tools(metrics("m-1", OK_EXPR)),
            tools(metrics("m-2", OK_EXPR_2)),
            report(),
        ]
    )
    harness.run(incident, provider)
    assert len(provider.requests) == 3
    assert "tools" in provider.requests[1] and "tools" not in provider.requests[2]
    # Published as usual: an incident conclusion, no handoff reason added.
    assert harness.store.rebuild(incident)["conclusion"] is not None
    conclusion = conclusion_of(harness, incident)
    assert (conclusion["handoff"], conclusion["handoff_reasons"]) == (False, [])

    assert element(page(harness, incident), "collection-truncated") is not None
    claims = report_claims(projection(harness.dsn, incident), run)
    assert len(claims) == 1
    assert claims[0].collection_truncated is True


def test_item12_report_retry_final_round_is_not_truncated(
    dsn, monkeypatch, stub, opened
):
    _serve(stub)
    harness = _harness(dsn, monkeypatch, stub, budget=3)
    opened.append(harness)
    incident, run = harness.submit(QUESTION, target_id=TARGET_ID)
    provider = ScriptedProvider(
        [
            tools(metrics("m-1", OK_EXPR)),
            assistant(content="I think checkout is fine."),  # L1a: invalid, no tools
            report(),
        ]
    )
    harness.run(incident, provider)
    assert len(provider.requests) == 3
    assert "tools" in provider.requests[1] and "tools" not in provider.requests[2]
    assert harness.store.rebuild(incident)["conclusion"] is not None

    assert element(page(harness, incident), "collection-truncated") is None
    claims = report_claims(projection(harness.dsn, incident), run)
    assert len(claims) == 1
    assert claims[0].collection_truncated is False


def test_item13_report_before_the_limit_is_not_truncated(
    dsn, monkeypatch, stub, opened
):
    _serve(stub)
    harness = _harness(dsn, monkeypatch, stub, budget=5)
    opened.append(harness)
    incident, run = harness.submit(QUESTION, target_id=TARGET_ID)
    provider = ScriptedProvider([tools(metrics("m-1", OK_EXPR)), report()])
    harness.run(incident, provider)
    assert len(provider.requests) == 2
    assert harness.store.rebuild(incident)["conclusion"] is not None

    assert element(page(harness, incident), "collection-truncated") is None
    claims = report_claims(projection(harness.dsn, incident), run)
    assert len(claims) == 1
    assert claims[0].collection_truncated is False


def test_item14_budget_exhausted_without_report_has_no_annotation(
    dsn, monkeypatch, stub, opened
):
    _serve(stub)
    harness = _harness(dsn, monkeypatch, stub, budget=2)
    opened.append(harness)
    incident, run = harness.submit(QUESTION, target_id=TARGET_ID)
    provider = ScriptedProvider(
        [tools(metrics("m-1", OK_EXPR)), provider_down, provider_down]
    )
    harness.run(incident, provider)
    assert harness.store.rebuild(incident)["conclusion"] is None
    conclusion = conclusion_of(harness, incident)
    assert conclusion["handoff"] is True
    assert conclusion["handoff_reasons"] == ["BUDGET_EXHAUSTED"]

    html_page = page(harness, incident)
    assert element(html_page, "collection-truncated") is None
    value = projection(harness.dsn, incident)
    assert report_claims(value, run) == []
    # The section is still there for the handed-off Run.
    coverage = run_coverage(value, run)
    assert source(coverage, "prometheus").state == "all_ok"
    assert_page_matches_projection(coverage_section(html_page), coverage)


# -- item 15: a Run recorded before data_sources / context.final ------------------------


def test_item15_old_run_rows_by_tool_name_and_unknown_truncation(
    dsn, monkeypatch, stub, opened
):
    _serve(stub)
    harness = _harness(dsn, monkeypatch, stub)
    opened.append(harness)
    incident, run = harness.submit(QUESTION, target_id=TARGET_ID)
    provider = ScriptedProvider(
        [tools(metrics("m-ok", OK_EXPR), traces("t-ok", "checkout")), report()]
    )
    harness.run(incident, provider)
    assert sorted(statuses(stored_results(harness, incident))) == [
        ("ok", None),
        ("ok", None),
    ]
    # Make the rows look like a Run recorded before #311 / before
    # ``context.final`` existed (owner connection, test-only rewrite).
    with psycopg.connect(harness.dsn) as conn:
        conn.execute(
            "UPDATE opspilot_runs SET input = input #- "
            "'{evidence_context,data_sources}' WHERE run_id=%s",
            (run,),
        )
        conn.execute(
            "UPDATE opspilot_steps SET response = response #- '{context,final}' "
            "WHERE run_id=%s AND response ? 'context'",
            (run,),
        )
    rebuilt = harness.store.rebuild(incident)
    assert "data_sources" not in rebuilt["run"]["input"]["evidence_context"]
    assert all(
        "final" not in ((s.get("response") or {}).get("context") or {})
        for s in rebuilt["steps"]
    )

    html_page = page(harness, incident)
    assert element(html_page, "collection-truncated") is None
    assert element(html_page, "not-integrated") is None
    value = projection(harness.dsn, incident)
    claims = report_claims(value, run)
    assert len(claims) == 1 and claims[0].collection_truncated is None

    coverage = run_coverage(value, run)
    assert coverage.sources_recorded is False
    names = {s.name for s in coverage.sources}
    assert {METRICS_TOOL, TRACES_TOOL} <= names
    assert all(
        s.state not in {"not_authorized", "not_integrated"} for s in coverage.sources
    )
    assert source(coverage, METRICS_TOOL).ok == 1
    assert source(coverage, TRACES_TOOL).ok == 1
    section = coverage_section(html_page)
    assert state_on_page(section, METRICS_TOOL) == "all_ok"
    assert_page_matches_projection(section, coverage)


# -- item 16: read-only and unknown incidents --------------------------------------------


def test_item16_projection_is_read_only_and_refuses_unknown_incidents(dsn):
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.read_only = True
        with pytest.raises(LookupError):
            investigation_evidence(conn, uuid4())
        conn.rollback()
