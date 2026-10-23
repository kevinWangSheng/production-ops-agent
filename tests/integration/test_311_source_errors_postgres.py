"""#311 (F3 PR-2) on the product path: one worker Run against real sockets.

Contract: ``tmp/prereview-311.md`` (U1-U6 as recommended, R1-R7), items 2, 7
and 8 of "给独立验收测试作者的可观察行为", with the projection at v10.

The web workbench (``otel-demo`` profile) records the Run input; the real
``InvestigationRunner`` drives the real ``DeepSeekClient`` whose HTTPS opener
is a scripted provider double; Prometheus and Jaeger are one local HTTP server
reached through the production urllib transport (urllib is not mocked). The
model asks for a malformed PromQL (Prometheus answers 400 with an error text
carrying synthetic secrets), for a query the source refuses with 401 (body
echoing credentials) and for a traces search that succeeds.

Run with ``M1_DURABLE_POSTGRES=1`` and ``OPSPILOT_LAB_DSN``; a scratch
database is created and dropped.
"""

from __future__ import annotations

import html
import json
import os
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from opspilot import tracing
from opspilot.knowledge import KnowledgeStore
from opspilot.persistence import DurableStore
from opspilot.redaction import REDACTED_CREDENTIAL
from opspilot.tools.otel_demo import METRICS_TOOL, TARGET_ID, TRACES_TOOL
from opspilot.tools.profiles import PROFILE_ENV, select_profile
from opspilot.web import (
    Authenticator,
    DurableClock,
    DurableEventLog,
    DurableEvidenceStore,
    create_app,
)
from tests.acceptance.f7_hostile_support import jaeger_payload, leaks, text_of
from tests.acceptance.f7_pg_support import (
    MODEL_KEY,
    POOL,
    Harness,
    ScriptedProvider,
    _auth,
    _workbench,
    assistant,
    evidence_rows,
    final_report,
    fn,
    scratch_database,
    tool_views,
)
from tests.acceptance.test_311_source_errors import (
    ECHO_SECRETS,
    SECRETS,
    _echo_body,
    _secret_body,
    _SourceStub,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

QUESTION = "Why is checkout erroring?"
BAD_EXPR = "sum(rate(http_requests_total[5m])"
REFUSED_EXPR = "up"
ALL_SECRETS = (*SECRETS, *ECHO_SECRETS)


@pytest.fixture(scope="module")
def dsn():
    yield from scratch_database()


@pytest.fixture
def stub():
    server = _SourceStub()

    def respond(path: str, query: dict[str, list[str]]):
        if path.endswith("/api/v1/query_range"):
            expr = (query.get("query") or [""])[0]
            if expr == BAD_EXPR:
                return 400, _secret_body()
            return 401, _echo_body()
        if path.endswith("/api/traces"):
            return 200, jaeger_payload()
        return 404, b"{}"

    server.respond = respond
    try:
        yield server
    finally:
        server.close()


def _harness(dsn: str, monkeypatch, stub: _SourceStub) -> Harness:
    monkeypatch.delenv("OPSPILOT_OTEL_TOKEN", raising=False)
    # The profile reads its endpoints from the mapping it is given.
    profile = select_profile(
        {
            PROFILE_ENV: "otel-demo",
            "OPSPILOT_OTEL_PROMETHEUS_URL": stub.url,
            "OPSPILOT_OTEL_JAEGER_URL": f"{stub.url}/jaeger/ui",
        }
    )
    store = DurableStore(dsn, pool=POOL)
    store.install()
    events = DurableEventLog(store)
    evidence = DurableEvidenceStore(store)
    knowledge = KnowledgeStore(dsn, pool=POOL)
    workbench = _workbench(store, events, evidence, profile.versions(), knowledge)
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


def _lab_env(stub: _SourceStub) -> dict[str, str]:
    return {
        "OPSPILOT_TRACE": "lab",
        "LANGSMITH_API_KEY": "fake-langsmith-key-not-real-3110",
        "LANGSMITH_PROJECT": "opspilot-lab-311",
        "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
        "OPSPILOT_TOOL_PROFILE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": stub.url,
        "OPSPILOT_OTEL_JAEGER_URL": f"{stub.url}/jaeger/ui",
    }


def _exported_text(exporter: InMemorySpanExporter) -> str:
    tracing.shutdown()
    parts = []
    for span in exporter.get_finished_spans():
        parts.append(span.name)
        parts.append(json.dumps(dict(span.attributes), default=str))
        for event in span.events:
            parts.append(json.dumps(dict(event.attributes), default=str))
    return "\n".join(parts)


def _context_messages(request: dict[str, Any]) -> list[dict[str, Any]]:
    found = []
    for message in request["messages"]:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, str):
            continue
        try:
            value = json.loads(content)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("type") == (
            "opspilot-evidence-context-v4"
        ):
            found.append(value)
    return found


def _steps_section(page: str) -> str:
    start = page.index("<h2>Steps</h2>")
    return page[start : page.index("</table>", start)]


def _labels(entry: Any) -> list[str]:
    if isinstance(entry, str):
        return [entry]
    if isinstance(entry, dict):
        return [v for v in entry.values() if isinstance(v, str)]
    return []


def test_311_source_errors_and_data_sources_on_the_product_path(dsn, monkeypatch, stub):
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(_lab_env(stub), exporter=exporter)
    assert isinstance(tracer, tracing.Tracer), "lab tracer did not start"
    harness = _harness(dsn, monkeypatch, stub)
    try:
        incident, run = harness.submit(QUESTION, target_id=TARGET_ID)
        provider = ScriptedProvider(
            [
                assistant(
                    tool_calls=[
                        fn("bad-query", METRICS_TOOL, {"expr": BAD_EXPR}),
                        fn("refused-query", METRICS_TOOL, {"expr": REFUSED_EXPR}),
                        fn("read-traces", TRACES_TOOL, {"service": "checkout"}),
                    ]
                ),
                final_report(TARGET_ID),
            ]
        )

        harness.run(incident, provider)

        assert len(stub.requests) == 3, stub.requests
        assert len(provider.requests) == 2

        # Item 7: recorded with the Run input at submission, and sent in the
        # initial context message of the model input.
        snapshot = harness.store.rebuild(incident)
        recorded = snapshot["run"]["input"]["evidence_context"]
        data_sources = recorded.get("data_sources")
        assert isinstance(data_sources, dict), recorded
        assert len(data_sources["available"]) == 2
        assert len(data_sources["not_integrated"]) == 3
        contexts = _context_messages(provider.requests[0])
        assert len(contexts) == 1
        assert contexts[0].get("data_sources") == data_sources

        # Items 1 and 4 through the loop: the views the model received.
        views = {
            item["tool_call_id"]: item["view"]
            for item in tool_views(provider.requests[-1]["messages"])
        }
        bad = views["bad-query"]
        assert (bad["status"], bad["reason"]) == ("error", "INVALID_PARAMS")
        assert bad["trust"] == "gateway" and bad["content"] is None
        assert bad["source_error"]["trust"] == "untrusted-evidence"
        assert bad["source_error"]["http_status"] == 400
        assert "marker311parse" in bad["source_error"]["error"]
        assert REDACTED_CREDENTIAL in bad["source_error"]["error"]
        refused = views["refused-query"]
        assert (refused["status"], refused["reason"]) == ("error", "SOURCE_ERROR")
        assert refused["source_error"]["http_status"] == 401
        assert refused["source_error"]["error"] is None
        assert "marker311body" not in text_of(refused)
        assert views["read-traces"]["status"] == "ok"

        # U4: no evidence row for either source error.
        rows = evidence_rows(harness, run)
        assert len(rows) == 1, [r["evidence_id"] for r in rows]

        # Item 8: the page's step table shows the status codes and the
        # redacted text, and the Run's not-integrated sources.
        page = harness.get(f"/incidents/{incident}", html=True)
        assert page.status == 200, page.text
        text = html.unescape(page.text)
        steps = html.unescape(_steps_section(page.text))
        assert "400" in steps and "401" in steps
        assert "marker311parse" in steps
        assert REDACTED_CREDENTIAL in steps
        assert "marker311body" not in text
        for entry in data_sources["not_integrated"]:
            labels = _labels(entry)
            assert labels and any(label in text for label in labels), entry

        # Item 2: zero secret fragments anywhere the text travelled.
        trace = _exported_text(exporter)
        assert "marker311parse" in trace
        sinks = {
            "model input": text_of(b"\n".join(provider.raw_requests)),
            "step storage": text_of(snapshot),
            "pg evidence": text_of([r["raw"] for r in rows])
            + "\n".join(r["view"] for r in rows),
            "trace export": trace,
            "web page": text,
        }
        found = {name: leaks(value, ALL_SECRETS) for name, value in sinks.items()}
        assert found == {name: [] for name in sinks}
        for name, value in sinks.items():
            assert MODEL_KEY not in value, name
    finally:
        tracing.reset()
        for closer in harness.closers:
            closer.close()
