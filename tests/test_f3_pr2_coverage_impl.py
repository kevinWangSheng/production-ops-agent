"""F3 PR-2 implementer tests: coverage and truncation rules, stub, guards.

The independent acceptance tests live in
``tests/acceptance/test_f3_step3_coverage.py``; these pin the pure rules of
``opspilot.investigation.coverage`` (contract r1 D1-D5, R-2-R-5, R-10, R-15)
and the model-visible revisions (R-14).
"""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.request
from datetime import timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from opspilot.acceptance import PRODUCT_CONSTRAINT_SCENARIO_INDEX
from opspilot.investigation.context import context_policy_revision
from opspilot.investigation.coverage import (
    INTERNAL_SOURCE,
    collection_truncated,
    run_coverage,
)
from opspilot.investigation.loop import DISCIPLINE_VARIANT, prompt_revision_versions
from opspilot.tools.fixture import fixture_face
from opspilot.tools.otel_demo import (
    LABEL_VALUES_TOOL,
    METRIC_NAMES_TOOL,
    METRICS_TOOL,
    TARGET_ID,
    TOOL_SCHEMA_REVISION,
    TRACES_TOOL,
    otel_demo_face,
)
from opspilot.tools.outcomes import PROJECTION_REVISION
from scripts.m1_otel_stub import handler
from tests.m1_tool_support import NOW, WINDOW_END, FakeClock

ROOT = Path(__file__).resolve().parents[1]
ANSWER = ROOT / "evals/f3/missing-source/answer.json"


def _otel_input(model_requests: int = 6, schemas=None) -> dict:
    snapshot = (
        otel_demo_face(FakeClock(start=WINDOW_END))
        .input_for(
            run_id="run-1",
            question="Why is checkout erroring?",
            target_id=TARGET_ID,
            deadline=NOW + timedelta(minutes=10),
            model_requests=model_requests,
        )
        .as_json()
    )
    if schemas is not None:
        snapshot["tool_schemas"] = [
            item
            for item in snapshot["tool_schemas"]
            if item["function"]["name"] in schemas
        ]
    return snapshot


def _result(tool, status="ok", reason=None, **extra):
    view = {"tool": tool, "status": status, **extra}
    if reason is not None:
        view["reason"] = reason
    return view


def _step(*results, status="tool_result_committed", calls=None, **response):
    names = calls if calls is not None else [r.get("tool") for r in results]
    return {
        "step_id": response.pop("step_id", None),
        "status": status,
        "response": {
            "assistant": {
                "role": "assistant",
                "tool_calls": [
                    {"id": f"c{i}", "function": {"name": name or "x"}}
                    for i, name in enumerate(names)
                ],
            },
            **response,
        },
        "tool_results": [
            {"ordinal": i, "result": dict(result)} for i, result in enumerate(results)
        ],
    }


def _rows(coverage):
    return {row.name: row for row in coverage.sources}


# -- coverage ----------------------------------------------------------------


def test_sources_come_from_the_recorded_data_sources():
    steps = [
        _step(
            _result(TRACES_TOOL, "error", "SOURCE_UNAVAILABLE"),
            _result(TRACES_TOOL, "error", "SOURCE_UNAVAILABLE"),
            _result(METRICS_TOOL),
        )
    ]
    coverage = run_coverage("run-1", _otel_input(), steps)
    rows = _rows(coverage)
    assert coverage.sources_recorded is True
    assert [row.name for row in coverage.sources] == [
        "prometheus",
        "jaeger",
        "kubernetes_state",
        "pod_logs",
        "change_history",
    ]
    jaeger = rows["jaeger"]
    assert (jaeger.ok, jaeger.non_ok, jaeger.state, jaeger.gap) == (
        0,
        2,
        "failed",
        True,
    )
    assert dict(jaeger.reasons) == {"SOURCE_UNAVAILABLE": 2}
    prometheus = rows["prometheus"]
    assert (prometheus.ok, prometheus.state, prometheus.gap) == (1, "all_ok", False)
    for name in ("kubernetes_state", "pod_logs", "change_history"):
        assert (rows[name].state, rows[name].gap) == ("not_integrated", True)


def test_reasons_are_counted_apart_and_ordered_by_count():
    steps = [
        _step(
            _result(METRICS_TOOL, "error", "INVALID_PARAMS"),
            _result(METRICS_TOOL, "timeout", "TOOL_TIMEOUT"),
            _result(METRICS_TOOL, "timeout", "TOOL_TIMEOUT"),
            _result(METRICS_TOOL, "error", "SOURCE_UNAVAILABLE"),
            _result(METRICS_TOOL),
        )
    ]
    prometheus = _rows(run_coverage("r", _otel_input(), steps))["prometheus"]
    assert list(prometheus.reasons.items()) == [
        ("TOOL_TIMEOUT", 2),
        ("INVALID_PARAMS", 1),
        ("SOURCE_UNAVAILABLE", 1),
    ]
    assert (prometheus.ok, prometheus.non_ok, prometheus.state) == (
        1,
        4,
        "partial_failure",
    )
    assert prometheus.gap is True


def test_no_data_only_and_not_queried_are_gaps():
    steps = [_step(_result(METRICS_TOOL, "no_data"))]
    rows = _rows(run_coverage("r", _otel_input(), steps))
    assert (rows["prometheus"].state, rows["prometheus"].gap) == ("no_data_only", True)
    assert rows["prometheus"].no_data == 1
    assert (rows["jaeger"].state, rows["jaeger"].gap) == ("not_queried", True)


def test_a_source_with_no_offered_tool_is_not_authorized():
    schemas = {METRICS_TOOL, METRIC_NAMES_TOOL, LABEL_VALUES_TOOL, "read_evidence"}
    steps = [
        _step(
            _result(TRACES_TOOL, "denied", "TOOL_NOT_IN_SCOPE"),
            _result(METRICS_TOOL),
        )
    ]
    rows = _rows(run_coverage("r", _otel_input(schemas=schemas), steps))
    assert (rows["jaeger"].state, rows["jaeger"].gap) == ("not_authorized", True)
    assert dict(rows["jaeger"].reasons) == {"TOOL_NOT_IN_SCOPE": 1}
    assert rows["prometheus"].state == "all_ok"


def test_fixture_profile_lists_traces_as_not_integrated():
    snapshot = (
        fixture_face()
        .input_for(
            run_id="r",
            question="q",
            target_id="checkout-prod",
            deadline=NOW,
            model_requests=2,
        )
        .as_json()
    )
    rows = _rows(run_coverage("r", snapshot, []))
    assert rows["traces"].state == "not_integrated"
    assert rows["prometheus"].state == "not_queried"


def test_duplicates_late_results_and_missing_tool_keys():
    duplicate = {
        "status": "denied",
        "reason": "DUPLICATE_TOOL_CALL",
        "duplicate_of_evidence_id": "e1",
    }
    unnamed = {"status": "error", "reason": "SOURCE_UNAVAILABLE"}
    steps = [
        _step(_result(METRICS_TOOL), duplicate, calls=[METRICS_TOOL, METRICS_TOOL]),
        # R-5: no ``tool`` key; the name comes from the planned call.
        _step(unnamed, calls=[TRACES_TOOL]),
        _step(dict(unnamed), calls=[]),
        _step(_result(TRACES_TOOL, "error", "X"), status="late_result"),
    ]
    coverage = run_coverage("r", _otel_input(), steps)
    rows = _rows(coverage)
    assert coverage.duplicates_skipped == 1
    assert rows["prometheus"].non_ok == 0
    assert dict(rows["jaeger"].reasons) == {"SOURCE_UNAVAILABLE": 1}
    assert rows["unknown"].non_ok == 1


def test_read_evidence_slices_follow_their_parent_and_refusals_are_internal():
    steps = [
        _step(
            _result(METRICS_TOOL),
            _result("read_evidence", parent_tool=METRICS_TOOL),
            _result("read_evidence", "denied", "EVIDENCE_NOT_READABLE"),
        )
    ]
    rows = _rows(run_coverage("r", _otel_input(), steps))
    assert (rows["prometheus"].ok, rows["prometheus"].state) == (2, "all_ok")
    internal = rows[INTERNAL_SOURCE]
    assert (internal.state, internal.gap, internal.non_ok) == ("internal", False, 1)


def test_a_legacy_run_is_counted_per_tool_and_claims_nothing_missing():
    snapshot = _otel_input()
    del snapshot["evidence_context"]["data_sources"]
    steps = [_step(_result(METRICS_TOOL), _result(TRACES_TOOL, "timeout", "T"))]
    coverage = run_coverage("r", snapshot, steps)
    assert coverage.sources_recorded is False
    assert {row.name: row.state for row in coverage.sources} == {
        METRICS_TOOL: "all_ok",
        TRACES_TOOL: "failed",
    }
    assert run_coverage("r", None, []).sources == ()


# -- truncation --------------------------------------------------------------


def _round(step_id, *, final, tools=(), rejected=None):
    response = {
        "assistant": {
            "role": "assistant",
            "tool_calls": [{"function": {"name": t}} for t in tools],
        },
        "context": {"final": final},
    }
    if rejected is not None:
        response["rejected_plan"] = rejected
    return {"step_id": step_id, "status": "tool_result_committed", "response": response}


def _conclusion(used, source):
    return {
        "kind": "conclusion",
        "conclusion": {"model_requests_used": used, "source_step_id": source},
    }


def test_a_forced_final_round_at_the_limit_is_truncated():
    steps = [
        _round("s1", final=False, tools=[METRICS_TOOL]),
        _round("s2", final=False, tools=[METRICS_TOOL]),
        _round("s3", final=True),
    ]
    assert collection_truncated(_otel_input(3), steps, _conclusion(3, "s3")) is True
    # A one-request Run has no earlier round: still the bound's doing.
    assert (
        collection_truncated(
            _otel_input(1), [_round("s1", final=True)], _conclusion(1, "s1")
        )
        is True
    )


def test_the_l1a_retry_and_an_early_report_are_not_truncated():
    l1a = [
        _round("s1", final=False, tools=[METRICS_TOOL]),
        _round("s2", final=False),
        _round("s3", final=True),
    ]
    assert collection_truncated(_otel_input(3), l1a, _conclusion(3, "s3")) is False
    early = [_round("s1", final=False, tools=[METRICS_TOOL]), _round("s2", final=False)]
    assert collection_truncated(_otel_input(6), early, _conclusion(2, "s2")) is False


def test_late_and_compaction_rows_do_not_count_as_the_prior_round():
    steps = [
        _round("s1", final=False, tools=[METRICS_TOOL]),
        {**_round("late", final=False), "status": "late_result"},
        {
            "step_id": "c1",
            "status": "response_committed",
            "response": {"kind": "compaction", "context": {"final": False}},
        },
        _round("s2", final=True),
    ]
    assert collection_truncated(_otel_input(3), steps, _conclusion(3, "s2")) is True


def test_missing_fields_give_none():
    steps = [_round("s1", final=True)]
    legacy = {
        "step_id": "s1",
        "status": "tool_result_committed",
        "response": {"assistant": {"role": "assistant"}},
    }
    assert collection_truncated(_otel_input(1), [legacy], _conclusion(1, "s1")) is None
    # Below the limit is still unjudgeable without ``context.final`` (R-10).
    assert collection_truncated(_otel_input(6), [legacy], _conclusion(1, "s1")) is None
    assert collection_truncated({}, steps, _conclusion(1, "s1")) is None
    assert collection_truncated(_otel_input(1), steps, None) is None
    assert collection_truncated(_otel_input(1), steps, {"assistant": {}}) is None
    assert collection_truncated(_otel_input(1), steps, _conclusion(1, "zz")) is None


# -- stub, scenario index, frozen answer, revisions ---------------------------


@pytest.fixture
def stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(0.0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_stub_serves_discovery_label_values(stub):
    with urllib.request.urlopen(
        f"{stub}/api/v1/label/__name__/values?match[]=up&limit=1"
    ) as response:
        names = json.loads(response.read())
    assert names["status"] == "success"
    assert "traces_span_metrics_calls_total" in names["data"]
    with urllib.request.urlopen(f"{stub}/api/v1/label/other/values") as response:
        assert json.loads(response.read())["data"] == []


def test_scenarios_are_indexed():
    index = PRODUCT_CONSTRAINT_SCENARIO_INDEX["workflow_uncertainty_visible"]
    assert {"F3:missing-source", "F3:truncated-at-limit"} <= set(index)


def test_missing_source_answer_is_frozen():
    digest = hashlib.sha256(ANSWER.read_bytes()).hexdigest()
    assert digest == "b9a541ab9d13b7fce5433a923263eb133936b95e59822561d136ff6fa3ddaab5"
    assert json.loads(ANSWER.read_bytes())["required_source"] == "jaeger"


def test_model_visible_revisions_are_unchanged():
    # R-14: the values on main e2fcea4; this PR only adds read-side views.
    assert TOOL_SCHEMA_REVISION == "otel-demo-68ed2ca8f5d6"
    assert PROJECTION_REVISION == "m1-01-tool-view-v10"
    assert prompt_revision_versions(DISCIPLINE_VARIANT) == {
        "prompt_revision": "prompt-replay-candidate-bef70649a3a8"
    }
    assert context_policy_revision() == "ctx-ctx-policy-v1-a30e65ea52f4"
