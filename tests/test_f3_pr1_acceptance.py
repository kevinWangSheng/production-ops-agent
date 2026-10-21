"""Independent acceptance tests for F3 PR-1 (steps 1, 2 and 4), no PostgreSQL.

Written by a fresh-context author who did not implement PR-1, from:

* ``feature_list.json`` F3 step 4 -- "Inject invalid tool parameters and
  verify input errors, connectivity failures, timeout and no-data results are
  distinguished" -- and steps 1 and 2 where the workbench is their surface;
* the F3 contract pre-review (2026-10-17, ruled r1 by the user), section
  seven: the four-class mapping (point 4), "the four classes must be
  distinguishable in the model view, the audit query entries and the
  workbench step table" (point 5), U8-a (a Prometheus 503 whose error body
  says ``errorType: timeout`` is a timeout, ``source_contact=confirmed``; a
  body that cannot be parsed after the ``ERROR_BODY_BYTES`` cut stays
  ``SOURCE_UNAVAILABLE``), R6 (real urllib transport against a local HTTP
  stub; an unknown metric name is no data) and R7 (the workbench step table
  shows ``reason`` for a non-ok result);
* public interfaces only: ``otel_demo_executor_factory`` /
  ``OtelDemoConfig`` / ``ToolRequest`` / ``ToolOutcome`` and the workbench
  pages served by ``create_app``.

The acceptance-projection side of step 4 (audit / ``RefusedOperation``) and
the step 1/2 projection (``investigation_evidence``) need PostgreSQL; they are
in ``tests/integration/test_f3_pr1_projection_acceptance_postgres.py``.

Assertions are on observable outcomes (status, reason, source contact, what
the page shows), never on call order inside the executor.
"""

from __future__ import annotations

import html
import json
import re
import socket
import threading
from collections.abc import Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from opspilot.tools import ReadOnlyToolExecutor
from opspilot.tools.otel_demo import (
    ERROR_BODY_BYTES,
    METRICS_TOOL,
    OtelDemoConfig,
    otel_demo_executor_factory,
)
from tests.f3_pr1_support import (
    CLAIM_TEXT,
    EXPECTED,
    EXPRS,
    INVALID_EXPR,
    NO_DATA_EXPR,
    OK_EXPR,
    TIMEOUT_EXPR,
    UNREACHABLE_EXPR,
    FiveClassInvestigator,
)
from tests.m1_tool_support import RecordingSink
from tests.m1_web_support import basic, build_workbench, call, submit_incident
from tests.test_m1_otel_demo_contract import (
    CHECKOUT_BODY,
    EMPTY_BODY,
    GOOD_EXPR,
    JAEGER_URL,
    NOW,
    FakeClock,
    FakeStore,
    _call,
    _input,
    _lease,
)

# -- a local Prometheus stub over a real socket (pre-review R6) ---------------

#: Prometheus HTTP API error bodies. ``errorType`` values follow the
#: Prometheus API's documented error types (pre-review section eight: the
#: "query timeout is 503" behaviour itself was not re-checked online).
TIMEOUT_503 = json.dumps(
    {
        "status": "error",
        "errorType": "timeout",
        "error": "query timed out in expression evaluation",
    }
).encode()
UNAVAILABLE_503 = json.dumps(
    {"status": "error", "errorType": "unavailable", "error": "too many queries"}
).encode()
INTERNAL_500 = json.dumps(
    {"status": "error", "errorType": "internal", "error": "storage failure"}
).encode()
BAD_DATA_400 = json.dumps(
    {
        "status": "error",
        "errorType": "bad_data",
        "error": 'invalid parameter "query": 1:8: parse error',
    }
).encode()
EXEC_422 = json.dumps(
    {"status": "error", "errorType": "execution", "error": "expanding series"}
).encode()
#: A timeout body whose ``errorType`` only appears after the transport's
#: ``ERROR_BODY_BYTES`` cut: what survives the cut is not parseable JSON, so
#: U8-a's fail-closed rule keeps it ``SOURCE_UNAVAILABLE``.
TRUNCATED_TIMEOUT_503 = (
    b'{"status":"error","error":"'
    + b"x" * (ERROR_BODY_BYTES + 512)
    + b'","errorType":"timeout"}'
)
NOT_JSON_503 = b"<html><body>503 Service Unavailable</body></html>"

UNKNOWN_METRIC_EXPR = "sum(rate(no_such_metric_anywhere_total[5m]))"
EMPTY_EXPR = 'sum(rate(traces_span_metrics_calls_total{case="empty"}[1m]))'
HANG_EXPR = 'sum(rate(traces_span_metrics_calls_total{case="hang"}[1m]))'
TIMEOUT_503_EXPR = 'sum(rate(traces_span_metrics_calls_total{case="timeout"}[1m]))'
UNAVAILABLE_503_EXPR = (
    'sum(rate(traces_span_metrics_calls_total{case="unavailable"}[1m]))'
)
INTERNAL_500_EXPR = 'sum(rate(traces_span_metrics_calls_total{case="internal"}[1m]))'
TRUNCATED_503_EXPR = 'sum(rate(traces_span_metrics_calls_total{case="truncated"}[1m]))'
NOT_JSON_503_EXPR = 'sum(rate(traces_span_metrics_calls_total{case="not_json"}[1m]))'
BAD_DATA_EXPR = 'sum(rate(traces_span_metrics_calls_total{case="bad_data"}[1m])'
EXEC_422_EXPR = 'sum(rate(traces_span_metrics_calls_total{case="exec"}[1m]))'

#: expr -> (HTTP status, body). ``HANG_EXPR`` never answers in time.
ROUTES: dict[str, tuple[int, bytes]] = {
    GOOD_EXPR: (200, CHECKOUT_BODY),
    UNKNOWN_METRIC_EXPR: (200, EMPTY_BODY),
    EMPTY_EXPR: (200, EMPTY_BODY),
    TIMEOUT_503_EXPR: (503, TIMEOUT_503),
    UNAVAILABLE_503_EXPR: (503, UNAVAILABLE_503),
    INTERNAL_500_EXPR: (500, INTERNAL_500),
    TRUNCATED_503_EXPR: (503, TRUNCATED_TIMEOUT_503),
    NOT_JSON_503_EXPR: (503, NOT_JSON_503),
    BAD_DATA_EXPR: (400, BAD_DATA_400),
    EXEC_422_EXPR: (422, EXEC_422),
}


class _Prometheus(BaseHTTPRequestHandler):
    release: threading.Event
    seen: list[tuple[str, str]]

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parts = urlsplit(self.path)
        expr = parse_qs(parts.query).get("query", [""])[0]
        self.seen.append((self.command, parts.path))
        if parts.path != "/api/v1/query_range":
            self.send_error(404)
            return
        if expr == HANG_EXPR:
            # Hold the connection past the client's timeout; released at
            # teardown so the server thread can exit.
            self.release.wait(10)
            return
        status, payload = ROUTES[expr]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:  # keep test output quiet
        return


@pytest.fixture
def prometheus() -> Iterator[dict[str, Any]]:
    release = threading.Event()
    seen: list[tuple[str, str]] = []
    handler = type("PrometheusStub", (_Prometheus,), {"release": release, "seen": seen})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"url": f"http://127.0.0.1:{server.server_port}", "seen": seen}
    finally:
        release.set()
        server.shutdown()
        server.server_close()


def _closed_port_url() -> str:
    """A loopback URL nothing listens on: connection refused, not a timeout."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _real_executor(
    prometheus_url: str, *, deadline_seconds: float = 600.0
) -> ReadOnlyToolExecutor:
    """The shipped OTel profile executor with its real urllib opener.

    Unlike ``tests.test_m1_otel_demo_contract._executor`` nothing is
    monkeypatched: requests cross a real loopback socket. The scope deadline
    (``FakeStore.deadline``) bounds the per-call timeout, which is how the
    hang case times out in about a second.
    """
    store = FakeStore(_lease(), deadline=NOW + timedelta(seconds=deadline_seconds))
    factory = otel_demo_executor_factory(
        store,
        evidence=RecordingSink(),
        clock=FakeClock(start=NOW),
        config=OtelDemoConfig(prometheus_url=prometheus_url, jaeger_url=JAEGER_URL),
    )
    executor = factory(store.lease, _input(str(store.lease.run_id)))
    assert isinstance(executor, ReadOnlyToolExecutor)
    return executor


def _metrics(executor: ReadOnlyToolExecutor, expr: str):
    return executor.execute(_call(METRICS_TOOL, {"expr": expr}))


# -- step 4: each class through the real transport ----------------------------


def test_input_error_400_is_invalid_params_with_confirmed_contact(prometheus):
    outcome = _metrics(_real_executor(prometheus["url"]), BAD_DATA_EXPR)
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert outcome.source_contact == "confirmed"
    assert outcome.evidence is None
    assert outcome.model_view["status"] == "error"
    assert outcome.model_view["reason"] == "INVALID_PARAMS"


def test_execution_error_422_is_invalid_params(prometheus):
    # Pre-review section seven, point 4: "PromQL 400/422" are input errors.
    outcome = _metrics(_real_executor(prometheus["url"]), EXEC_422_EXPR)
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    # The source itself said 422; not a local pre-dispatch refusal.
    assert outcome.source_contact == "confirmed" and prometheus["seen"]


def test_schema_and_window_input_errors_are_invalid_params_without_a_request(
    prometheus,
):
    # Point 4: "schema 不合 ... 窗口外" are input errors too; they never leave.
    executor = _real_executor(prometheus["url"])
    offset = _metrics(
        executor, "sum(rate(traces_span_metrics_calls_total[5m] offset 1h))"
    )
    assert (offset.status, offset.reason) == ("error", "INVALID_PARAMS")
    assert offset.source_contact == "none"
    bad_step = executor.execute(
        _call(METRICS_TOOL, {"expr": GOOD_EXPR, "step_seconds": 0})
    )
    assert (bad_step.status, bad_step.reason) == ("error", "INVALID_PARAMS")
    assert prometheus["seen"] == []


def test_connection_refused_is_source_unavailable():
    outcome = _metrics(_real_executor(_closed_port_url()), GOOD_EXPR)
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_UNAVAILABLE")
    assert outcome.evidence is None
    assert outcome.model_view["reason"] == "SOURCE_UNAVAILABLE"


def test_a_source_that_never_answers_is_a_client_timeout(prometheus):
    executor = _real_executor(prometheus["url"], deadline_seconds=1.0)
    outcome = _metrics(executor, HANG_EXPR)
    assert prometheus["seen"], "the request must have reached the stub"
    assert outcome.status == "timeout"
    assert outcome.reason in {"TOOL_TIMEOUT", "GATEWAY_TIMEOUT"}
    assert outcome.evidence is None
    assert outcome.model_view["status"] == "timeout"


def test_prometheus_503_timeout_body_is_a_timeout_not_unavailable(prometheus):
    # U8-a: 503 + errorType=timeout -> timeout/TOOL_TIMEOUT; the source
    # answered, so contact is confirmed.
    outcome = _metrics(_real_executor(prometheus["url"]), TIMEOUT_503_EXPR)
    assert prometheus["seen"], "the request must have reached the stub"
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT")
    assert outcome.source_contact == "confirmed"
    assert outcome.evidence is None
    assert outcome.model_view["status"] == "timeout"
    assert outcome.model_view["reason"] == "TOOL_TIMEOUT"
    # The error body is the source's, not evidence and not model content.
    assert "expression evaluation" not in json.dumps(outcome.model_view)


@pytest.mark.parametrize(
    "expr",
    [UNAVAILABLE_503_EXPR, INTERNAL_500_EXPR, NOT_JSON_503_EXPR, TRUNCATED_503_EXPR],
    ids=["503-unavailable", "500-internal", "503-not-json", "503-timeout-past-cut"],
)
def test_other_server_errors_stay_source_unavailable(prometheus, expr):
    # U8-a: only a parseable 503 timeout body changes class; every other 5xx,
    # and a timeout marker beyond the ERROR_BODY_BYTES cut, fails closed.
    outcome = _metrics(_real_executor(prometheus["url"]), expr)
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_UNAVAILABLE")
    assert outcome.evidence is None
    assert prometheus["seen"], "the request must have reached the stub"


def test_an_empty_result_and_an_unknown_metric_name_are_no_data(prometheus):
    # R6: a syntactically valid query over a metric that does not exist is
    # "no data", not an input error.
    executor = _real_executor(prometheus["url"])
    for expr in (EMPTY_EXPR, UNKNOWN_METRIC_EXPR):
        outcome = _metrics(executor, expr)
        assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA")
        assert outcome.source_contact == "confirmed"
        assert outcome.evidence is not None
        assert outcome.model_view["status"] == "no_data"
        assert outcome.model_view["citable_as_fact"] is False


def test_the_four_classes_are_pairwise_distinct_on_the_real_transport(prometheus):
    executor = _real_executor(prometheus["url"])
    seen = {
        "input": _metrics(executor, BAD_DATA_EXPR),
        "connectivity": _metrics(_real_executor(_closed_port_url()), GOOD_EXPR),
        "timeout": _metrics(executor, TIMEOUT_503_EXPR),
        "no_data": _metrics(executor, UNKNOWN_METRIC_EXPR),
    }
    pairs = {
        name: (o.status, o.reason, o.model_view["status"], o.model_view.get("reason"))
        for name, o in seen.items()
    }
    # Distinct on the outcome itself and on what the model is shown.
    assert len({(s, r) for s, r, _, _ in pairs.values()}) == 4, pairs
    assert len({(vs, vr) for _, _, vs, vr in pairs.values()}) == 4, pairs
    assert pairs["input"][:2] == ("error", "INVALID_PARAMS")
    assert pairs["connectivity"][:2] == ("error", "SOURCE_UNAVAILABLE")
    assert pairs["timeout"][:2] == ("timeout", "TOOL_TIMEOUT")
    assert pairs["no_data"][:2] == ("no_data", "NO_DATA")


def test_every_request_the_stub_received_was_a_get(prometheus):
    executor = _real_executor(prometheus["url"])
    for expr in (GOOD_EXPR, BAD_DATA_EXPR, TIMEOUT_503_EXPR, UNKNOWN_METRIC_EXPR):
        _metrics(executor, expr)
    assert prometheus["seen"]
    assert {method for method, _ in prometheus["seen"]} == {"GET"}


# -- workbench: one Run, five tool results, five report kinds -----------------


def _section(page: str, start: str, end: str) -> str:
    begin = page.index(start)
    return page[begin : page.index(end, begin)]


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment))


@pytest.fixture
def five_class_run() -> dict[str, Any]:
    app, workbench, clock = build_workbench()
    incident = submit_incident(app, key="f3-pr1-five").json()["incident_id"]
    subject = workbench.list_incidents()[0].incident_id
    investigator = FiveClassInvestigator(clock)
    outcome = workbench.run_once(subject, investigator)
    assert outcome is not None
    # The scripted report must have been accepted, or nothing below is about
    # a real report.
    assert outcome.execution == "completed" and outcome.handoff is False, outcome
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
    return {
        "app": app,
        "incident": incident,
        "page": page.text,
        "outcome": outcome,
        "investigator": investigator,
    }


def test_the_step_table_shows_why_each_non_ok_tool_result_failed(five_class_run):
    # R7 + point 5: the operator tells the four classes apart on the
    # workbench step table, not only by the bare status badge.
    steps = _section(
        five_class_run["page"], "<h2>Steps</h2>", "<h2>Investigation history</h2>"
    )
    lines = _text(steps).split("metrics.range_query")[1:]
    assert len(lines) == len(EXPRS), _text(steps)
    by_expr = dict(zip(EXPRS, lines, strict=True))
    reasons = {"INVALID_PARAMS", "SOURCE_UNAVAILABLE", "TOOL_TIMEOUT"}
    for expr in (INVALID_EXPR, UNREACHABLE_EXPR, TIMEOUT_EXPR):
        status, reason = EXPECTED[expr]
        line = by_expr[expr]
        assert status in line, (expr, line)
        assert reason in line, (expr, line)
        assert not (reasons - {reason}) & set(re.findall(r"[A-Z_]{6,}", line)), (
            expr,
            line,
        )
    # No data is told apart by its own status; it carries none of the
    # failure reasons.
    assert "no_data" in by_expr[NO_DATA_EXPR]
    assert not reasons & set(re.findall(r"[A-Z_]{6,}", by_expr[NO_DATA_EXPR]))
    # The ok row shows no failure reason and no placeholder for one.
    assert not reasons & set(re.findall(r"[A-Z_]{6,}", by_expr[OK_EXPR]))
    assert "None" not in by_expr[OK_EXPR]


def test_each_committed_observation_resolves_with_its_provenance(five_class_run):
    # Step 1 on the workbench surface (R1: ok and no_data are observations).
    app, incident = five_class_run["app"], five_class_run["incident"]
    steps = _section(
        five_class_run["page"], "<h2>Steps</h2>", "<h2>Investigation history</h2>"
    )
    evidence_ids = re.findall(rf"/incidents/{incident}/evidence/([^\"<>]+)\"", steps)
    assert len(evidence_ids) == 2, evidence_ids  # ok + no_data only
    for evidence_id in evidence_ids:
        record = call(
            app,
            "GET",
            f"/incidents/{incident}/evidence/{html.unescape(evidence_id)}",
            headers=basic(),
        )
        assert record.status == 200
        payload = record.json()
        assert payload["hashes_verified"] is True
        view = payload["view"]
        assert view["source"] and view["target_id"] and view["tool"]
        assert view["query"] and view["query"]["expr"] in (OK_EXPR, NO_DATA_EXPR)
        assert view["window"]["start"] and view["window"]["end"]
        assert "freshness_seconds" in view and view["observed_at"]
        if view["status"] == "ok":
            assert isinstance(view["freshness_seconds"], (int, float))
        else:
            # R2: unknown freshness is explicit, never a made-up number.
            assert view["status"] == "no_data"
            assert view["data_as_of"] is None and view["freshness_seconds"] is None


def test_the_report_keeps_the_five_claim_kinds_in_separate_groups(five_class_run):
    # Step 2 on the workbench surface: each claim appears under its own
    # heading and under no other.
    page = five_class_run["page"]
    headings = {
        "fact": "Facts (",
        "hypothesis": "Hypotheses (",
        "counter_evidence": "Counter-evidence (",
        "rejected_hypothesis": "Rejected hypotheses (",
        "recommendation": "Recommendations (",
    }
    sections = {}
    for kind, heading in headings.items():
        start = page.index(heading)
        end = page.index("</section>", start)
        sections[kind] = page[start:end]
    for kind, text in CLAIM_TEXT.items():
        for other, fragment in sections.items():
            present = html.escape(text) in fragment or text in fragment
            assert present is (other == kind), (kind, other)
    for kind in headings:
        assert f"{headings[kind]}1)" in page
