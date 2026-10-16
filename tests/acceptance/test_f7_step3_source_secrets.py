"""F7 step 3 (S3c, contract section 3 class 6): secrets in data-source content.

``feature_list.json`` F7 step 3: "Inspect model inputs, evidence exports and
traces and verify execution credentials and access secrets are absent". U7-a
with the r1 supplement: tool views (span-log values, metric label values) go
through ``redact_credentials``, and because the evidence interfaces return
``record.raw`` too, the secret is redacted *before persistence*; assertions
target the stored and exported representation.

Hermetic: the real OTel Demo executor from its profile factory, a fake
``urllib`` opener serving synthetic Jaeger/Prometheus bodies, a recording
evidence sink and the SDK in-memory span exporter. The PostgreSQL, worker,
model-input, web and ``summary.json`` sinks are in
``test_f7_step3_source_secrets_postgres.py``.

Interface relied on (task record "接口（验收测试作者使用）"): the single marker
``opspilot.redaction.REDACTED_CREDENTIAL``; ``EvidenceRecord.raw``/``view``
and ``ToolOutcome.model_view`` free of the declared secret shapes;
``raw_sha256 == sha256(raw)``; a payload without secrets keeps ``raw``
byte-identical to what the transport returned; ``PROJECTION_REVISION`` and
``TOOL_SCHEMA_REVISION`` values.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from opspilot import tracing
from opspilot.investigation.inputs import ToolFace
from opspilot.investigation.loop import DISCIPLINE_VARIANT
from opspilot.persistence import Lease
from opspilot.redaction import REDACTED_CREDENTIAL
from opspilot.tools import ReadOnlyToolExecutor, ToolRequest, otel_demo
from opspilot.tools import fixture as fixture_profile
from opspilot.tools.otel_demo import (
    METRICS_TOOL,
    TARGET_ID,
    TOOL_SCHEMAS,
    TRACES_TOOL,
    OtelDemoConfig,
    OtelDemoTransport,
    otel_demo_executor_factory,
)
from opspilot.tools.outcomes import PROJECTION_REVISION
from tests.acceptance.f7_hostile_support import (
    BOUNDARY_BEARER,
    BOUNDARY_PASSWORD,
    LABEL_SECRETS,
    LOG_MESSAGE,
    LOG_SECRETS,
    LOG_STACKTRACE,
    SECRET_LABELS,
    jaeger_payload,
    leaks,
    prometheus_payload,
    text_of,
)
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import (
    GOOD_EXPR,
    JAEGER_URL,
    NOW,
    PROMETHEUS_URL,
    WINDOW,
    FakeOpener,
    FakeStore,
)

# Observed with the public ``otel_demo_versions()`` / ``fixture_versions()``
# and ``PROJECTION_REVISION`` in an isolated ``git archive`` of the PR
# starting point ``origin/main`` d19e78a (2026-10-15), before U7-a.
OLD_PROJECTION_REVISION = "m1-01-tool-view-v7"
OLD_OTEL_TOOL_SCHEMA_REVISION = "otel-demo-d61d6f2a3b04"
OLD_FIXTURE_TOOL_SCHEMA_REVISION = "fixture-2"

PRODUCT_CONSTRAINT_SCENARIOS = ("F7:secret-scan",)


# -- harness --------------------------------------------------------------------


def _fixed_window_context(run_id: str) -> dict:
    return {
        "type": "opspilot-evidence-context-v4",
        "run_id": run_id,
        "time_policies": [
            {
                "id": "policy-window-1",
                "mode": "historical_window",
                "all_authorized_targets": True,
                "window": WINDOW.as_json(),
                "reference_rule": "response_received_at",
            }
        ],
    }


class _TransportSpy:
    """Records every ``TransportResponse`` the OTel Demo transport returned,
    so "byte-identical to what the transport returned" has an oracle."""

    def __init__(self, monkeypatch):
        self.bodies: list[bytes] = []
        original = OtelDemoTransport.fetch

        def fetch(transport, request):
            response = original(transport, request)
            self.bodies.append(bytes(response.body))
            return response

        monkeypatch.setattr(OtelDemoTransport, "fetch", fetch)


def _executor(monkeypatch, opener):
    lease = Lease(
        incident_id=uuid4(),
        run_id=uuid4(),
        owner=uuid4(),
        epoch=1,
        control_generation=3,
        global_suspension_generation=1,
        target_suspension_generation=2,
    )
    store = FakeStore(lease, deadline=NOW + timedelta(minutes=10))
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    sink = RecordingSink()
    factory = otel_demo_executor_factory(
        store,
        evidence=sink,
        clock=FakeClock(start=NOW),
        config=OtelDemoConfig(prometheus_url=PROMETHEUS_URL, jaeger_url=JAEGER_URL),
    )
    face = ToolFace(
        tool_schemas=TOOL_SCHEMAS,
        variant_id=DISCIPLINE_VARIANT,
        evidence_context=_fixed_window_context,
    )
    run_input = face.input_for(
        run_id=str(lease.run_id),
        question="Why is checkout erroring?",
        target_id=TARGET_ID,
        deadline=NOW + timedelta(minutes=10),
        model_requests=2,
    )
    executor = factory(lease, run_input)
    assert isinstance(executor, ReadOnlyToolExecutor)
    return executor, sink


def _call(tool: str, params: dict, *, index: int = 0) -> ToolRequest:
    return ToolRequest(
        step_id="step-1",
        tool_index=index,
        tool_name=tool,
        target_ref=TARGET_ID,
        params=params,
        window=WINDOW.as_json(),
    )


def _lab_env() -> dict[str, str]:
    return {
        "OPSPILOT_TRACE": "lab",
        "LANGSMITH_API_KEY": "fake-langsmith-key-not-real-f7f7",
        "LANGSMITH_PROJECT": "opspilot-lab-f7-step3",
        "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
        "OPSPILOT_TOOL_PROFILE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": PROMETHEUS_URL,
        "OPSPILOT_OTEL_JAEGER_URL": JAEGER_URL,
    }


def _exported_text(exporter: InMemorySpanExporter) -> str:
    """Flush the batch processor (stops the tracer) and join every span."""
    tracing.shutdown()
    parts = []
    for span in exporter.get_finished_spans():
        parts.append(span.name)
        parts.append(json.dumps(dict(span.attributes), default=str))
        for event in span.events:
            parts.append(json.dumps(dict(event.attributes), default=str))
    return "\n".join(parts)


@pytest.fixture
def exporter():
    memory = InMemorySpanExporter()
    tracer = tracing.configure(_lab_env(), exporter=memory)
    assert isinstance(tracer, tracing.Tracer), "lab tracer did not start"
    yield memory
    tracing.reset()


def _traced(executor, request):
    with tracing.tracer().run(
        incident_id=uuid4(), run_id=executor.scope.run_id, versions={}
    ) as span:
        outcome = executor.execute(request)
        span.settle("published", None)
    return outcome


def _assert_no_leak_in_outcome(outcome, sink, secrets):
    assert outcome.status == "ok", outcome.model_view
    record = outcome.evidence
    assert record is not None
    sinks = {
        "model_view": text_of(outcome.model_view),
        "evidence.raw": text_of(record.raw),
        "evidence.view": text_of(record.view),
        "sink.raw": text_of([r.raw for r in sink.records]),
        "sink.view": text_of([r.view for r in sink.records]),
    }
    for name, text in sinks.items():
        assert leaks(text, secrets) == [], name
    assert record.raw_sha256 == sha256(record.raw).hexdigest()
    for stored in sink.records:
        assert stored.raw_sha256 == sha256(stored.raw).hexdigest()
    return record


# -- S3c: span-log detail values -------------------------------------------------


def test_s3c_span_log_secrets_are_absent_from_view_evidence_and_trace(
    monkeypatch, exporter
):
    body = jaeger_payload(
        {"key": "exception.message", "value": LOG_MESSAGE},
        {"key": "exception.stacktrace", "value": LOG_STACKTRACE},
    )
    opener = FakeOpener(routes={"/api/traces": body})
    executor, sink = _executor(monkeypatch, opener)

    outcome = _traced(executor, _call(TRACES_TOOL, {"service": "checkout"}))

    record = _assert_no_leak_in_outcome(outcome, sink, LOG_SECRETS)
    assert len(opener.requests) == 1
    # The redaction is visible as the one marker, in the view the model gets
    # and in the stored evidence, rather than the detail silently vanishing.
    assert REDACTED_CREDENTIAL in text_of(outcome.model_view)
    assert REDACTED_CREDENTIAL in text_of(record.raw)
    assert outcome.model_view["trust"] == "untrusted-evidence"
    trace = _exported_text(exporter)
    assert trace, "no span exported"
    assert leaks(trace, LOG_SECRETS) == []


@pytest.mark.parametrize(
    ("secret", "prefix"),
    [
        (BOUNDARY_PASSWORD, " password="),
        (BOUNDARY_BEARER, " Authorization: Bearer "),
    ],
    ids=["password", "bearer"],
)
def test_s3c_a_secret_across_the_600_byte_cut_leaves_no_fragment(
    monkeypatch, exporter, secret, prefix
):
    # Pad so the 600-byte detail cut falls inside the secret's tail: a
    # truncate-then-redact order would leave a fragment the rules no longer
    # recognise (for Bearer: fewer than 8 token characters).
    tail = len(secret) - len("f7-synthetic-")
    pad = 600 - len(prefix) - len("f7-synthetic-") - tail // 2
    value = "x" * pad + prefix + secret + " while charging"
    assert len(value[:600].encode()) == 600 and secret not in value[:600]
    body = jaeger_payload({"key": "exception.message", "value": value})
    opener = FakeOpener(routes={"/api/traces": body})
    executor, sink = _executor(monkeypatch, opener)

    outcome = _traced(executor, _call(TRACES_TOOL, {"service": "checkout"}))

    _assert_no_leak_in_outcome(outcome, sink, (secret,))
    assert leaks(_exported_text(exporter), (secret,)) == []


# -- S3c: metric label values ----------------------------------------------------


def test_s3c_metric_label_secrets_are_absent_from_view_evidence_and_trace(
    monkeypatch, exporter
):
    body = prometheus_payload(SECRET_LABELS)
    opener = FakeOpener(by_query={GOOD_EXPR: body})
    executor, sink = _executor(monkeypatch, opener)

    outcome = _traced(executor, _call(METRICS_TOOL, {"expr": GOOD_EXPR}))

    record = _assert_no_leak_in_outcome(outcome, sink, LABEL_SECRETS)
    assert REDACTED_CREDENTIAL in text_of(outcome.model_view)
    assert REDACTED_CREDENTIAL in text_of(record.raw)
    # Ordinary labels survive: the series is still identifiable.
    assert "checkout" in text_of(outcome.model_view)
    assert leaks(_exported_text(exporter), LABEL_SECRETS) == []


# -- no secret: zero behaviour change -------------------------------------------


@pytest.mark.parametrize("tool", [METRICS_TOOL, TRACES_TOOL])
def test_a_payload_without_secrets_keeps_raw_byte_identical(monkeypatch, tool):
    spy = _TransportSpy(monkeypatch)
    opener = FakeOpener(
        by_query={GOOD_EXPR: prometheus_payload()},
        routes={"/api/traces": jaeger_payload()},
    )
    executor, sink = _executor(monkeypatch, opener)
    params = {"expr": GOOD_EXPR} if tool == METRICS_TOOL else {"service": "checkout"}

    outcome = executor.execute(_call(tool, params))

    assert outcome.status == "ok", outcome.model_view
    assert len(spy.bodies) == 1
    assert outcome.evidence is not None
    assert outcome.evidence.raw == spy.bodies[0]
    assert outcome.evidence.raw_sha256 == sha256(spy.bodies[0]).hexdigest()
    assert REDACTED_CREDENTIAL not in text_of(outcome.model_view)
    if tool == METRICS_TOOL:
        assert outcome.evidence.raw == prometheus_payload()


# -- contract section 3 class 6: revision bump ----------------------------------


def test_class6_projection_and_tool_schema_revisions_are_bumped():
    assert PROJECTION_REVISION == "m1-01-tool-view-v8"
    assert fixture_profile.TOOL_SCHEMA_REVISION == "fixture-3"
    assert otel_demo.TOOL_SCHEMA_REVISION != OLD_OTEL_TOOL_SCHEMA_REVISION
    assert (
        otel_demo.otel_demo_versions()["tool_schema_revision"]
        != OLD_OTEL_TOOL_SCHEMA_REVISION
    )
    assert (
        fixture_profile.fixture_versions()["tool_schema_revision"]
        != OLD_FIXTURE_TOOL_SCHEMA_REVISION
    )
    assert PROJECTION_REVISION != OLD_PROJECTION_REVISION


def test_class6_new_evidence_carries_the_new_projection_revision(monkeypatch):
    opener = FakeOpener(by_query={GOOD_EXPR: prometheus_payload()})
    executor, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}))
    assert outcome.evidence is not None
    assert outcome.evidence.projection_revision == "m1-01-tool-view-v8"
