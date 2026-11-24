"""#744 item 4: the Run span's ``opspilot.run.tool_calls`` counts data-source
tool calls only; task-list (``TodoWrite``) calls go to
``opspilot.run.task_list_calls``.

Written from the batch 2 pre-review, section 六, first bullet
(docs/tasks/2026-11-22-next-batches-b2-prereview.md) and C1-9 (the task list
is not counted as a query or a tool call;
docs/tasks/2026-11-20-tool-expansion-c1.md:65), without reading the
implementation. The ``summary.json`` / comparison-table query count is the
number of gateway requests the Run made (``run.tool_requests``,
scripts/holmes_evals/todo_compare.py:37); here the same Run's gateway
requests are what reaches the counting executor double.

Chosen expectation: ``task_list_calls`` is always set on the Run span, 0 when
the Run made no task-list call (like ``opspilot.run.model_calls``).
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from opspilot import tracing
from tests.m1_investigation_support import tool_call
from tests.test_tool_expansion_c1_contract import (
    KNOWLEDGE_SWITCH,
    METRICS,
    SWITCH,
    Harness,
    _cite_metrics,
    _items,
    _metrics_call,
    _todo_call,
    _tools,
)
from tests.test_tracing import lab_env


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (SWITCH, KNOWLEDGE_SWITCH, "OPSPILOT_CLUSTER_NAME"):
        monkeypatch.delenv(name, raising=False)
    yield
    tracing.reset()


def _second_query(call_id="call-metrics-2"):
    # A different expression, so it is not a refused duplicate.
    args = json.dumps({"expr": "rate(http_requests_total[5m])"})
    return tool_call(call_id=call_id, name=METRICS, arguments=args)


def _traced(h: Harness):
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(lab_env(), exporter=exporter)
    with tracer.run(
        incident_id=uuid4(),
        run_id=uuid4(),
        versions={"prompt_revision": "p", "tool_schema_revision": "t"},
    ) as span:
        outcome = h.run()
        span.settle("published", None)
    tracer.shutdown()
    [root] = [s for s in exporter.get_finished_spans() if s.name == "opspilot.run"]
    return outcome, dict(root.attributes)


def test_task_list_calls_are_counted_apart_from_data_source_calls():
    h = Harness(
        [
            _tools(_metrics_call(), _todo_call(_items("in_progress", "pending"))),
            _tools(_second_query()),
            _cite_metrics,
        ],
        model_requests=3,
    )
    outcome, attrs = _traced(h)
    assert outcome.execution == "completed", outcome.handoff_reasons
    assert len(h.executor.requests) == 2  # the two data-source calls ran
    assert attrs["opspilot.run.tool_calls"] == 2
    assert attrs["opspilot.run.task_list_calls"] == 1
    # The same Run's gateway request count, which summary.json reports.
    assert attrs["opspilot.run.tool_calls"] == len(h.executor.requests)


def test_a_run_without_the_task_list_reports_zero_task_list_calls():
    h = Harness([_tools(_metrics_call()), _cite_metrics], model_requests=2, todo=False)
    outcome, attrs = _traced(h)
    assert outcome.execution == "completed", outcome.handoff_reasons
    assert attrs["opspilot.run.tool_calls"] == 1 == len(h.executor.requests)
    assert attrs["opspilot.run.task_list_calls"] == 0


def test_a_task_list_only_round_adds_no_data_source_call():
    h = Harness(
        [
            _tools(
                _todo_call(_items("pending"), "c-1"),
                _todo_call(_items("completed"), "c-2"),
            ),
            _tools(_metrics_call()),
            _cite_metrics,
        ],
        model_requests=3,
    )
    outcome, attrs = _traced(h)
    assert outcome.execution == "completed", outcome.handoff_reasons
    assert attrs["opspilot.run.tool_calls"] == len(h.executor.requests) == 1
    assert attrs["opspilot.run.task_list_calls"] == 2
