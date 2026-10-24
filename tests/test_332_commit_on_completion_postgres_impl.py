"""#332 on the durable product path: while one otel-demo call is still in
flight, its finished sibling is already committed in PostgreSQL, and the
step's rows end up sorted by ordinal.

Run with ``M1_DURABLE_POSTGRES=1`` and ``OPSPILOT_LAB_DSN``; a scratch
database is created and dropped.
"""

from __future__ import annotations

import os
import time
from urllib.parse import urlsplit

import pytest

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
from tests.test_m1_otel_demo_contract import GOOD_EXPR, FakeOpener, _url

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)


@pytest.fixture(scope="module")
def dsn():
    yield from scratch_database()


class _TracesWaitForMetrics(FakeOpener):
    """The traces call (index 0) answers only once ordinal 1 is in PG."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.committed_ordinals: list[int] = []
        self.saw_sibling: bool | None = None
        self.poll = None

    def open(self, request, timeout=None):
        if urlsplit(_url(request)).path.endswith("/api/traces"):
            deadline = time.monotonic() + 5
            self.saw_sibling = False
            while time.monotonic() < deadline:
                if 1 in self.poll():
                    self.saw_sibling = True
                    break
                time.sleep(0.02)
        return super().open(request, timeout=timeout)


def test_a_finished_sibling_is_in_postgres_while_the_other_call_runs(dsn, monkeypatch):
    opener = _TracesWaitForMetrics(
        by_query={GOOD_EXPR: prometheus_payload(SECRET_LABELS)},
        routes={"/api/traces": jaeger_payload()},
    )
    harness = build_harness(dsn, monkeypatch, opener)
    try:
        incident, run = harness.submit("Why is checkout erroring?", target_id=TARGET_ID)

        def committed():
            snapshot = harness.store.rebuild(incident)
            return {r["ordinal"] for s in snapshot["steps"] for r in s["tool_results"]}

        opener.poll = committed
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
        assert opener.saw_sibling is True
        assert len(opener.requests) == 2
        # The model still sees index order.
        views = tool_views(provider.requests[-1]["messages"])
        assert [v["tool_call_id"] for v in views] == ["read-traces", "read-metrics"]
        snapshot = harness.store.rebuild(incident)
        tool_step = next(s for s in snapshot["steps"] if s["tool_results"])
        assert [r["ordinal"] for r in tool_step["tool_results"]] == [0, 1]
    finally:
        for closer in harness.closers:
            closer.close()
