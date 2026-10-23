"""#309 on the durable product path: one turn's two otel-demo tool calls run
at once under ``tool_parallelism=2`` against PostgreSQL (pooled store,
durable evidence, ledger and control), committed in index order.

Run with ``M1_DURABLE_POSTGRES=1`` and ``OPSPILOT_LAB_DSN``; a scratch
database is created and dropped.
"""

from __future__ import annotations

import os
import threading

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from opspilot import tracing
from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.runner import InvestigationRunner
from opspilot.tools.otel_demo import TARGET_ID
from opspilot.web.store import DurableClock
from opspilot.worker import Worker
from tests.acceptance.f7_hostile_support import (
    SECRET_LABELS,
    jaeger_payload,
    prometheus_payload,
)
from tests.acceptance.f7_pg_support import (
    MODEL_KEY,
    ScriptedProvider,
    assistant,
    build_harness,
    final_report,
    fn,
    scratch_database,
    tool_views,
)
from tests.test_m1_otel_demo_contract import (
    GOOD_EXPR,
    JAEGER_URL,
    PROMETHEUS_URL,
    FakeOpener,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)


@pytest.fixture(scope="module")
def dsn():
    yield from scratch_database()


class _OverlapOpener(FakeOpener):
    """Each source request waits until both are in flight at once."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.barrier = threading.Barrier(2, timeout=10)
        self._lock = threading.Lock()

    def open(self, request, timeout=None):
        self.barrier.wait()
        with self._lock:
            return super().open(request, timeout=timeout)


def _lab_env() -> dict[str, str]:
    return {
        "OPSPILOT_TRACE": "lab",
        "LANGSMITH_API_KEY": "fake-langsmith-key-not-real-0309",
        "LANGSMITH_PROJECT": "opspilot-lab-309",
        "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
        "OPSPILOT_TOOL_PROFILE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": PROMETHEUS_URL,
        "OPSPILOT_OTEL_JAEGER_URL": JAEGER_URL,
    }


def test_two_tool_calls_overlap_on_the_durable_path(dsn, monkeypatch):
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(_lab_env(), exporter=exporter)
    assert isinstance(tracer, tracing.Tracer)
    opener = _OverlapOpener(
        by_query={GOOD_EXPR: prometheus_payload(SECRET_LABELS)},
        routes={"/api/traces": jaeger_payload()},
    )
    harness = build_harness(dsn, monkeypatch, opener)
    try:
        incident, run = harness.submit("Why is checkout erroring?", target_id=TARGET_ID)
        provider = ScriptedProvider(
            [
                assistant(
                    tool_calls=[
                        fn("read-traces", "traces_search", {"service": "checkout"}),
                        fn("read-metrics", "metrics_range_query", {"expr": GOOD_EXPR}),
                    ]
                ),
                final_report(TARGET_ID),
            ]
        )
        clock = DurableClock(harness.store)
        runner = InvestigationRunner(
            store=harness.store,
            worker=Worker.create(harness.store, harness.profile.versions()),
            model=DeepSeekClient(MODEL_KEY, opener=provider),
            executor_factory=harness.profile.executor_factory(
                harness.store, harness.evidence, clock
            ),
            clock=clock,
            lease_seconds=30,
            events=harness.events,
            evidence=harness.evidence,
            tool_parallelism=2,
        )
        outcome = runner.resume(incident)

        assert outcome.status == "published", outcome
        assert len(opener.requests) == 2
        views = tool_views(provider.requests[-1]["messages"])
        assert [v["tool_call_id"] for v in views] == ["read-traces", "read-metrics"]
        assert all(v["view"]["status"] == "ok" for v in views), views
        snapshot = harness.store.rebuild(incident)
        tool_step = next(s for s in snapshot["steps"] if s["tool_results"])
        assert [r["ordinal"] for r in tool_step["tool_results"]] == [0, 1]

        tracing.shutdown()
        spans = exporter.get_finished_spans()
        root = next(s for s in spans if s.name == "opspilot.run")
        tools = [s for s in spans if s.name.startswith("tool:")]
        assert len(tools) == 2
        assert {s.parent.span_id for s in tools} == {root.context.span_id}
        first, second = sorted(tools, key=lambda s: s.start_time)
        assert second.start_time < first.end_time  # the two calls overlapped
    finally:
        tracing.reset()
        for closer in harness.closers:
            closer.close()
